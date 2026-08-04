"""Redis client wrapper: caching, locks and health.

Redis carries four different workloads in KnowledgeOS — cache, rate-limit counters,
distributed locks and the Celery broker. They share one server but are namespaced by
key prefix so a `FLUSHDB` during a cache incident cannot destroy queued jobs.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import AsyncIterator
from typing import Any

import orjson
import redis.asyncio as aioredis
from redis.asyncio.client import Redis

from .config import ServiceSettings
from .logging import get_logger

logger = get_logger(__name__)

#: Lua script for a safe lock release: only the owner's token may delete the key,
#: which prevents a slow holder from releasing a lock another worker has since taken.
_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisClient:
    """Thin, typed convenience layer over ``redis.asyncio``."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._prefix = f"kos:{settings.service_name}"
        self._client: Redis = aioredis.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            decode_responses=False,
            health_check_interval=30,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
        self._release_lock = self._client.register_script(_RELEASE_LOCK_LUA)

    @property
    def client(self) -> Redis:
        """Raw client, for commands this wrapper does not cover."""
        return self._client

    def key(self, *parts: str) -> str:
        """Build a namespaced key: ``kos:<service>:<part>:<part>``."""
        return ":".join([self._prefix, *parts])

    # ---- cache ----------------------------------------------------------

    async def get_json(self, key: str) -> Any | None:
        raw = await self._client.get(self.key("cache", key))
        if raw is None:
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            # A poisoned cache entry should never break a request path.
            logger.warning("redis.cache_decode_failed", cache_key=key)
            await self._client.delete(self.key("cache", key))
            return None

    async def set_json(self, key: str, value: Any, *, ttl: int = 300) -> None:
        await self._client.set(self.key("cache", key), orjson.dumps(value), ex=ttl)

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._client.delete(*[self.key("cache", k) for k in keys]))

    async def invalidate_prefix(self, prefix: str) -> int:
        """Delete every cache key under a prefix using SCAN (never ``KEYS``)."""
        pattern = self.key("cache", prefix) + "*"
        deleted = 0
        async for batch in self._scan_batches(pattern):
            if batch:
                deleted += int(await self._client.delete(*batch))
        return deleted

    async def _scan_batches(self, pattern: str, count: int = 500) -> AsyncIterator[list[bytes]]:
        cursor = 0
        while True:
            cursor, keys = await self._client.scan(cursor=cursor, match=pattern, count=count)
            yield keys
            if cursor == 0:
                return

    # ---- distributed lock ----------------------------------------------

    @contextlib.asynccontextmanager
    async def lock(self, name: str, *, ttl: int = 30) -> AsyncIterator[bool]:
        """Best-effort mutex. Yields ``True`` if this caller holds the lock.

        Used to make scheduled jobs single-flight when several worker replicas wake
        up at the same instant. The TTL bounds how long a crashed holder can block
        others, so pick a value larger than the critical section.
        """
        lock_key = self.key("lock", name)
        token = secrets.token_hex(16)
        acquired = bool(await self._client.set(lock_key, token, nx=True, ex=ttl))
        try:
            yield acquired
        finally:
            if acquired:
                with contextlib.suppress(Exception):
                    await self._release_lock(keys=[lock_key], args=[token])

    # ---- counters -------------------------------------------------------

    async def incr_with_ttl(self, key: str, *, ttl: int) -> int:
        """Atomically increment and set the TTL only on first write."""
        full = self.key("counter", key)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(full)
            pipe.expire(full, ttl, nx=True)
            value, _ = await pipe.execute()
        return int(value)

    # ---- lifecycle ------------------------------------------------------

    async def healthcheck(self) -> dict[str, Any]:
        try:
            await self._client.ping()
            return {"status": "up"}
        except Exception as exc:
            logger.warning("redis.healthcheck_failed", error=str(exc))
            return {"status": "down", "error": str(exc)}

    async def close(self) -> None:
        await self._client.aclose()
        logger.info("redis.closed")

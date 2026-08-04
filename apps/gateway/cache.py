"""Edge response cache.

**The failure mode this file exists to prevent is serving one user's response to
another.** A cache key built from the path alone will happily store an authenticated
``/v1/library`` response and then hand it to the next anonymous caller. Everything
below follows from refusing to let that happen:

* The key always encodes the **auth scope** — anonymous, or a specific user id.
* A route must opt in with ``cache_vary_on_user`` before a per-user response is
  cached at all, and then it is keyed by user id.
* Only ``GET``/``HEAD`` and only ``200``/``203``/``404`` are stored.
* Responses carrying ``Set-Cookie`` are never stored, whatever the route says.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import orjson
from redis.asyncio.client import Redis
from routes import Route

from knowledgeos_core import get_logger
from knowledgeos_core.metrics import cache_operations_total
from knowledgeos_core.security import Principal
from settings import Settings

logger = get_logger(__name__)

CACHE_PREFIX = "kos:gateway:cache"

#: Status codes worth storing. 404 is included deliberately — a crawler hammering
#: missing slugs otherwise reaches the books service on every request.
CACHEABLE_STATUS = frozenset({200, 203, 404})

#: Never stored, regardless of route configuration.
_NEVER_CACHE_HEADERS = frozenset({"set-cookie", "www-authenticate"})


@dataclass(slots=True)
class CachedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def to_blob(self) -> bytes:
        return orjson.dumps(
            {"s": self.status_code, "h": self.headers, "b": self.body.decode("latin-1")}
        )

    @classmethod
    def from_blob(cls, blob: bytes) -> CachedResponse | None:
        try:
            data: dict[str, Any] = orjson.loads(blob)
            return cls(
                status_code=int(data["s"]),
                headers=dict(data["h"]),
                # latin-1 round-trips arbitrary bytes through str losslessly.
                body=data["b"].encode("latin-1"),
            )
        except Exception:
            return None


def auth_scope(route: Route, principal: Principal | None) -> str | None:
    """The auth dimension of the cache key, or ``None`` when caching is unsafe.

    Returning ``None`` means "do not cache this request at all".
    """
    if principal is None:
        return "anon"
    if route.cache_vary_on_user:
        return f"u:{principal.user_id}"
    # An authenticated caller on a route that does not vary by user: the response
    # may still be personalised in ways the route author did not anticipate
    # (an "owned" flag, a draft the author can see). Refuse rather than guess.
    return None


def build_key(
    *, method: str, path: str, query: str, scope: str, accept_encoding_relevant: bool = False
) -> str:
    """Deterministic, collision-resistant cache key.

    The query string is hashed rather than embedded so a long filter chain cannot
    produce an unbounded key, and so key length stays constant.
    """
    material = f"{method}\n{path}\n{query}\n{scope}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:40]
    return f"{CACHE_PREFIX}:{digest}"


class ResponseCache:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._service = settings.service_name

    def _record(self, result: str) -> None:
        cache_operations_total.labels(
            service=self._service, cache="gateway_response", result=result
        ).inc()

    async def get(self, key: str) -> CachedResponse | None:
        if not self._settings.cache_enabled:
            return None
        try:
            blob = await self._redis.get(key)
        except Exception as exc:
            # Fail open: a Redis outage must degrade to "slower", not "broken".
            logger.warning("gateway.cache_read_failed", error=str(exc))
            self._record("error")
            return None

        if blob is None:
            self._record("miss")
            return None

        cached = CachedResponse.from_blob(blob)
        if cached is None:
            self._record("error")
            await self._redis.delete(key)
            return None

        self._record("hit")
        return cached

    async def store(
        self,
        key: str,
        *,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        ttl: int,
    ) -> None:
        if not self._settings.cache_enabled or ttl <= 0:
            return
        if status_code not in CACHEABLE_STATUS:
            return
        if len(body) > self._settings.cache_max_body_bytes:
            return
        # A response that sets a cookie is by definition specific to one client.
        if any(name.lower() in _NEVER_CACHE_HEADERS for name in headers):
            return
        # Honour an upstream that explicitly opts out.
        cache_control = headers.get("cache-control", headers.get("Cache-Control", "")).lower()
        if "no-store" in cache_control or "private" in cache_control:
            return

        stored_headers = {
            name: value
            for name, value in headers.items()
            # Recomputed per response; storing them would pin a stale request id
            # onto every future cache hit.
            if name.lower() not in {"x-request-id", "x-trace-id", "x-cache", "date"}
        }
        try:
            await self._redis.set(
                key,
                CachedResponse(
                    status_code=status_code, headers=stored_headers, body=body
                ).to_blob(),
                ex=ttl,
            )
        except Exception as exc:
            logger.warning("gateway.cache_write_failed", error=str(exc))

    async def invalidate_all(self) -> int:
        """Drop every cached response.

        Used when an invalidating event arrives. Deliberately coarse: the alternative
        is tracking which keys a book id appears in, and a stale catalogue page is a
        worse bug than a brief drop in hit rate. Uses SCAN, never ``KEYS``.
        """
        deleted = 0
        cursor = 0
        try:
            while True:
                cursor, keys = await self._redis.scan(
                    cursor=cursor, match=f"{CACHE_PREFIX}:*", count=500
                )
                if keys:
                    deleted += int(await self._redis.delete(*keys))
                if cursor == 0:
                    break
        except Exception as exc:
            logger.warning("gateway.cache_invalidate_failed", error=str(exc))
        if deleted:
            logger.info("gateway.cache_invalidated", keys=deleted)
        return deleted

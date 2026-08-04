"""Trending queries: Redis sorted sets with time decay.

**Why buckets rather than one sorted set.** The obvious implementation is a single
ZSET and ``ZINCRBY key weight member`` where ``weight`` grows exponentially with
time — the classic Hacker News trick. It works, and then six months later the
exponent overflows a float and the whole ranking becomes ``inf``. Rescaling to
avoid that is a background job nobody writes.

Hourly buckets have none of that. Each bucket is a plain count with a TTL, decay
is applied at read time as a weight per bucket age, and the data expires itself.
Reading merges the last N buckets in Python — a few hundred members each, so the
merge is trivial and there is no temp key to leak.

Failure behaviour: **every method here swallows Redis errors.** Trending is a
garnish. A Redis outage must not turn a working search into a 500.
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio.client import Redis

from knowledgeos_core import get_logger
from settings import Settings

logger = get_logger(__name__)

KEY_PREFIX = "kos:search:trending"


def bucket_key(moment: datetime) -> str:
    return f"{KEY_PREFIX}:{moment.strftime('%Y%m%d%H')}"


class TrendingService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def record(self, query: str) -> None:
        """Count one search against the current hour's bucket."""
        if not self._settings.trending_enabled:
            return
        term = query.strip().lower()
        if len(term) < self._settings.trending_min_query_length:
            return
        now = datetime.now(UTC)
        key = bucket_key(now)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.zincrby(key, 1.0, term)
                # TTL slightly beyond the read window so the oldest bucket the
                # reader wants is still there when it asks.
                pipe.expire(key, (self._settings.trending_window_hours + 2) * 3600)
                await pipe.execute()
        except Exception as exc:
            logger.debug("search.trending_record_failed", error=str(exc))

    async def top(self, limit: int | None = None) -> list[tuple[str, float]]:
        """Merged, decayed ranking across the window.

        Weight is ``decay ** hours_ago``, so a query from this hour counts fully
        and one from 24 hours ago counts for ``0.9 ** 24`` — about 8%. That is
        what makes this *trending* rather than *all-time popular*.
        """
        if not self._settings.trending_enabled:
            return []
        limit = limit or self._settings.trending_max_terms
        now = datetime.now(UTC)
        scores: dict[str, float] = {}

        try:
            for hours_ago in range(self._settings.trending_window_hours):
                moment = datetime.fromtimestamp(now.timestamp() - hours_ago * 3600, tz=UTC)
                weight = self._settings.trending_decay_per_hour**hours_ago
                if weight < 0.01:
                    # Contributes less than a rounding error; stop walking.
                    break
                # Only the head of each bucket: a long tail of one-off queries
                # cannot reach the top of the merged ranking anyway.
                entries = await self._redis.zrevrange(
                    bucket_key(moment), 0, limit * 5, withscores=True
                )
                for member, count in entries:
                    term = member.decode() if isinstance(member, bytes) else str(member)
                    scores[term] = scores.get(term, 0.0) + float(count) * weight
        except Exception as exc:
            logger.warning("search.trending_read_failed", error=str(exc))
            return []

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [(term, round(score, 4)) for term, score in ranked[:limit]]

    async def reset(self) -> None:
        """Clear every bucket. Used by tests and by an operator after a spam run."""
        try:
            cursor = 0
            while True:
                cursor, keys = await self._redis.scan(
                    cursor=cursor, match=f"{KEY_PREFIX}:*", count=200
                )
                if keys:
                    await self._redis.delete(*keys)
                if cursor == 0:
                    return
        except Exception as exc:
            logger.debug("search.trending_reset_failed", error=str(exc))

"""Redis read-through cache for the two hottest catalogue reads.

Only two things are cached, because only two things are read far more often than
they are written: the book detail page (keyed by slug *and* by id, because the
frontend renders from the slug and internal callers hold the id) and the category
tree (rebuilt from a flat SELECT, read on every page render, written maybe weekly).

Invalidation is explicit on write rather than TTL-only. A TTL alone means an editor
fixes a typo and then watches the old title for five minutes, which is how people
stop trusting the admin panel.
"""

from __future__ import annotations

import uuid
from typing import Any

from knowledgeos_core import get_logger
from knowledgeos_core.redis import RedisClient
from settings import Settings

logger = get_logger(__name__)

#: Namespace for every key this module writes. ``RedisClient`` already prefixes
#: with ``kos:<service>:cache:``.
BOOK_SLUG_KEY = "book:slug:{slug}"
BOOK_ID_KEY = "book:id:{book_id}"
CATEGORY_TREE_KEY = "categories:tree"


class CatalogueCache:
    """Thin, failure-tolerant wrapper.

    Every method degrades to "no cache" when Redis is unavailable or disabled. A
    cache outage must slow the catalogue down, never take it offline.
    """

    def __init__(self, redis: RedisClient | None, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._redis is not None and self._settings.cache_enabled

    async def _get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        try:
            return await self._redis.get_json(key)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("cache.read_failed", cache_key=key, error=str(exc))
            return None

    async def _set(self, key: str, value: Any, ttl: int) -> None:
        if not self.enabled:
            return
        try:
            await self._redis.set_json(key, value, ttl=ttl)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("cache.write_failed", cache_key=key, error=str(exc))

    async def _drop(self, *keys: str) -> None:
        if not self.enabled or not keys:
            return
        try:
            await self._redis.delete(*keys)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("cache.invalidate_failed", error=str(exc))

    # ---- book detail ----------------------------------------------------

    async def get_book(self, *, slug: str | None = None, book_id: uuid.UUID | None = None) -> Any:
        if slug is not None:
            return await self._get(BOOK_SLUG_KEY.format(slug=slug))
        if book_id is not None:
            return await self._get(BOOK_ID_KEY.format(book_id=book_id))
        return None

    async def set_book(self, *, slug: str, book_id: uuid.UUID, payload: Any) -> None:
        ttl = self._settings.book_cache_ttl
        await self._set(BOOK_SLUG_KEY.format(slug=slug), payload, ttl)
        await self._set(BOOK_ID_KEY.format(book_id=book_id), payload, ttl)

    async def invalidate_book(self, *, book_id: uuid.UUID, slugs: tuple[str, ...] = ()) -> None:
        """Drop a book's entries.

        ``slugs`` takes the *old* slug as well as the new one on a rename: leaving
        the old key behind would keep serving the previous document to anyone who
        still holds that URL.
        """
        keys = [BOOK_ID_KEY.format(book_id=book_id)]
        keys += [BOOK_SLUG_KEY.format(slug=slug) for slug in slugs if slug]
        await self._drop(*keys)

    # ---- category tree --------------------------------------------------

    async def get_category_tree(self) -> Any:
        return await self._get(CATEGORY_TREE_KEY)

    async def set_category_tree(self, payload: Any) -> None:
        await self._set(CATEGORY_TREE_KEY, payload, self._settings.category_tree_cache_ttl)

    async def invalidate_category_tree(self) -> None:
        await self._drop(CATEGORY_TREE_KEY)

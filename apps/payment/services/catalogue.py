"""Reading the catalogue from the payment service.

The payment service does not own book prices and never queries the books schema
directly. It asks the books service over a signed internal call. That boundary is
the whole reason a price cannot be spoofed: whatever the client puts in the cart,
the amount charged comes from the catalogue's own row.

Prices are cached for a few seconds. A price change should reach checkout quickly,
but a checkout page that re-prices on every keystroke should not turn into a
thundering herd against the catalogue.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import orjson

from knowledgeos_core import BadRequestError, NotFoundError, get_logger
from knowledgeos_core.http import ServiceRegistry
from knowledgeos_core.redis import RedisClient

logger = get_logger(__name__)

#: Short enough that a price correction is live almost immediately, long enough to
#: absorb a coupon field being retyped.
PRICE_CACHE_TTL = 30


@dataclass(frozen=True, slots=True)
class CatalogueBook:
    """The slice of a book that pricing cares about."""

    id: uuid.UUID
    slug: str
    title: str
    price_minor: int
    currency: str
    status: str
    category_ids: tuple[uuid.UUID, ...] = ()

    @property
    def is_purchasable(self) -> bool:
        """Only a published book may be sold.

        A draft in a cart is not a validation nicety: it is someone buying a file
        that has not cleared review, and there is no clean way to unwind that.
        """
        return self.status == "published"


def _parse(item: dict[str, Any]) -> CatalogueBook:
    # effective_price_minor is the catalogue's own answer to "what does this cost
    # right now", already accounting for a discount price. Using price_minor here
    # would quietly ignore every sale the merchandising team ran.
    price = item.get("effective_price_minor")
    if price is None:
        price = item.get("price_minor", 0)
    return CatalogueBook(
        id=uuid.UUID(str(item["id"])),
        slug=str(item.get("slug", "")),
        title=str(item.get("title", "")),
        price_minor=int(price),
        currency=str(item.get("currency", "INR")),
        status=str(item.get("status", "")),
        category_ids=tuple(
            uuid.UUID(str(category["id"]))
            for category in item.get("categories", [])
            if category.get("id")
        ),
    )


class CatalogueClient:
    def __init__(self, services: ServiceRegistry | None, redis: RedisClient | None) -> None:
        self._services = services
        self._redis = redis

    # ---- cache ----------------------------------------------------------

    @staticmethod
    def _key(book_id: uuid.UUID) -> str:
        return f"kos:payment:book:{book_id}"

    async def _cached(self, book_ids: list[uuid.UUID]) -> dict[uuid.UUID, CatalogueBook]:
        if self._redis is None or not book_ids:
            return {}
        try:
            raw = await self._redis.client.mget([self._key(b) for b in book_ids])
        except Exception as exc:
            # A cache outage must not stop people buying things.
            logger.warning("payment.price_cache_read_failed", error=str(exc))
            return {}
        found: dict[uuid.UUID, CatalogueBook] = {}
        for book_id, value in zip(book_ids, raw, strict=True):
            if not value:
                continue
            try:
                found[book_id] = _parse(orjson.loads(value))
            except Exception as exc:
                # A poisoned entry falls through to a fresh fetch rather than
                # failing the checkout. Logged, because a cache that keeps
                # producing unparseable rows is a bug and not a cache miss.
                logger.warning(
                    "payment.price_cache_entry_invalid", book_id=str(book_id), error=str(exc)
                )
        return found

    async def _store(self, books: list[CatalogueBook]) -> None:
        if self._redis is None or not books:
            return
        try:
            pipe = self._redis.client.pipeline()
            for book in books:
                pipe.set(
                    self._key(book.id),
                    orjson.dumps(
                        {
                            "id": str(book.id),
                            "slug": book.slug,
                            "title": book.title,
                            "price_minor": book.price_minor,
                            "effective_price_minor": book.price_minor,
                            "currency": book.currency,
                            "status": book.status,
                            "categories": [{"id": str(c)} for c in book.category_ids],
                        }
                    ),
                    ex=PRICE_CACHE_TTL,
                )
            await pipe.execute()
        except Exception as exc:
            logger.warning("payment.price_cache_write_failed", error=str(exc))

    # ---- lookups --------------------------------------------------------

    async def fetch(self, book_ids: list[uuid.UUID]) -> dict[uuid.UUID, CatalogueBook]:
        """Resolve every requested book, or raise.

        A partial result is never returned. Pricing a cart against a book we could
        not read would mean either dropping a line the customer chose or charging
        for something we cannot describe on the invoice.
        """
        if not book_ids:
            return {}
        unique = list(dict.fromkeys(book_ids))
        resolved = await self._cached(unique)
        missing = [book_id for book_id in unique if book_id not in resolved]
        if not missing:
            return resolved

        if self._services is None:
            raise NotFoundError(
                "The catalogue is unavailable, so these books cannot be priced.",
                details={"book_ids": [str(b) for b in missing]},
            )

        payload = await self._services.get("books").post_json(
            "/internal/books/batch",
            {"book_ids": [str(b) for b in missing], "include_unpublished": True},
        )
        fetched = [_parse(item) for item in (payload or {}).get("items", [])]
        await self._store(fetched)
        resolved.update({book.id: book for book in fetched})

        still_missing = [book_id for book_id in unique if book_id not in resolved]
        if still_missing:
            raise NotFoundError(
                "One or more books in this order no longer exist.",
                details={"book_ids": [str(b) for b in still_missing]},
            )
        return resolved

    async def require_purchasable(
        self, book_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, CatalogueBook]:
        """Fetch, and reject anything not on sale."""
        books = await self.fetch(book_ids)
        unavailable = [book for book in books.values() if not book.is_purchasable]
        if unavailable:
            raise BadRequestError(
                "One or more books in this order are not available for purchase.",
                code="book_not_purchasable",
                details={"book_ids": [str(book.id) for book in unavailable]},
            )
        return books

    async def owned_book_ids(self, user_id: uuid.UUID, book_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these the buyer already holds.

        Best-effort by design: if the books service cannot answer, checkout proceeds
        rather than failing. The consequence of a false negative is a duplicate
        purchase we can refund; the consequence of failing closed is that nobody can
        buy anything while one service is degraded.
        """
        if self._services is None or not book_ids:
            return set()
        try:
            payload = await self._services.get("books").post_json(
                "/internal/entitlements/check",
                {"user_id": str(user_id), "book_ids": [str(b) for b in book_ids]},
            )
        except Exception as exc:
            logger.warning("payment.ownership_check_failed", error=str(exc))
            return set()
        return {uuid.UUID(str(b)) for b in (payload or {}).get("owned_book_ids", [])}

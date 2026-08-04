"""Reading the source of truth: the books service's ``/internal/*`` API.

The index is a cache. This module is the only path back to what it is a cache
*of*, and it is used in two places: when an event tells us a book changed (we get
an id, not a document), and when reconciliation walks the whole catalogue.

**Contract with the books service** — these endpoints are what this service
depends on, and the pagination shape is what makes a full reconciliation possible
without an ``OFFSET 40000``:

``GET /internal/books/{book_id}``
    One book, in the same shape as the public detail response, regardless of
    status. Returns 404 when the book does not exist or was hard-deleted.

``GET /internal/books?status=published&limit=200&cursor=<opaque>``
    Keyset page. Responds ``{"items": [...], "next_cursor": "..." | null}``.

``GET /internal/authors`` and ``GET /internal/categories``
    Same page shape.

Every call is HMAC-signed and circuit-broken by ``ServiceClient``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from knowledgeos_core import UpstreamError, get_logger
from knowledgeos_core.http import ServiceRegistry

logger = get_logger(__name__)


class CatalogueGateway:
    """Typed access to the books service, with the paging loop written once."""

    def __init__(self, services: ServiceRegistry, *, page_size: int = 200) -> None:
        self._services = services
        self._page_size = page_size

    @property
    def _client(self) -> Any:
        return self._services.get("books")

    async def get_book(self, book_id: str) -> dict[str, Any] | None:
        """One book, or ``None`` when it is gone.

        A 404 is not an error here — it is the answer. A book deleted between the
        event being published and this call is exactly the race the reconciler
        exists to resolve, and it must not dead-letter the event.
        """
        try:
            data = await self._client.get_json(f"/internal/books/{book_id}")
        except UpstreamError as exc:
            if exc.status_code == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    async def iter_books(
        self, *, status: str = "published", max_documents: int | None = None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Walk the catalogue a page at a time.

        Yields pages rather than documents so the caller can batch its writes to
        match; the whole point of the cursor is that memory stays flat no matter
        how large the catalogue gets.
        """
        async for page in self._iter_pages(
            "/internal/books", {"status": status}, max_documents=max_documents
        ):
            yield page

    async def iter_authors(
        self, *, max_documents: int | None = None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        async for page in self._iter_pages("/internal/authors", {}, max_documents=max_documents):
            yield page

    async def iter_categories(
        self, *, max_documents: int | None = None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        async for page in self._iter_pages("/internal/categories", {}, max_documents=max_documents):
            yield page

    async def _iter_pages(
        self,
        path: str,
        params: dict[str, Any],
        *,
        max_documents: int | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        cursor: str | None = None
        seen = 0
        while True:
            query = {**params, "limit": self._page_size}
            if cursor:
                query["cursor"] = cursor
            payload = await self._client.get_json(path, params=query)
            items = list((payload or {}).get("items", []))
            if not items:
                return
            if max_documents is not None and seen + len(items) > max_documents:
                items = items[: max_documents - seen]
            seen += len(items)
            yield items
            cursor = (payload or {}).get("next_cursor")
            if not cursor or (max_documents is not None and seen >= max_documents):
                if max_documents is not None and seen >= max_documents:
                    logger.warning(
                        "search.reconcile_truncated", path=path, max_documents=max_documents
                    )
                return

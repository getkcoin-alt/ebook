"""Calls out to the services the pipeline needs: books, ai, search.

A thin, typed wrapper over the shared circuit-broken client — thin on purpose, so
that this file is a readable inventory of exactly which other services this one
depends on and what it asks them for. Every entry here is coupling, and coupling that
is spread across ten call sites is coupling nobody can count.

Two conventions:

**Nothing here raises for an optional dependency.** A search index that is down must
not stop a book being published; the reconciler will pick it up. Methods that back an
optional stage return a status instead of raising, and the stage decides.

**Nothing here retries.** The client already has a circuit breaker, and the pipeline
already has stage-level retry with backoff. A third retry layer in the middle
multiplies with both and turns one provider blip into an eight-minute stall.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from knowledgeos_core import get_logger
from knowledgeos_core.http import ServiceRegistry
from settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class AiOutput:
    """What the enrichment stages got back. Every field optional — a partial result
    is still worth writing, and an empty one is not a failure."""

    description: str | None = None
    summary: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    #: Kinds the AI service could not produce. Recorded on the stage so the reason a
    #: listing has no tags is visible without reading logs.
    failed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any((self.description, self.summary, self.meta_title, self.keywords, self.tags))


class PipelineClients:
    def __init__(self, settings: Settings, services: ServiceRegistry | None) -> None:
        self._settings = settings
        self._services = services

    @property
    def available(self) -> bool:
        return self._services is not None

    # ---- ai --------------------------------------------------------------

    async def generate(
        self,
        *,
        kinds: list[str],
        context: dict[str, Any],
        book_id: uuid.UUID,
        force_refresh: bool = False,
    ) -> AiOutput | None:
        """One batch call for every enrichment field.

        One round trip rather than three: the AI service already runs the kinds
        sequentially behind its own budget check, and three separate calls would each
        pass that check before any of them recorded a cost.

        Returns None when AI is unavailable — the caller skips rather than fails.
        """
        if self._services is None or not self._settings.ai_enabled:
            return None

        try:
            payload = await self._services.get("ai").post_json(
                "/internal/generate/batch",
                {
                    "book_id": str(book_id),
                    "kinds": kinds,
                    "context": context,
                    "force_refresh": force_refresh,
                },
            )
        except Exception as exc:
            logger.warning("automation.ai_batch_failed", book_id=str(book_id), error=str(exc))
            return None

        return _parse_ai_batch(payload or {})

    # ---- books -----------------------------------------------------------

    async def get_book(self, book_id: uuid.UUID) -> dict | None:
        if self._services is None:
            return None
        try:
            return await self._services.get("books").get_json(f"/internal/books/{book_id}")
        except Exception as exc:
            logger.warning("automation.book_fetch_failed", book_id=str(book_id), error=str(exc))
            return None

    async def create_book(self, payload: dict[str, Any]) -> dict:
        """Create a catalogue row for an imported book.

        Raises on rejection, and the import records that against the row. Swallowing
        it would report an import as successful while producing no books, which is
        the failure mode an import must never have.
        """
        if self._services is None:
            raise RuntimeError("Service discovery is not configured.")
        created = await self._services.get("books").post_json("/internal/books", payload)
        if not created or "id" not in created:
            raise RuntimeError("The catalogue did not return a book id.")
        return created

    async def update_book(self, book_id: uuid.UUID, fields: dict[str, Any]) -> bool:
        """Write pipeline output back to the catalogue.

        Raises on failure, unlike the optional calls here: this is how the work
        reaches the thing the user sees. A pipeline that "succeeded" without writing
        its results back has produced nothing.
        """
        if self._services is None:
            raise RuntimeError("Service discovery is not configured.")
        await self._services.get("books").request(
            "PATCH", f"/internal/books/{book_id}", json=fields
        )
        return True

    async def publish_book(self, book_id: uuid.UUID) -> bool:
        if self._services is None:
            raise RuntimeError("Service discovery is not configured.")
        await self._services.get("books").post_json(
            f"/internal/books/{book_id}/publish", {"reason": "automation"}
        )
        return True

    # ---- search ----------------------------------------------------------

    async def index_book(self, book_id: uuid.UUID) -> bool:
        """Ask search to index the book now.

        Best effort by design. `book.published` already triggers indexing through the
        event bus and the search service runs a reconciler over the catalogue, so a
        failure here costs freshness for minutes, not correctness. Blocking a
        publication on the search index being up would make search a hard dependency
        of the catalogue, which is exactly the coupling the event bus exists to avoid.
        """
        if self._services is None or not self._settings.search_indexing_enabled:
            return False
        try:
            await self._services.get("search").post_json(
                "/internal/index/books", {"book_ids": [str(book_id)]}
            )
            return True
        except Exception as exc:
            logger.info("automation.index_failed", book_id=str(book_id), error=str(exc))
            return False


def _parse_ai_batch(payload: dict) -> AiOutput:
    """Map the AI service's batch response onto the fields the catalogue stores.

    Written against the actual response shape: `results` is keyed by task kind, each
    entry carrying `content` for prose tasks and `data` for structured ones.
    """
    results = payload.get("results") or {}
    output = AiOutput(
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        failed=[str(kind) for kind in (payload.get("failed") or [])],
    )

    if description := results.get("description"):
        output.description = (description.get("content") or "").strip() or None
    if summary := results.get("summary"):
        output.summary = (summary.get("content") or "").strip() or None

    seo = (results.get("seo") or {}).get("data") or {}
    output.meta_title = _trim(seo.get("meta_title"), 255)
    output.meta_description = _trim(seo.get("meta_description"), 500)
    output.keywords = _string_list(seo.get("keywords"), limit=15)

    tags = (results.get("tags") or {}).get("data") or {}
    output.tags = _string_list(tags.get("tags"), limit=10)

    return output


def _trim(value: Any, limit: int) -> str | None:
    """Truncate to the catalogue's column width.

    A model told "at most 60 characters" produces 63 often enough to matter, and a
    422 from the books service would throw away a whole batch of usable output over
    three characters.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] or None


def _string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip().lower()[:60]
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
        if len(seen) >= limit:
            break
    return seen

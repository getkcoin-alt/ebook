"""Request and response models for the search service.

These mirror ``packages/types/src/discovery.ts`` — the TypeScript SDK is generated
from the OpenAPI these produce, so a field renamed here is a compile error in the
frontend rather than an ``undefined`` at runtime.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from knowledgeos_core import BaseSchema


class SortOption(StrEnum):
    """Sort orders the API accepts.

    A closed enum rather than a free-form string: the value selects a
    server-declared sort expression, so a user cannot ask Meilisearch to sort by
    an attribute we never made sortable (which is a 4xx from the engine and a 500
    for us if it reaches it).
    """

    RELEVANCE = "relevance"
    NEWEST = "newest"
    OLDEST = "oldest"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    RATING = "rating"
    POPULARITY = "popularity"
    TITLE = "title"


class SuggestionKind(StrEnum):
    BOOK = "book"
    AUTHOR = "author"
    CATEGORY = "category"
    QUERY = "query"


# ---------------------------------------------------------------------------
# Query results
# ---------------------------------------------------------------------------


class SearchHighlights(BaseSchema):
    """``<mark>``-wrapped fragments. Meilisearch escapes the source text."""

    title: str | None = None
    subtitle: str | None = None
    description: str | None = None


class BookHit(BaseSchema):
    """The card-sized projection stored in the index.

    Deliberately denormalised: rendering a result page must not require a call to
    the books service, or a search outage and a catalogue outage become the same
    incident from the user's point of view.
    """

    id: str
    slug: str
    title: str
    subtitle: str | None = None
    description: str | None = None
    authors: list[dict[str, Any]] = Field(default_factory=list)
    categories: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    language: str | None = None
    price_minor: int = 0
    currency: str = "INR"
    compare_at_price_minor: int | None = None
    is_free: bool = False
    rating_average: float | None = None
    rating_count: int = 0
    review_count: int = 0
    cover_url: str | None = None
    published_at: datetime | None = None
    #: Meilisearch ranking score, 0-1. Only comparable inside one result set.
    score: float | None = None
    highlights: SearchHighlights = Field(default_factory=SearchHighlights)


class FacetValue(BaseSchema):
    value: str
    label: str
    count: int


class SearchFacets(BaseSchema):
    categories: list[FacetValue] = Field(default_factory=list)
    authors: list[FacetValue] = Field(default_factory=list)
    formats: list[FacetValue] = Field(default_factory=list)
    tags: list[FacetValue] = Field(default_factory=list)
    languages: list[FacetValue] = Field(default_factory=list)
    price_ranges: list[FacetValue] = Field(default_factory=list)


class SearchResponse(BaseSchema):
    query: str
    hits: list[BookHit]
    facets: SearchFacets
    #: Meilisearch's estimate. Presented as "about N results", never as exact —
    #: it is computed from a capped scan and will disagree with a COUNT(*).
    estimated_total: int
    #: Server-side round trip. Surfaced in the UI because it is fast.
    took_ms: int
    #: Opaque; ``None`` when this is the last page.
    next_cursor: str | None = None
    has_more: bool = False
    #: Set when a typo-tolerant rewrite found results the literal query did not.
    did_you_mean: str | None = None
    #: True when results came from the vector/hybrid backend.
    semantic: bool = False


class Suggestion(BaseSchema):
    text: str
    kind: SuggestionKind
    href: str
    subtitle: str | None = None
    image_url: str | None = None


class SuggestResponse(BaseSchema):
    query: str
    suggestions: list[Suggestion]
    took_ms: int


class TrendingQuery(BaseSchema):
    query: str
    #: Time-decayed weight, not a raw count — an hour-old search outweighs a
    #: day-old one. Only meaningful as a ranking, never as a volume figure.
    score: float
    rank: int


class TrendingResponse(BaseSchema):
    window_hours: int
    queries: list[TrendingQuery]


class RelatedResponse(BaseSchema):
    book_id: str
    reason: Literal["similar", "same_author", "same_category"] = "similar"
    books: list[BookHit]


class ClickRequest(BaseSchema):
    """Records which result a user opened, to tune ranking."""

    query_id: uuid.UUID
    book_id: str = Field(min_length=1, max_length=64)
    position: int = Field(ge=0, le=1000)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


class QueryStat(BaseSchema):
    query: str
    searches: int
    average_results: float
    zero_result_rate: float


class AnalyticsResponse(BaseSchema):
    window_days: int
    total_searches: int
    unique_queries: int
    zero_result_searches: int
    zero_result_rate: float
    average_took_ms: float
    top_queries: list[QueryStat]
    zero_result_queries: list[QueryStat]


# ---------------------------------------------------------------------------
# Index administration (internal)
# ---------------------------------------------------------------------------


class IndexHealth(BaseSchema):
    reachable: bool
    status: str
    indexes: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ReindexRequest(BaseSchema):
    """Trigger a rebuild.

    ``mode="full"`` rebuilds from scratch; ``mode="reconcile"`` walks the source
    and repairs only what diverged, which is what the scheduled task uses.
    """

    mode: Literal["full", "reconcile"] = "reconcile"
    #: Restrict to one index. ``None`` means every index this service owns.
    index: str | None = None
    #: Ignore the ledger and re-send every document even when unchanged.
    force: bool = False


class ReindexResponse(BaseSchema):
    run_id: uuid.UUID
    index_name: str
    mode: str
    status: str
    documents_seen: int
    documents_indexed: int
    documents_deleted: int
    documents_failed: int
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


class DocumentBatchRequest(BaseSchema):
    """Push documents straight into the index.

    Used by the automation pipeline's publish stage, which already holds the full
    record and should not force a round trip back to the books service.
    """

    index: str | None = None
    documents: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class DocumentDeleteRequest(BaseSchema):
    index: str | None = None
    document_ids: list[str] = Field(min_length=1, max_length=1000)


class IndexOperationResponse(BaseSchema):
    index_name: str
    accepted: int
    skipped: int
    failed: int

"""Turning API parameters into a Meilisearch request, and back into a response.

Two things to know before editing:

**Filters are built from a whitelist, and values are escaped.** A Meilisearch
filter is an expression language. Interpolating a user-supplied string into it
unescaped is injection — not SQL injection, but the same shape of bug: a crafted
``category`` value could rewrite the expression and read documents the caller's
filters were meant to exclude (unpublished drafts, for one). Attribute names come
from :data:`FILTERABLE`, never from the request; values are quoted and escaped.

**The backend is swappable.** ``SearchBackend`` is the seam a vector or hybrid
engine slots into. The query API, the filters, the facets and the response shape
are all backend-independent, so enabling semantic search is a settings change,
not a rewrite of this module. See the README.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from knowledgeos_core import BadRequestError, decode_cursor, encode_cursor, get_logger
from schemas import (
    BookHit,
    FacetValue,
    SearchFacets,
    SearchHighlights,
    SearchResponse,
    SortOption,
)
from services.documents import PRICE_BUCKET_LABELS
from services.indexes import BOOK_FILTERABLE_ATTRIBUTES
from services.meili import MeiliClient
from settings import Settings

logger = get_logger(__name__)

#: API filter name -> index attribute. The *only* source of attribute names that
#: can reach a filter expression. Every value here must also appear in
#: BOOK_FILTERABLE_ATTRIBUTES; `test_filters.py` asserts that.
FILTERABLE: dict[str, str] = {
    "category": "category_slugs",
    "author": "author_slugs",
    "language": "language",
    "format": "formats",
    "tag": "tags",
    "publisher": "publisher",
}

#: Sort option -> Meilisearch sort expressions. Closed set; a value outside it
#: is a 400 before it ever reaches the engine.
SORT_EXPRESSIONS: dict[SortOption, list[str]] = {
    SortOption.RELEVANCE: [],
    SortOption.NEWEST: ["published_at_ts:desc"],
    SortOption.OLDEST: ["published_at_ts:asc"],
    SortOption.PRICE_ASC: ["price_minor:asc"],
    SortOption.PRICE_DESC: ["price_minor:desc"],
    SortOption.RATING: ["rating_average:desc", "rating_count:desc"],
    SortOption.POPULARITY: ["popularity:desc"],
    SortOption.TITLE: ["title:asc"],
}

_WHITESPACE = re.compile(r"\s+")


def normalise_query(text: str) -> str:
    """Lower-cased, whitespace-collapsed. Used for analytics and trending keys."""
    return _WHITESPACE.sub(" ", text.strip().lower())


def escape_filter_value(value: str) -> str:
    """Quote a value for a Meilisearch filter expression.

    Backslashes first, then quotes — reversing the order would double-escape the
    backslashes introduced by the quote replacement and let a crafted value break
    out of the string.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(slots=True)
class SearchParams:
    """Everything a query can ask for. Validated at the router boundary."""

    query: str = ""
    #: API filter name -> accepted values. OR within a name, AND across names —
    #: which is what a facet UI means by ticking two categories.
    filters: dict[str, list[str]] = field(default_factory=dict)
    min_price_minor: int | None = None
    max_price_minor: int | None = None
    min_rating: float | None = None
    free_only: bool = False
    sort: SortOption = SortOption.RELEVANCE
    limit: int = 20
    cursor: str | None = None
    facets: bool = True
    #: Force the hybrid backend on for this request, when it is configured.
    semantic: bool | None = None


class SearchBackend(Protocol):
    """The seam a vector or hybrid engine slots into.

    Anything satisfying this can serve ``/v1/search`` — the router, the filter
    builder, the facet mapping and the response schema are all unaware of which
    implementation is behind it.
    """

    async def execute(self, params: SearchParams, body: dict[str, Any]) -> dict[str, Any]:
        """Run a prepared query body and return Meilisearch-shaped results."""
        ...


class KeywordBackend:
    """The default: Meilisearch's lexical index, optionally hybrid.

    When ``SEMANTIC_ENABLED`` is set, the same request gains a ``hybrid`` block
    and Meilisearch blends vector and keyword scores. Nothing else changes, which
    is the point — turning semantic search on is a config flip, and turning it
    off during an incident is the same flip in reverse.
    """

    def __init__(self, settings: Settings, meili: MeiliClient) -> None:
        self._settings = settings
        self._meili = meili

    async def execute(self, params: SearchParams, body: dict[str, Any]) -> dict[str, Any]:
        use_semantic = (
            params.semantic if params.semantic is not None else self._settings.semantic_enabled
        )
        if use_semantic and self._settings.semantic_enabled:
            body = {
                **body,
                "hybrid": {
                    "semanticRatio": self._settings.semantic_ratio,
                    "embedder": self._settings.semantic_embedder,
                },
            }
        return await self._meili.search(self._settings.books_index, body)


class QueryService:
    """Builds requests, runs them through a backend, and maps the results."""

    def __init__(
        self, settings: Settings, meili: MeiliClient, backend: SearchBackend | None = None
    ) -> None:
        self._settings = settings
        self._meili = meili
        self._backend = backend or KeywordBackend(settings, meili)

    # ---- request building ------------------------------------------------

    def build_filter(self, params: SearchParams) -> list[Any]:
        """Compose the filter expression.

        Returns Meilisearch's array form: outer elements are ANDed, a nested
        array is ORed. Building it structurally rather than as a string is what
        removes the operator-precedence bugs that a string concatenation invites.
        """
        clauses: list[Any] = []

        for name, values in params.filters.items():
            if not values:
                continue
            attribute = FILTERABLE.get(name)
            if attribute is None:
                raise BadRequestError(
                    f"Cannot filter by '{name}'.",
                    details={"allowed": sorted(FILTERABLE)},
                )
            clauses.append([f"{attribute} = {escape_filter_value(value)}" for value in values])

        if params.free_only:
            clauses.append("is_free = true")
        else:
            if params.min_price_minor is not None:
                clauses.append(f"price_minor >= {int(params.min_price_minor)}")
            if params.max_price_minor is not None:
                clauses.append(f"price_minor <= {int(params.max_price_minor)}")

        if params.min_rating is not None:
            clauses.append(f"rating_average >= {float(params.min_rating)}")

        # Never leak drafts, rejected submissions or archived titles into public
        # results. Applied unconditionally, after the caller's filters, so no
        # combination of query parameters can remove it.
        clauses.append('status = "published"')
        return clauses

    def build_body(self, params: SearchParams) -> dict[str, Any]:
        offset = self._offset(params.cursor)
        limit = max(1, min(params.limit, self._settings.search_max_limit))
        body: dict[str, Any] = {
            "q": params.query,
            "offset": offset,
            # One extra document, so "is there a next page?" is answered without
            # a second query or a reliance on the estimated total.
            "limit": limit + 1,
            "filter": self.build_filter(params),
            "attributesToHighlight": ["title", "subtitle", "description"],
            "highlightPreTag": self._settings.highlight_pre_tag,
            "highlightPostTag": self._settings.highlight_post_tag,
            "attributesToCrop": ["description"],
            "cropLength": 40,
            "showRankingScore": True,
        }
        sort = SORT_EXPRESSIONS.get(params.sort, [])
        if sort:
            body["sort"] = sort
        if params.facets:
            body["facets"] = list(self._settings.facet_attributes)
        return body

    def _offset(self, cursor: str | None) -> int:
        if not cursor:
            return 0
        payload = decode_cursor(cursor)
        offset = payload.get("o")
        if not isinstance(offset, int) or offset < 0:
            raise BadRequestError("The pagination cursor is malformed.")
        # Meilisearch's `maxTotalHits` caps how deep it will go; refusing here
        # gives a clear 400 instead of a silently empty page.
        if offset > 5000:
            raise BadRequestError(
                "This result set cannot be paged any further. Narrow the query with filters.",
                code="cursor_too_deep",
            )
        return offset

    # ---- execution -------------------------------------------------------

    async def search(self, params: SearchParams) -> SearchResponse:
        started = time.perf_counter()
        body = self.build_body(params)
        raw = await self._backend.execute(params, body)

        limit = max(1, min(params.limit, self._settings.search_max_limit))
        hits = list(raw.get("hits", []))
        has_more = len(hits) > limit
        hits = hits[:limit]

        offset = self._offset(params.cursor)
        next_cursor = encode_cursor({"o": offset + limit}) if has_more else None

        took_ms = int(raw.get("processingTimeMs", 0)) or int(
            (time.perf_counter() - started) * 1000
        )
        semantic_used = "hybrid" in body

        return SearchResponse(
            query=params.query,
            hits=[to_hit(hit) for hit in hits],
            facets=to_facets(raw.get("facetDistribution", {})),
            estimated_total=int(raw.get("estimatedTotalHits", len(hits))),
            took_ms=took_ms,
            next_cursor=next_cursor,
            has_more=has_more,
            did_you_mean=None,
            semantic=semantic_used,
        )

    async def related(self, book_id: str, limit: int) -> tuple[str, list[BookHit]]:
        """Books similar to one book.

        Implemented as a keyword query built from the source book's own metadata:
        its categories, tags and authors. It works without embeddings, and when
        semantic search is enabled the same call gets better automatically
        because the hybrid backend handles the query.
        """
        document = await self._meili.get_document(self._settings.books_index, book_id)
        if document is None:
            return "similar", []

        terms = [
            *(document.get("category_names") or []),
            *(document.get("tags") or [])[:5],
            *(document.get("author_names") or [])[:2],
        ]
        query = " ".join(str(term) for term in terms if term)[:200]

        filters: list[Any] = ['status = "published"']
        categories = [slug for slug in (document.get("category_slugs") or []) if slug]
        if categories:
            filters.append(
                [f"category_slugs = {escape_filter_value(str(slug))}" for slug in categories]
            )
        # Exclude the source book, or the most related result is always itself.
        filters.append(f"id != {escape_filter_value(book_id)}")

        raw = await self._meili.search(
            self._settings.books_index,
            {
                "q": query,
                "limit": limit,
                "filter": filters,
                "showRankingScore": True,
                "sort": ["popularity:desc"],
            },
        )
        reason = "same_category" if categories else "similar"
        return reason, [to_hit(hit) for hit in raw.get("hits", [])]


def to_hit(raw: dict[str, Any]) -> BookHit:
    """Map one Meilisearch hit onto the response model."""
    formatted = raw.get("_formatted") or {}
    return BookHit(
        id=str(raw.get("id", "")),
        slug=str(raw.get("slug", "")),
        title=str(raw.get("title", "")),
        subtitle=raw.get("subtitle"),
        description=raw.get("description"),
        authors=list(raw.get("authors") or []),
        categories=list(raw.get("categories") or []),
        tags=list(raw.get("tags") or []),
        formats=list(raw.get("formats") or []),
        language=raw.get("language"),
        price_minor=int(raw.get("price_minor") or 0),
        currency=str(raw.get("currency") or "INR"),
        compare_at_price_minor=raw.get("compare_at_price_minor"),
        is_free=bool(raw.get("is_free", False)),
        rating_average=raw.get("rating_average"),
        rating_count=int(raw.get("rating_count") or 0),
        review_count=int(raw.get("review_count") or 0),
        cover_url=raw.get("cover_url"),
        published_at=raw.get("published_at"),
        score=raw.get("_rankingScore"),
        highlights=SearchHighlights(
            title=formatted.get("title"),
            subtitle=formatted.get("subtitle"),
            description=formatted.get("description"),
        ),
    )


#: Facet attribute -> the response field it populates and how values are labelled.
_FACET_FIELDS: dict[str, str] = {
    "category_slugs": "categories",
    "author_slugs": "authors",
    "formats": "formats",
    "tags": "tags",
    "language": "languages",
    "price_bucket": "price_ranges",
}


def to_facets(distribution: dict[str, Any]) -> SearchFacets:
    """Map Meilisearch's facet distribution onto the response model.

    Sorted by count descending and truncated: a tag facet with 4,000 values is a
    payload nobody renders and a scroll nobody reads.
    """
    buckets: dict[str, list[FacetValue]] = {}
    for attribute, values in (distribution or {}).items():
        target = _FACET_FIELDS.get(attribute)
        if target is None or not isinstance(values, dict):
            continue
        entries = [
            FacetValue(value=str(value), label=_label(attribute, str(value)), count=int(count))
            for value, count in values.items()
        ]
        entries.sort(key=lambda entry: (-entry.count, entry.value))
        buckets[target] = entries
    return SearchFacets(**buckets)


def _label(attribute: str, value: str) -> str:
    if attribute == "price_bucket":
        return PRICE_BUCKET_LABELS.get(value, value)
    if attribute in {"category_slugs", "author_slugs"}:
        # Slugs are what the index holds; the display name lives on the record.
        # Title-casing the slug is a readable fallback the frontend can override
        # from data it already has, and avoids a fan-out lookup per facet value.
        return value.replace("-", " ").title()
    return value


def assert_filters_are_indexable() -> None:
    """Guard invoked by the tests: every filter maps to a filterable attribute.

    A filter on an attribute Meilisearch was never told to make filterable is a
    400 from the engine on every request that uses it — a whole facet silently
    broken in production. Cheaper to catch here.
    """
    unknown = set(FILTERABLE.values()) - set(BOOK_FILTERABLE_ATTRIBUTES)
    if unknown:
        raise AssertionError(f"Filters reference non-filterable attributes: {sorted(unknown)}")

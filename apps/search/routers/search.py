"""The public search surface.

Every route here is anonymous-friendly. Search is how someone decides whether to
create an account, so requiring one first is backwards.

These endpoints are the platform's highest-frequency path — autocomplete fires on
every keystroke — which is why the timeouts are short, the result sizes are capped,
and analytics recording never blocks or fails a response.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from deps import (
    Analytics,
    DbSession,
    OptionalUserId,
    Queries,
    SearchQueryParams,
    SessionId,
    Trending,
)
from knowledgeos_core import get_logger
from knowledgeos_core.deps import Ctx, rate_limit
from schemas import (
    ClickRequest,
    RelatedResponse,
    SearchResponse,
    SuggestResponse,
    TrendingQuery,
    TrendingResponse,
)
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["search"])


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search the catalogue",
    description=(
        "Full-text search with facets, filters and cursor pagination.\n\n"
        "Filters are repeatable `name:value` parameters — same name ORs, different "
        "names AND, which is what a facet sidebar means. `status = published` is "
        "applied unconditionally, so no combination of parameters can surface a "
        "draft.\n\n"
        "`estimated_total` is exactly that: Meilisearch computes it from a capped "
        'scan, so present it as "about N results" and never as an exact count.'
    ),
    dependencies=[Depends(rate_limit("search"))],
)
async def search(
    params: SearchQueryParams,
    queries: Queries,
    trending: Trending,
    analytics: Analytics,
    ctx: Ctx,
    session_id: SessionId = None,
    user_id: OptionalUserId = None,
) -> SearchResponse:
    response = await queries.search(params)

    # Observation happens after the answer is built, and neither call can fail the
    # request: a search that returned good results must not 500 because a counter
    # was unavailable.
    if params.query and len(params.query) >= settings.trending_min_query_length:
        await trending.record(params.query)

    query_id = await analytics.record_query(
        ctx.database.sessionmaker if ctx.database else None,
        query=params.query,
        filters=params.filters,
        sort=str(params.sort),
        result_count=response.estimated_total,
        took_ms=response.took_ms,
        user_id=user_id,
        session_id=session_id,
    )
    # Handed back so a later click can be attributed to this search. Without the
    # pairing, click-through cannot be reconstructed afterwards.
    if query_id is not None:
        response.query_id = query_id
    return response


@router.get(
    "/search/suggest",
    response_model=SuggestResponse,
    summary="Autocomplete",
    description=(
        "Books, authors and categories in one round trip. Runs on every keystroke, "
        "so it has its own tighter timeout and a small result cap.\n\n"
        "Results are interleaved by kind rather than sorted by score: Meilisearch "
        "scores are only comparable within a single index, so a global sort would "
        "be confidently wrong."
    ),
    dependencies=[Depends(rate_limit("search"))],
)
async def suggest(
    queries: Queries,
    q: Annotated[str, Query(max_length=100, description="Partial query.")] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> SuggestResponse:
    return await queries.suggest(q, limit)


@router.get(
    "/search/trending",
    response_model=TrendingResponse,
    summary="Trending searches",
    description=(
        "Time-decayed over hourly buckets — a search from this hour outweighs one "
        "from yesterday. The score is a ranking weight, not a volume figure, and "
        "should never be displayed as a count."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def trending_searches(
    trending: Trending,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> TrendingResponse:
    entries = await trending.top(limit)
    return TrendingResponse(
        window_hours=settings.trending_window_hours,
        queries=[
            TrendingQuery(query=query, score=round(score, 3), rank=index + 1)
            for index, (query, score) in enumerate(entries)
        ],
    )


@router.get(
    "/search/related/{book_id}",
    response_model=RelatedResponse,
    summary="Books similar to one book",
    description=(
        "Built from the source book's own categories, tags and authors, so it works "
        "with no embeddings at all. When semantic search is enabled the same call "
        "gets better automatically, because the hybrid backend handles the query."
    ),
    dependencies=[Depends(rate_limit("search"))],
)
async def related(
    book_id: str,
    queries: Queries,
    limit: Annotated[int, Query(ge=1, le=50)] = 12,
) -> RelatedResponse:
    reason, books = await queries.related(book_id, limit)
    return RelatedResponse(book_id=book_id, reason=reason, books=books)  # type: ignore[arg-type]


@router.post(
    "/search/click",
    status_code=201,
    summary="Record which result was opened",
    description=(
        "Click-through is how the ranking rules get tuned. Position is recorded "
        "with it, because a click at position 9 says far more about relevance than "
        "one at position 1."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def record_click(
    payload: ClickRequest,
    session: DbSession,
    analytics: Analytics,
) -> dict[str, bool]:
    if not analytics.enabled:
        return {"recorded": False}
    await analytics.record_click(
        session,
        query_id=payload.query_id,
        book_id=payload.book_id,
        position=payload.position,
    )
    return {"recorded": True}

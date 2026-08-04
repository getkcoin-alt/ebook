"""Index administration and service-to-service endpoints.

Reindexing is the operation that matters here. A full rebuild empties the index
before refilling it, so it is genuinely disruptive: run it when the document shape
or the index settings change. A reconcile walks the source and repairs only what
diverged, which is cheap enough to run hourly and is what the scheduled task uses.

Both take a **distributed lock**. Two replicas rebuilding the same index at once
would each delete the other's freshly written documents, and the visible symptom is
a search index that empties itself for no apparent reason.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from deps import Analytics, DbSession, Indexing, Meili, Trending
from knowledgeos_core import (
    ConflictError,
    ListResponse,
    MessageResponse,
    NotFoundError,
    get_logger,
)
from knowledgeos_core.deps import Ctx, InternalCaller, require_permission
from models import ReindexRun
from schemas import (
    AnalyticsResponse,
    DocumentBatchRequest,
    DocumentDeleteRequest,
    IndexHealth,
    IndexOperationResponse,
    ReindexRequest,
    ReindexResponse,
)
from settings import settings

logger = get_logger(__name__)

ANALYTICS_READ = Depends(require_permission("analytics:read"))
SETTINGS_WRITE = Depends(require_permission("settings:write"))

router = APIRouter(prefix="/v1/admin/search", tags=["admin"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])

#: Held for the duration of a rebuild, so two replicas cannot both rewrite one index.
REINDEX_LOCK = "search:reindex"


def _resolve_indexes(requested: str | None) -> list[str]:
    """Which indexes an operation applies to.

    An unrecognised name is a 404 rather than a silent no-op — "the reindex ran and
    did nothing" is a far worse thing to debug than an error.
    """
    if requested is None:
        return list(settings.index_names)
    if requested not in settings.index_names:
        raise NotFoundError(
            f"No index named '{requested}'.", details={"indexes": list(settings.index_names)}
        )
    return [requested]


async def _reindex(
    ctx: Ctx, indexer: Indexing, payload: ReindexRequest, *, triggered_by: str
) -> list[ReindexResponse]:
    names = _resolve_indexes(payload.index)

    async def _run() -> list[ReindexResponse]:
        runs = [
            await indexer.run(
                index=name, mode=payload.mode, triggered_by=triggered_by, force=payload.force
            )
            for name in names
        ]
        return [ReindexResponse.model_validate(run) for run in runs]

    redis = ctx.redis
    if redis is None:
        # No Redis means a single instance with nothing to race against.
        return await _run()

    async with redis.lock(REINDEX_LOCK, ttl=settings.reindex_lock_seconds) as acquired:
        if not acquired:
            raise ConflictError(
                "A reindex is already running.",
                code="reindex_in_progress",
                details={"hint": "Wait for it to finish, or check /v1/admin/search/runs."},
            )
        return await _run()


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    response_model=IndexHealth,
    summary="Meilisearch reachability and index stats",
    description=(
        "Deliberately separate from `/health/ready`. A search engine that is down "
        "should show up here, not restart every replica — the service degrades to "
        "clean 503s on the query path and keeps serving everything else."
    ),
    dependencies=[ANALYTICS_READ],
)
async def index_health(meili: Meili) -> IndexHealth:
    health = await meili.health()
    reachable = health.get("status") == "up"
    stats: dict = {}
    if reachable:
        for name in settings.index_names:
            stats[name] = await meili.index_stats(name) or {"status": "missing"}
    return IndexHealth(
        reachable=reachable,
        status=str(health.get("status", "unknown")),
        indexes=stats,
        error=health.get("error"),
    )


@router.post(
    "/reindex",
    response_model=list[ReindexResponse],
    summary="Rebuild or reconcile the indexes",
    description=(
        "`reconcile` (the default) repairs only what diverged from the source. "
        "`full` empties the index first — use it when the document shape or the "
        "index settings changed.\n\n"
        "Takes a distributed lock: two replicas rebuilding at once would each "
        "delete the other's freshly written documents."
    ),
    dependencies=[SETTINGS_WRITE],
)
async def reindex(
    payload: ReindexRequest,
    indexer: Indexing,
    ctx: Ctx,
) -> list[ReindexResponse]:
    return await _reindex(ctx, indexer, payload, triggered_by="admin")


@router.get(
    "/runs",
    response_model=ListResponse[ReindexResponse],
    summary="Recent reindex runs",
    dependencies=[ANALYTICS_READ],
)
async def list_runs(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ListResponse[ReindexResponse]:
    from sqlalchemy import select

    stmt = select(ReindexRun).order_by(ReindexRun.started_at.desc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    return ListResponse[ReindexResponse](
        items=[ReindexResponse.model_validate(row) for row in rows], total=len(rows)
    )


@router.get(
    "/analytics",
    response_model=AnalyticsResponse,
    summary="Search analytics",
    description=(
        "The zero-result list is the point of this endpoint: what people look for "
        "and do not find is a list of books to acquire and synonyms to add, "
        "written by the people who wanted to buy them."
    ),
    dependencies=[ANALYTICS_READ],
)
async def analytics_report(
    session: DbSession,
    analytics: Analytics,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    top: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AnalyticsResponse:
    return await analytics.report(session, days=days, top=top)


@router.delete(
    "/trending",
    response_model=MessageResponse,
    summary="Clear the trending buckets",
    description="For when a spike is spam rather than interest.",
    dependencies=[SETTINGS_WRITE],
)
async def reset_trending(trending: Trending) -> MessageResponse:
    await trending.reset()
    return MessageResponse(message="Trending searches cleared.")


# ---------------------------------------------------------------------------
# Internal (HMAC-signed callers only)
# ---------------------------------------------------------------------------


@internal_router.post(
    "/documents",
    response_model=IndexOperationResponse,
    summary="Push documents straight into an index (internal)",
    description=(
        "Used by the automation pipeline's publish stage, which already holds the "
        "full record and should not force a round trip back to the books service."
    ),
)
async def push_documents(
    payload: DocumentBatchRequest,
    caller: InternalCaller,
    indexer: Indexing,
) -> IndexOperationResponse:
    index = payload.index or settings.books_index
    _resolve_indexes(index)
    indexed, skipped, failed = await indexer.index_documents(index, payload.documents)
    logger.info(
        "search.documents_pushed", index=index, caller=caller, indexed=indexed, failed=failed
    )
    return IndexOperationResponse(
        index_name=index, accepted=indexed, skipped=skipped, failed=failed
    )


@internal_router.post(
    "/documents/delete",
    response_model=IndexOperationResponse,
    summary="Remove documents from an index (internal)",
)
async def delete_documents(
    payload: DocumentDeleteRequest,
    caller: InternalCaller,
    indexer: Indexing,
) -> IndexOperationResponse:
    index = payload.index or settings.books_index
    _resolve_indexes(index)
    removed = await indexer.delete_documents(index, payload.document_ids)
    return IndexOperationResponse(index_name=index, accepted=removed, skipped=0, failed=0)


@internal_router.post(
    "/reindex",
    response_model=list[ReindexResponse],
    summary="Trigger a reindex (internal)",
    description="Called by the worker on a schedule. Same lock as the admin route.",
)
async def internal_reindex(
    payload: ReindexRequest,
    caller: InternalCaller,
    indexer: Indexing,
    ctx: Ctx,
) -> list[ReindexResponse]:
    return await _reindex(ctx, indexer, payload, triggered_by=f"internal:{caller}")


@internal_router.post(
    "/maintenance/prune-analytics",
    response_model=MessageResponse,
    summary="Drop analytics rows past their useful life (internal)",
    description=(
        "These accumulate faster than anything else in this service — one row per "
        "completed search — and a year-old query tells you nothing a month-old one "
        "does not."
    ),
)
async def prune_analytics(
    caller: InternalCaller,
    session: DbSession,
    analytics: Analytics,
    days: Annotated[int, Query(ge=7, le=730)] = 90,
) -> MessageResponse:
    pruned = await analytics.prune(session, older_than_days=days)
    return MessageResponse(message=f"Pruned {pruned} analytics rows.")

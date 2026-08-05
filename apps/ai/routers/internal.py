"""Generation, moderation and embeddings for sibling services.

The automation pipeline is the heaviest caller: it generates a description, a
summary, SEO metadata, tags and categories for every book it ingests. That is five
model calls per book, which is why `/internal/generate/batch` exists — one request,
one budget check, and a partial result rather than an all-or-nothing failure.

**A book with no AI description is still publishable; one that blocks the pipeline
is not.** The batch endpoint reports per-kind failures and lets the caller decide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from deps import Budget, DbSession, Generate, Moderation, Providers
from knowledgeos_core import MessageResponse, ServiceUnavailableError, get_logger
from knowledgeos_core.deps import InternalCaller
from models import Generation
from schemas import (
    BatchGenerateRequest,
    BatchGenerateResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    GenerateRequest,
    GenerationOut,
    GenerationStatus,
    ModerationRequest,
    ModerationResult,
    UsageSummary,
)
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


def _out(result, kind) -> GenerationOut:  # type: ignore[no-untyped-def]
    return GenerationOut(
        id=result.id,
        kind=kind,
        status=result.status,
        content=result.content,
        data=result.data or {},
        provider=result.provider,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        error=result.error,
        created_at=datetime.now(UTC),
    )


@router.post(
    "/generate",
    response_model=GenerationOut,
    summary="Generate one field (internal)",
    description=(
        "Cached by a fingerprint of the book metadata and excerpt, so the same book "
        "does not pay twice. `force_refresh` bypasses it — that costs money and is "
        "recorded as a distinct row."
    ),
)
async def generate(
    payload: GenerateRequest,
    caller: InternalCaller,
    session: DbSession,
    generator: Generate,
) -> GenerationOut:
    result = await generator.generate(
        session,
        kind=payload.kind,
        context=payload.context,
        book_id=payload.book_id,
        source=caller,
        locale=payload.locale,
        force_refresh=payload.force_refresh,
    )
    return _out(result, payload.kind)


@router.post(
    "/generate/batch",
    response_model=BatchGenerateResponse,
    summary="Generate several fields for one book (internal)",
    description=(
        "What the automation pipeline calls. Returns a **partial result**: kinds that "
        "failed are listed rather than failing the whole request, because a book with "
        "no AI description is still publishable and a pipeline that stops for one is "
        "not.\n\n"
        "Kinds run sequentially, not concurrently — five parallel calls would blow "
        "through a provider's rate limit and make the budget check meaningless, since "
        "all five would pass it before any of them recorded a cost."
    ),
)
async def generate_batch(
    payload: BatchGenerateRequest,
    caller: InternalCaller,
    session: DbSession,
    generator: Generate,
) -> BatchGenerateResponse:
    results: dict[str, GenerationOut] = {}
    failed: list[str] = []
    total = 0.0

    for kind in payload.kinds:
        try:
            result = await generator.generate(
                session,
                kind=kind,
                context=payload.context,
                book_id=payload.book_id,
                source=caller,
                force_refresh=payload.force_refresh,
            )
        except ServiceUnavailableError:
            # Budget exhausted or every provider down. Nothing after this will
            # succeed either, so stop rather than logging the same failure five times.
            failed.extend(
                str(remaining) for remaining in payload.kinds[len(results) + len(failed) :]
            )
            logger.warning("ai.batch_aborted", book_id=str(payload.book_id))
            break
        except Exception as exc:
            failed.append(str(kind))
            logger.warning("ai.batch_kind_failed", kind=str(kind), error=str(exc))
            continue

        results[str(kind)] = _out(result, kind)
        total += result.cost_usd

    logger.info(
        "ai.batch_generated",
        book_id=str(payload.book_id),
        succeeded=len(results),
        failed=len(failed),
        cost_usd=round(total, 6),
    )
    return BatchGenerateResponse(
        book_id=payload.book_id, results=results, failed=failed, total_cost_usd=round(total, 6)
    )


@router.post(
    "/moderate",
    response_model=ModerationResult,
    summary="Classify user content (internal)",
    description=(
        "Fails **closed**: when the classifier is unavailable this returns 503 rather "
        "than allowing the content through. An unmoderated path opens exactly when "
        "moderation is under strain, which is when it is most needed.\n\n"
        "A review is allowed to be harshly negative about a book — criticism of a "
        "work is not harassment of its author."
    ),
)
async def moderate(
    payload: ModerationRequest,
    caller: InternalCaller,
    session: DbSession,
    moderation: Moderation,
) -> ModerationResult:
    return await moderation.check(session, content=payload.content, context=payload.context)


@router.post(
    "/embeddings",
    response_model=EmbeddingResponse,
    summary="Vectors for semantic search (internal)",
    description="Used by the search service when SEMANTIC_ENABLED is on.",
)
async def embeddings(
    payload: EmbeddingRequest,
    caller: InternalCaller,
    session: DbSession,
    providers: Providers,
    budget: Budget,
) -> EmbeddingResponse:
    from services.providers import OpenAIProvider, estimate_cost

    provider = providers.get("openai")
    if not isinstance(provider, OpenAIProvider):
        raise ServiceUnavailableError(
            "Embeddings need an OpenAI key on this deployment.",
            code="embeddings_unavailable",
            details={"hint": "Set OPENAI_API_KEY."},
        )

    await budget.check(session)
    vectors, tokens, model = await provider.embed(payload.texts, payload.model)
    cost = estimate_cost(model, tokens, 0)
    await budget.record(session, cost_usd=cost, input_tokens=tokens)
    await session.commit()

    return EmbeddingResponse(
        embeddings=vectors,
        model=model,
        dimensions=len(vectors[0]) if vectors else settings.embedding_dimensions,
        input_tokens=tokens,
        cost_usd=cost,
    )


@router.get(
    "/usage",
    response_model=UsageSummary,
    summary="Spend and volume (internal)",
    description=(
        "Cached and blocked requests are counted separately. Folding them into the "
        "total would make the cache look free and moderation look like it never ran."
    ),
)
async def usage(
    caller: InternalCaller,
    session: DbSession,
    budget: Budget,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> UsageSummary:
    since = datetime.now(UTC) - timedelta(days=days)
    window = [Generation.created_at >= since]

    def _count(status_value):  # type: ignore[no-untyped-def]
        # coalesce because SUM over an empty window is NULL, which would propagate
        # into every derived figure as None.
        return func.coalesce(func.sum(case((Generation.status == status_value, 1), else_=0)), 0)

    row = (
        await session.execute(
            select(
                func.count(Generation.id),
                _count(GenerationStatus.CACHED),
                _count(GenerationStatus.FAILED),
                _count(GenerationStatus.BLOCKED),
                func.coalesce(func.sum(Generation.input_tokens), 0),
                func.coalesce(func.sum(Generation.output_tokens), 0),
                func.coalesce(func.sum(Generation.cost_usd), 0.0),
            ).where(*window)
        )
    ).one()
    total, cached, failed, blocked, input_tokens, output_tokens, cost = row

    by_kind = {
        str(kind): int(count)
        for kind, count in (
            await session.execute(
                select(Generation.kind, func.count(Generation.id))
                .where(*window)
                .group_by(Generation.kind)
            )
        ).all()
    }
    by_provider = {
        str(provider): int(count)
        for provider, count in (
            await session.execute(
                select(Generation.provider, func.count(Generation.id))
                .where(*window, Generation.provider.is_not(None))
                .group_by(Generation.provider)
            )
        ).all()
    }

    state = await budget.state(session)
    return UsageSummary(
        window_days=days,
        total_requests=int(total),
        cached_requests=int(cached),
        failed_requests=int(failed),
        blocked_requests=int(blocked),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cost_usd=round(float(cost), 6),
        daily_limit_usd=settings.daily_cost_limit_usd,
        today_cost_usd=round(state.spent_usd, 6),
        by_kind=by_kind,
        by_provider=by_provider,
    )


@router.post(
    "/maintenance/prune",
    response_model=MessageResponse,
    summary="Drop expired cache and old per-user budget rows (internal)",
)
async def prune(
    caller: InternalCaller,
    session: DbSession,
    budget: Budget,
    days: Annotated[int, Query(ge=7, le=730)] = 90,
) -> MessageResponse:
    from sqlalchemy import delete

    from models import CachedResult, Conversation

    cutoff = datetime.now(UTC) - timedelta(days=days)
    expired = await session.execute(
        delete(CachedResult).where(
            CachedResult.expires_at.is_not(None), CachedResult.expires_at < datetime.now(UTC)
        )
    )
    stale_chats = await session.execute(
        delete(Conversation).where(Conversation.updated_at < cutoff)
    )
    await session.commit()
    budgets = await budget.prune(session)

    return MessageResponse(
        message=(
            f"Pruned {expired.rowcount or 0} cache entries, "
            f"{stale_chats.rowcount or 0} conversations, {budgets} budget rows."
        )
    )

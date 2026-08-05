"""AI spend and volume for the admin console.

The same numbers `/internal/usage` returns, behind a bearer token instead of an HMAC
signature — because the console is a browser and cannot sign. Both routes call one
service; a second implementation would drift, and the failure mode is the console and
the ops endpoint disagreeing about the bill with no way to tell which is right.

Everything here needs `analytics:read`. Nothing here exposes a prompt or a generated
answer: an admin console is not a place to read customers' questions, and the parts
that diagnose a problem — provider, model, error, latency — are enough.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from deps import Budget, DbSession, Reporting
from knowledgeos_core import get_logger
from knowledgeos_core.deps import require_permission
from schemas import DailySpend, FailureOut, SpendHistory, UsageSummary
from settings import settings

logger = get_logger(__name__)

ANALYTICS_READ = Depends(require_permission("analytics:read"))

router = APIRouter(prefix="/v1/admin/ai", tags=["admin"])


@router.get(
    "/usage",
    response_model=UsageSummary,
    summary="Spend and volume",
    description=(
        "Cached, blocked and failed requests are counted **separately** and never "
        "folded into the total. Collapsing them makes the cache look free, makes "
        "moderation look like it never ran, and hides the failure rate — which is the "
        "number that says a provider is degrading.\n\n"
        "`billable_requests` is the denominator for cost per generation. Dividing by "
        "`total_requests` instead makes every cache hit look like it made the model "
        "cheaper, which is the opposite of what happened."
    ),
    dependencies=[ANALYTICS_READ],
)
async def usage(
    session: DbSession,
    reporting: Reporting,
    budget: Budget,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> UsageSummary:
    totals = await reporting.usage(session, days=days)
    state = await budget.state(session)
    return UsageSummary(
        window_days=days,
        total_requests=totals.total_requests,
        cached_requests=totals.cached_requests,
        failed_requests=totals.failed_requests,
        blocked_requests=totals.blocked_requests,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cost_usd=totals.cost_usd,
        daily_limit_usd=settings.daily_cost_limit_usd,
        today_cost_usd=round(state.spent_usd, 6),
        by_kind=totals.by_kind,
        by_provider=totals.by_provider,
        billable_requests=totals.billable_requests,
    )


@router.get(
    "/spend",
    response_model=SpendHistory,
    summary="Daily spend history",
    description=(
        "Platform-wide, newest first, for the cost chart. Read from the budget "
        "counters rather than summed from the generation ledger — the counters are "
        "what the ceiling is enforced against, so a chart built from anything else "
        "could disagree with the limit that actually stopped serving."
    ),
    dependencies=[ANALYTICS_READ],
)
async def spend(
    session: DbSession,
    budget: Budget,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> SpendHistory:
    history = await budget.history(session, days=days)
    rows = [
        DailySpend(day=day, cost_usd=round(cost, 6), request_count=count)
        for day, cost, count in history
    ]
    return SpendHistory(
        days=rows,
        daily_limit_usd=settings.daily_cost_limit_usd,
        total_usd=round(sum(row.cost_usd for row in rows), 6),
    )


@router.get(
    "/failures",
    response_model=list[FailureOut],
    summary="Recent generation failures",
    description=(
        "A spike in the failure count is a number; these are the diagnosis. Prompts "
        "and generated answers are deliberately not included — an admin console is "
        "not a place to read customers' questions."
    ),
    dependencies=[ANALYTICS_READ],
)
async def failures(
    session: DbSession,
    reporting: Reporting,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[FailureOut]:
    rows = await reporting.recent_failures(session, days=days, limit=limit)
    return [FailureOut.model_validate(row) for row in rows]

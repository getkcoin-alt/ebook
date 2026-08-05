"""Reading the generation ledger: spend, volume, and what it was spent on.

Extracted from the internal router when the admin console needed the same numbers.
Two callers, one query — the alternative is a second implementation that drifts, and
the failure mode there is the console and the ops endpoint disagreeing about the bill,
with no way to tell which is right.

**Cached, blocked and failed requests are counted separately and never folded into a
total.** Collapsing them makes the cache look free, moderation look like it never ran,
and the failure rate invisible — and the failure rate is the number that says a
provider is degrading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import get_logger
from models import Generation
from schemas import GenerationStatus
from settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UsageTotals:
    total_requests: int
    cached_requests: int
    failed_requests: int
    blocked_requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    by_kind: dict[str, int]
    by_provider: dict[str, int]

    @property
    def billable_requests(self) -> int:
        """Requests that actually reached a provider.

        The denominator for "cost per generation". Dividing by the total instead
        makes every cache hit look like it made the model cheaper, which is the
        opposite of what happened.
        """
        return max(0, self.total_requests - self.cached_requests - self.blocked_requests)


class ReportingService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def usage(self, session: AsyncSession, *, days: int = 7) -> UsageTotals:
        since = datetime.now(UTC) - timedelta(days=days)
        window = [Generation.created_at >= since]

        def count_of(status_value):  # type: ignore[no-untyped-def]
            # `coalesce` because SUM over an empty window is NULL, which would
            # propagate into every derived figure as None and render as a blank card.
            return func.coalesce(func.sum(case((Generation.status == status_value, 1), else_=0)), 0)

        row = (
            await session.execute(
                select(
                    func.count(Generation.id),
                    count_of(GenerationStatus.CACHED),
                    count_of(GenerationStatus.FAILED),
                    count_of(GenerationStatus.BLOCKED),
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

        return UsageTotals(
            total_requests=int(total),
            cached_requests=int(cached),
            failed_requests=int(failed),
            blocked_requests=int(blocked),
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cost_usd=round(float(cost), 6),
            by_kind=by_kind,
            by_provider=by_provider,
        )

    async def recent_failures(
        self, session: AsyncSession, *, limit: int = 20, days: int = 7
    ) -> list[Generation]:
        """The last failures, newest first.

        The one view worth having beyond the totals: a spike in the failure count is
        a number, and the error strings behind it are the diagnosis. Everything else
        about a generation — its prompt, its output — is deliberately not surfaced
        here, because an admin console is not a place to read customers' questions.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(Generation)
            .where(
                Generation.created_at >= since,
                Generation.status == GenerationStatus.FAILED,
            )
            .order_by(Generation.created_at.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

"""Cost control.

This is the most important module in the service, and the reason is structural: every
other service on the platform fails by becoming unavailable, which is loud. This one
fails by spending money, which is silent until the invoice arrives. A retry loop
against a paid model is the only bug in this codebase that keeps costing after
everyone has gone home.

So the ceiling is a **hard stop, not a target**. Reaching it returns 503 and serves
nothing. That is a deliberate trade: an AI feature that is off for the rest of the
day is a worse product, and an uncapped one is a worse business.

Two mechanics make it trustworthy:

* **The check is a single indexed row read**, not a SUM over the generation ledger.
  It runs before every request, and a full scan of a growing table on the hot path is
  how a cost control becomes the reason the service is slow.
* **Spend is recorded with a conditional UPDATE**, so concurrent requests cannot both
  read the same total and both decide there is room. Read-modify-write here means the
  ceiling is advisory under exactly the load that makes it matter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ServiceUnavailableError, get_logger
from models import CostBudget
from settings import Settings

logger = get_logger(__name__)


def today() -> str:
    """UTC day key. UTC rather than local time so the ceiling does not reset twice
    a year, and so every replica agrees on when "today" ends."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class BudgetState:
    spent_usd: float
    limit_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd

    @property
    def fraction(self) -> float:
        return (self.spent_usd / self.limit_usd) if self.limit_usd else 0.0


class BudgetService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.cost_tracking_enabled

    async def _row(
        self, session: AsyncSession, *, day: str, user_id: uuid.UUID | None
    ) -> CostBudget | None:
        stmt = select(CostBudget).where(CostBudget.day == day)
        # `IS NULL` rather than `= NULL`: the platform-wide row has a null user, and
        # `= NULL` is never true in SQL, so the wrong comparison silently means the
        # global ceiling is never found and never enforced.
        stmt = stmt.where(
            CostBudget.user_id.is_(None) if user_id is None else CostBudget.user_id == user_id
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def state(
        self, session: AsyncSession, *, user_id: uuid.UUID | None = None
    ) -> BudgetState:
        limit = (
            self._settings.user_daily_cost_limit_usd
            if user_id is not None
            else self._settings.daily_cost_limit_usd
        )
        row = await self._row(session, day=today(), user_id=user_id)
        return BudgetState(spent_usd=float(row.cost_usd) if row else 0.0, limit_usd=limit)

    async def check(self, session: AsyncSession, *, user_id: uuid.UUID | None = None) -> None:
        """Raise if this request would exceed a ceiling.

        Both ceilings are checked: the platform's, and the caller's own. A single
        user must not be able to consume the whole budget, or the first person to
        write a script takes the feature away from everyone else.
        """
        if not self.enabled:
            return

        platform = await self.state(session)
        if platform.exhausted:
            logger.error(
                "ai.budget_exhausted",
                scope="platform",
                spent=platform.spent_usd,
                limit=platform.limit_usd,
            )
            raise ServiceUnavailableError(
                "The daily AI budget for this platform has been reached. "
                "AI features will resume tomorrow.",
                code="ai_budget_exhausted",
                details={"scope": "platform", "resets_at": _tomorrow()},
            )

        if platform.fraction >= self._settings.cost_warning_threshold:
            # The signal that someone should look *before* the ceiling turns the
            # feature off entirely.
            logger.warning(
                "ai.budget_warning",
                spent=round(platform.spent_usd, 4),
                limit=platform.limit_usd,
                fraction=round(platform.fraction, 3),
            )

        if user_id is not None:
            personal = await self.state(session, user_id=user_id)
            if personal.exhausted:
                logger.info("ai.budget_exhausted", scope="user", user_id=str(user_id))
                raise ServiceUnavailableError(
                    "You have reached your daily limit for AI features. It resets at midnight UTC.",
                    code="ai_user_budget_exhausted",
                    details={"scope": "user", "resets_at": _tomorrow()},
                )

    async def record(
        self,
        session: AsyncSession,
        *,
        cost_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Add spend to today's counters — platform-wide and per user.

        Does not commit; the caller owns the transaction so the spend lands with the
        generation row it belongs to. A ledger entry without its cost, or a cost
        without its ledger entry, makes the two disagree forever.
        """
        if not self.enabled:
            return

        await self._increment(
            session,
            user_id=None,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if user_id is not None:
            await self._increment(
                session,
                user_id=user_id,
                cost_usd=cost_usd,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

    async def _increment(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | None,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        day = today()
        condition = (
            CostBudget.user_id.is_(None) if user_id is None else CostBudget.user_id == user_id
        )
        # A conditional UPDATE rather than read-modify-write: two concurrent requests
        # would otherwise both read the same total and both add to it from the same
        # base, losing one of the charges. Under load — which is exactly when the
        # ceiling matters — the counter would drift low and the limit would not hold.
        result = await session.execute(
            update(CostBudget)
            .where(CostBudget.day == day, condition)
            .values(
                cost_usd=CostBudget.cost_usd + cost_usd,
                request_count=CostBudget.request_count + 1,
                input_tokens=CostBudget.input_tokens + input_tokens,
                output_tokens=CostBudget.output_tokens + output_tokens,
            )
        )
        if result.rowcount:
            return

        # First request of the day for this scope.
        savepoint = await session.begin_nested()
        session.add(
            CostBudget(
                day=day,
                user_id=user_id,
                cost_usd=cost_usd,
                request_count=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError:
            # Another request created the row between the UPDATE and the INSERT.
            # Roll back just this attempt and apply the increment to their row.
            await savepoint.rollback()
            await session.execute(
                update(CostBudget)
                .where(CostBudget.day == day, condition)
                .values(
                    cost_usd=CostBudget.cost_usd + cost_usd,
                    request_count=CostBudget.request_count + 1,
                    input_tokens=CostBudget.input_tokens + input_tokens,
                    output_tokens=CostBudget.output_tokens + output_tokens,
                )
            )

    async def history(
        self, session: AsyncSession, *, days: int = 30
    ) -> list[tuple[str, float, int]]:
        """Platform-wide daily spend, newest first. Feeds the admin cost chart."""
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        stmt = (
            select(CostBudget.day, CostBudget.cost_usd, CostBudget.request_count)
            .where(CostBudget.day >= since, CostBudget.user_id.is_(None))
            .order_by(CostBudget.day.desc())
        )
        return [(row[0], float(row[1]), int(row[2])) for row in (await session.execute(stmt)).all()]

    async def prune(self, session: AsyncSession, *, keep_days: int = 400) -> int:
        """Drop old per-user rows.

        The platform-wide rows are kept: they are a year-over-year cost record and
        cost nothing to retain. The per-user rows are the ones that grow with the
        user base, and nobody needs last year's per-user daily spend.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        stale = (
            (
                await session.execute(
                    select(CostBudget).where(
                        CostBudget.day < cutoff, CostBudget.user_id.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in stale:
            await session.delete(row)
        if stale:
            await session.commit()
        return len(stale)

    async def total_spent(self, session: AsyncSession, *, days: int = 30) -> float:
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        stmt = select(func.coalesce(func.sum(CostBudget.cost_usd), 0.0)).where(
            CostBudget.day >= since, CostBudget.user_id.is_(None)
        )
        return float((await session.execute(stmt)).scalar_one())


def _tomorrow() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

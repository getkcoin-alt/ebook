"""Run history and scheduler health.

The history exists for one question — *did the nightly reconcile run, and did it
work?* — and one that is harder: **has beat stopped firing at all?**

The second is the reason this table is worth its cost. A job that is not running
produces no log lines and no failures. It is invisible in every signal except the
absence of recent runs, so "no run in several of its own intervals" is the check that
catches a scheduler which died quietly, and it is the field worth alerting on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from schedule import SCHEDULE, ScheduledJob
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import get_logger
from models import TaskRun
from schemas import JobOut, RunOutcome, SchedulerHealth
from settings import Settings

logger = get_logger(__name__)

#: A job is considered stale after this many of its own intervals with no run. Three
#: rather than one: a single missed tick is a deploy or a restart, and alerting on it
#: teaches everyone to ignore the alert.
STALE_INTERVALS = 3

#: Outcomes that count against a job's health. `skipped_locked` does not — it means
#: single-flight worked, and a three-replica deployment would otherwise look like it
#: is failing two runs in three.
UNHEALTHY_OUTCOMES = (RunOutcome.FAILED, RunOutcome.TIMED_OUT)


class HistoryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def record(self, session: AsyncSession, row: TaskRun, *, commit: bool = True) -> TaskRun:
        session.add(row)
        if commit:
            await session.commit()
        return row

    async def runs(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        job_name: str | None = None,
        outcome: RunOutcome | None = None,
    ) -> tuple[list[TaskRun], int]:
        stmt = select(TaskRun)
        count_stmt = select(func.count(TaskRun.id))
        if job_name:
            stmt = stmt.where(TaskRun.job_name == job_name)
            count_stmt = count_stmt.where(TaskRun.job_name == job_name)
        if outcome is not None:
            stmt = stmt.where(TaskRun.outcome == outcome)
            count_stmt = count_stmt.where(TaskRun.outcome == outcome)

        total = int((await session.execute(count_stmt)).scalar_one())
        stmt = stmt.order_by(TaskRun.started_at.desc()).limit(limit).offset(offset)
        return list((await session.execute(stmt)).scalars().all()), total

    async def last_run(self, session: AsyncSession, job_name: str) -> TaskRun | None:
        stmt = (
            select(TaskRun)
            .where(TaskRun.job_name == job_name)
            .order_by(TaskRun.started_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    async def consecutive_failures(self, session: AsyncSession, job_name: str) -> int:
        """Failures since the last success.

        Read from the most recent runs rather than counted over a window: "three
        failures today" is also true of a job that failed three times this morning and
        has been fine since, and that job does not need anyone's attention.
        """
        stmt = (
            select(TaskRun.outcome)
            .where(TaskRun.job_name == job_name)
            .order_by(TaskRun.started_at.desc())
            .limit(20)
        )
        streak = 0
        for (outcome,) in (await session.execute(stmt)).all():
            if outcome in UNHEALTHY_OUTCOMES:
                streak += 1
            elif outcome is RunOutcome.SKIPPED_LOCKED:
                # Neither a success nor a failure. Skipped past, so a run that another
                # replica took does not reset a genuine failure streak.
                continue
            else:
                break
        return streak

    async def describe(self, session: AsyncSession, job: ScheduledJob) -> JobOut:
        last = await self.last_run(session, job.name)
        failures = await self.consecutive_failures(session, job.name)
        return JobOut(
            name=job.name,
            service=job.service,
            path=job.path,
            cron=f"{job.minute} {job.hour} * * {job.day_of_week}",
            enabled=job.enabled and job.name not in self._settings.disabled_job_set,
            critical=job.critical,
            timeout=job.timeout,
            description=job.description,
            last_run_at=last.started_at if last else None,
            last_outcome=last.outcome if last else None,
            last_duration_ms=last.duration_ms if last else None,
            consecutive_failures=failures,
            healthy=failures < self._settings.unhealthy_after_failures,
        )

    async def list_jobs(self, session: AsyncSession) -> list[JobOut]:
        return [await self.describe(session, job) for job in SCHEDULE]

    async def health(self, session: AsyncSession) -> SchedulerHealth:
        now = datetime.now(UTC)
        hour_ago = now - timedelta(hours=1)

        runs_last_hour = int(
            (
                await session.execute(
                    select(func.count(TaskRun.id)).where(TaskRun.started_at >= hour_ago)
                )
            ).scalar_one()
        )
        failures_last_hour = int(
            (
                await session.execute(
                    select(func.count(TaskRun.id)).where(
                        TaskRun.started_at >= hour_ago,
                        TaskRun.outcome.in_(UNHEALTHY_OUTCOMES),
                    )
                )
            ).scalar_one()
        )
        last_run_at = _aware(
            (await session.execute(select(func.max(TaskRun.started_at)))).scalar_one_or_none()
        )
        # How long this deployment has been recording anything. A job that has never
        # run is only stale once the scheduler has been alive longer than that job's
        # own window — otherwise every fresh install reports its whole schedule as
        # broken thirty seconds after boot.
        first_run_at = _aware(
            (await session.execute(select(func.min(TaskRun.started_at)))).scalar_one_or_none()
        )
        observed_for = (now - first_run_at).total_seconds() if first_run_at else 0.0

        unhealthy: list[str] = []
        stale: list[str] = []
        disabled = self._settings.disabled_job_set

        for job in SCHEDULE:
            if not job.enabled or job.name in disabled:
                continue
            described = await self.describe(session, job)
            if not described.healthy:
                unhealthy.append(job.name)

            window = _interval_seconds(job) * STALE_INTERVALS
            if described.last_run_at is None:
                if observed_for > window:
                    stale.append(job.name)
            elif (now - _aware(described.last_run_at)).total_seconds() > window:
                stale.append(job.name)

        return SchedulerHealth(
            total_jobs=len(SCHEDULE),
            enabled_jobs=sum(1 for job in SCHEDULE if job.enabled and job.name not in disabled),
            unhealthy_jobs=unhealthy,
            stale_jobs=stale,
            runs_last_hour=runs_last_hour,
            failures_last_hour=failures_last_hour,
            last_run_at=last_run_at,
        )

    async def prune(self, session: AsyncSession) -> int:
        """Drop run records past retention.

        This service's own sweep, and the only one it runs against itself. A history
        table that grows forever is the same problem this service exists to solve for
        everyone else.
        """
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.run_retention_days)
        total = int(
            (
                await session.execute(
                    select(func.count(TaskRun.id)).where(TaskRun.started_at < cutoff)
                )
            ).scalar_one()
        )
        if total:
            await session.execute(delete(TaskRun).where(TaskRun.started_at < cutoff))
            await session.commit()
        return total


def _aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive timestamp.

    PostgreSQL returns these tz-aware; SQLite has no timezone type and returns them
    naive, so an arithmetic comparison raises `TypeError` on the backend the tests
    run against and works fine in production — the worst possible arrangement.
    Everything stored here is written in UTC, so attaching it is exact.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _interval_seconds(job: ScheduledJob) -> int:
    """Roughly how often the job runs, from its cron fields.

    Approximate on purpose. This feeds a staleness threshold that is already
    multiplied by three, so the difference between "hourly" and "every 57 minutes"
    changes nothing — and a real cron parser here would be a dependency carried for a
    number that is then rounded away.
    """
    minute = job.minute
    if minute.startswith("*/"):
        try:
            return max(60, int(minute[2:]) * 60)
        except ValueError:
            return 3_600
    if minute == "*":
        return 60
    # A fixed minute: hourly if the hour is a wildcard, daily otherwise.
    return 3_600 if job.hour == "*" else 86_400


def parse_triggered_by(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None

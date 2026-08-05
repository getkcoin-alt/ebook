"""Job and stage persistence — the checkpoint layer.

This module owns the answer to one question: *where did this job get to?* Everything
about the pipeline's reliability rests on that answer being durable and honest.

**The checkpoint is written after the stage's side effects, in the same transaction
where possible.** A checkpoint written first means a crash between the two loses the
work but claims it succeeded; a checkpoint written in a separate transaction after a
side effect means a crash between them redoes the work. Neither is avoidable in
general — the side effects reach object storage and sibling services, which are not
in our transaction — so stages are written to be **idempotent** instead, and the
checkpoint is recorded after. Redoing an idempotent stage is cheap; skipping one that
did not actually run is a book published without a cover.

**Backoff lives in the row, not in a sleeping worker.** `next_attempt_at` is a
timestamp the claim query filters on. A worker that holds a task through a ten-minute
backoff is a worker doing nothing while the queue grows behind it.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ConflictError, JobStatus, NotFoundError, get_logger
from models import AutomationJob, JobStage
from schemas import PIPELINE, JobCreate, JobOptions, PipelineStats, Stage, StageStatus
from settings import Settings

logger = get_logger(__name__)

#: Statuses that mean a job is still in the system's hands.
ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING)

_POSITION = {stage: index for index, stage in enumerate(PIPELINE)}


def _now() -> datetime:
    return datetime.now(UTC)


class JobService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- creation -------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        payload: JobCreate,
        *,
        requested_by: uuid.UUID | None = None,
        correlation_id: str | None = None,
        import_id: uuid.UUID | None = None,
    ) -> AutomationJob:
        """Queue a run. Refuses a second live job for the same source file.

        A double-clicked "process" button must not start the pipeline twice against
        one object: both runs would write derived artefacts under the same keys and
        both would try to publish, with the loser overwriting the winner's output
        somewhere in the middle. The partial unique index enforces it in the
        database; this check turns the resulting error into a useful 409.
        """
        existing = await self.active_for_source(session, payload.source_key)
        if existing is not None:
            raise ConflictError(
                "That file is already being processed.",
                code="job_already_running",
                details={"job_id": str(existing.id), "status": str(existing.status)},
            )

        job = AutomationJob(
            book_id=payload.book_id,
            source=payload.source,
            source_key=payload.source_key,
            original_filename=payload.original_filename,
            priority=payload.priority,
            options=payload.options.model_dump(mode="json"),
            status=JobStatus.QUEUED,
            requested_by=requested_by,
            correlation_id=correlation_id,
            import_id=import_id,
            result={},
        )
        session.add(job)
        await session.flush()

        # Every stage gets its row up front, PENDING. A history that materialises
        # rows as it goes cannot distinguish "stage 9 has not run yet" from "stage 9
        # was never part of this job", and the operator staring at a stuck job needs
        # exactly that distinction.
        options = JobOptions.model_validate(job.options)
        for stage in PIPELINE:
            session.add(
                JobStage(
                    job_id=job.id,
                    stage=stage,
                    position=_POSITION[stage],
                    status=(
                        StageStatus.SKIPPED
                        if not _stage_selected(stage, options)
                        else StageStatus.PENDING
                    ),
                )
            )
        await session.commit()

        logger.info(
            "automation.job_queued",
            job_id=str(job.id),
            book_id=str(job.book_id),
            source=str(job.source),
        )
        return job

    async def active_for_source(
        self, session: AsyncSession, source_key: str
    ) -> AutomationJob | None:
        stmt = select(AutomationJob).where(
            AutomationJob.source_key == source_key, AutomationJob.status.in_(ACTIVE)
        )
        return (await session.execute(stmt)).scalars().first()

    # ---- reading --------------------------------------------------------

    async def get(self, session: AsyncSession, job_id: uuid.UUID) -> AutomationJob:
        job = await session.get(AutomationJob, job_id)
        if job is None:
            raise NotFoundError("Job not found.", details={"job_id": str(job_id)})
        return job

    async def list_jobs(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        status: JobStatus | None = None,
        book_id: uuid.UUID | None = None,
    ) -> tuple[list[AutomationJob], int]:
        """Offset paging.

        This is an operator console over a table that is pruned on a schedule, not a
        public feed — the depth a cursor protects against is not reachable here, and
        an operator wants to jump to page 4.
        """
        stmt = select(AutomationJob)
        count_stmt = select(func.count(AutomationJob.id))
        if status is not None:
            stmt = stmt.where(AutomationJob.status == status)
            count_stmt = count_stmt.where(AutomationJob.status == status)
        if book_id is not None:
            stmt = stmt.where(AutomationJob.book_id == book_id)
            count_stmt = count_stmt.where(AutomationJob.book_id == book_id)

        total = int((await session.execute(count_stmt)).scalar_one())
        stmt = stmt.order_by(AutomationJob.created_at.desc()).limit(limit).offset(offset)
        return list((await session.execute(stmt)).scalars().all()), total

    async def stages(self, session: AsyncSession, job_id: uuid.UUID) -> list[JobStage]:
        stmt = select(JobStage).where(JobStage.job_id == job_id).order_by(JobStage.position)
        return list((await session.execute(stmt)).scalars().all())

    async def claim_due(self, session: AsyncSession, *, limit: int = 10) -> list[AutomationJob]:
        """Jobs eligible to run now, best first.

        `next_attempt_at` in the past (or null) is what makes backoff free: a job in
        its backoff window simply does not match, so no worker is holding it.
        """
        now = _now()
        stmt = (
            select(AutomationJob)
            .where(
                AutomationJob.status.in_((JobStatus.QUEUED, JobStatus.RETRYING)),
                (AutomationJob.next_attempt_at.is_(None)) | (AutomationJob.next_attempt_at <= now),
            )
            .order_by(AutomationJob.priority.desc(), AutomationJob.created_at)
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    # ---- lifecycle ------------------------------------------------------

    async def mark_running(self, session: AsyncSession, job: AutomationJob) -> None:
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.heartbeat_at = _now()
        job.error = None
        if job.started_at is None:
            job.started_at = _now()
        await session.commit()

    async def heartbeat(self, session: AsyncSession, job: AutomationJob) -> None:
        job.heartbeat_at = _now()
        await session.commit()

    async def stage_row(self, session: AsyncSession, job: AutomationJob, stage: Stage) -> JobStage:
        stmt = select(JobStage).where(JobStage.job_id == job.id, JobStage.stage == stage)
        row = (await session.execute(stmt)).scalars().one_or_none()
        if row is None:
            # A stage added to the pipeline after this job was created. Backfilled
            # rather than treated as an error, so an in-flight job survives a deploy
            # that lengthens the pipeline.
            row = JobStage(job_id=job.id, stage=stage, position=_POSITION[stage])
            session.add(row)
            await session.flush()
        return row

    async def begin_stage(
        self, session: AsyncSession, job: AutomationJob, stage: Stage
    ) -> JobStage:
        row = await self.stage_row(session, job, stage)
        row.status = StageStatus.RUNNING
        row.attempts += 1
        row.started_at = _now()
        row.error = None
        job.current_stage = stage
        job.heartbeat_at = _now()
        await session.commit()
        return row

    async def complete_stage(
        self,
        session: AsyncSession,
        job: AutomationJob,
        stage: Stage,
        *,
        output: dict | None = None,
        duration_ms: int = 0,
    ) -> None:
        """Checkpoint a successful stage and fold its output into the job."""
        row = await self.stage_row(session, job, stage)
        row.status = StageStatus.SUCCEEDED
        row.output = output or {}
        row.duration_ms = duration_ms
        row.finished_at = _now()
        row.error = None

        if output:
            # Reassigned rather than mutated: SQLAlchemy does not track in-place
            # changes to a plain JSON column, so `job.result.update(...)` would be
            # silently discarded at flush — and the next resume would rebuild from a
            # result document missing everything this run produced.
            job.result = {**(job.result or {}), **output}
        job.heartbeat_at = _now()
        await session.commit()

    async def skip_stage(
        self, session: AsyncSession, job: AutomationJob, stage: Stage, *, reason: str
    ) -> None:
        """Record that a stage was deliberately not run.

        Distinct from a failure, because the two lead to different operator actions:
        "no AI provider configured" is a deployment decision, "the model returned
        garbage" is a bug.
        """
        row = await self.stage_row(session, job, stage)
        row.status = StageStatus.SKIPPED
        row.error = reason
        row.finished_at = _now()
        await session.commit()

    async def fail_stage(
        self,
        session: AsyncSession,
        job: AutomationJob,
        stage: Stage,
        *,
        error: str,
        duration_ms: int = 0,
    ) -> None:
        row = await self.stage_row(session, job, stage)
        row.status = StageStatus.FAILED
        row.error = error[:4_000]
        row.duration_ms = duration_ms
        row.finished_at = _now()
        await session.commit()

    async def succeed(self, session: AsyncSession, job: AutomationJob) -> None:
        job.status = JobStatus.SUCCEEDED
        job.current_stage = None
        job.finished_at = _now()
        job.error = None
        job.next_attempt_at = None
        await session.commit()
        logger.info(
            "automation.job_succeeded",
            job_id=str(job.id),
            book_id=str(job.book_id),
            attempts=job.attempts,
        )

    async def schedule_retry(
        self, session: AsyncSession, job: AutomationJob, *, error: str
    ) -> bool:
        """Back off and try again, or dead-letter. Returns True if it will retry.

        The backoff is jittered. Without jitter, five hundred jobs that failed
        together because a provider went down all become eligible at the same instant
        and knock it over again the moment it recovers.
        """
        job.error = error[:4_000]
        if job.attempts >= self._settings.max_attempts:
            job.status = JobStatus.DEAD_LETTERED
            job.dead_lettered_at = _now()
            job.finished_at = _now()
            job.current_stage = None
            job.next_attempt_at = None
            await session.commit()
            logger.error(
                "automation.job_dead_lettered",
                job_id=str(job.id),
                book_id=str(job.book_id),
                attempts=job.attempts,
                stage=str(job.current_stage) if job.current_stage else None,
                error=error[:500],
            )
            return False

        delay = min(
            self._settings.retry_base_seconds * (2 ** max(0, job.attempts - 1)),
            self._settings.retry_max_seconds,
        )
        jittered = delay * random.uniform(0.5, 1.5)  # noqa: S311 - backoff spread, not crypto
        job.status = JobStatus.RETRYING
        job.next_attempt_at = _now() + timedelta(seconds=jittered)
        await session.commit()
        logger.warning(
            "automation.job_retrying",
            job_id=str(job.id),
            attempt=job.attempts,
            retry_in_seconds=round(jittered),
            error=error[:300],
        )
        return True

    async def fail_terminally(
        self, session: AsyncSession, job: AutomationJob, *, error: str
    ) -> None:
        """No retry. The input is wrong and will be just as wrong next time.

        A corrupt PDF retried five times with exponential backoff spends twenty
        minutes reaching the same answer and buries the real reason under four
        duplicate log lines.
        """
        job.status = JobStatus.FAILED
        job.error = error[:4_000]
        job.finished_at = _now()
        job.next_attempt_at = None
        await session.commit()
        logger.warning(
            "automation.job_failed",
            job_id=str(job.id),
            book_id=str(job.book_id),
            stage=str(job.current_stage) if job.current_stage else None,
            error=error[:500],
        )

    async def cancel(self, session: AsyncSession, job: AutomationJob) -> None:
        if job.is_terminal:
            raise ConflictError(
                "That job has already finished.", details={"status": str(job.status)}
            )
        job.status = JobStatus.CANCELLED
        job.current_stage = None
        job.finished_at = _now()
        job.next_attempt_at = None
        await session.commit()

    async def prepare_retry(
        self,
        session: AsyncSession,
        job: AutomationJob,
        *,
        from_stage: Stage | None = None,
        reset_attempts: bool = True,
    ) -> AutomationJob:
        """Requeue a settled job.

        `from_stage` re-opens that stage and everything after it, which is the move
        when a stage *succeeded* but produced something wrong — a description written
        from a bad excerpt, a cover extracted from the wrong page. Without it the
        resume starts after the last success and reproduces the same output.
        """
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING):
            raise ConflictError("That job is already running.", details={"status": str(job.status)})

        if from_stage is not None:
            cutoff = _POSITION[from_stage]
            await session.execute(
                update(JobStage)
                .where(JobStage.job_id == job.id, JobStage.position >= cutoff)
                .values(
                    status=StageStatus.PENDING,
                    error=None,
                    output={},
                    started_at=None,
                    finished_at=None,
                    duration_ms=None,
                )
            )

        job.status = JobStatus.QUEUED
        # Without this a dead-lettered job requeued once more immediately
        # dead-letters again, because the attempt count is already at the ceiling.
        if reset_attempts:
            job.attempts = 0
        job.dead_lettered_at = None
        job.error = None
        job.finished_at = None
        job.next_attempt_at = None
        job.current_stage = None
        await session.commit()
        logger.info(
            "automation.job_requeued",
            job_id=str(job.id),
            from_stage=str(from_stage) if from_stage else None,
        )
        return job

    # ---- sweeps ---------------------------------------------------------

    async def requeue_stale(self, session: AsyncSession) -> int:
        """Rescue jobs whose worker died.

        `acks_late` covers a task the broker still knows about. It does not cover a
        worker that acked, started, and was then SIGKILLed — that job sits at RUNNING
        forever with nobody looking at it. The heartbeat is what distinguishes it from
        a job legitimately spending twelve minutes on a large conversion.
        """
        cutoff = _now() - timedelta(seconds=self._settings.stale_job_seconds)
        stmt = select(AutomationJob).where(
            AutomationJob.status == JobStatus.RUNNING,
            (AutomationJob.heartbeat_at.is_(None)) | (AutomationJob.heartbeat_at < cutoff),
        )
        stale = list((await session.execute(stmt)).scalars().all())
        for job in stale:
            job.status = JobStatus.QUEUED
            job.current_stage = None
            job.next_attempt_at = None
            logger.warning(
                "automation.stale_job_requeued",
                job_id=str(job.id),
                heartbeat_at=job.heartbeat_at.isoformat() if job.heartbeat_at else None,
            )
        if stale:
            await session.commit()
        return len(stale)

    async def prune(self, session: AsyncSession) -> int:
        """Drop old finished jobs.

        Dead-lettered jobs are **kept**. They are the ones somebody still has to look
        at, and pruning the failures while retaining the successes is exactly backwards
        for a table whose reason to exist is explaining what went wrong.
        """
        cutoff = _now() - timedelta(days=self._settings.job_retention_days)
        stmt = select(AutomationJob).where(
            AutomationJob.finished_at.is_not(None),
            AutomationJob.finished_at < cutoff,
            AutomationJob.status.in_((JobStatus.SUCCEEDED, JobStatus.CANCELLED)),
        )
        stale = list((await session.execute(stmt)).scalars().all())
        for job in stale:
            await session.delete(job)
        if stale:
            await session.commit()
        return len(stale)

    async def stats(self, session: AsyncSession, *, days: int = 7) -> PipelineStats:
        since = _now() - timedelta(days=days)
        rows = (
            await session.execute(
                select(AutomationJob.status, func.count(AutomationJob.id))
                .where(AutomationJob.created_at >= since)
                .group_by(AutomationJob.status)
            )
        ).all()
        counts = {str(status): int(total) for status, total in rows}

        # Which stage fails most is the number that says what to fix first — the job
        # status only says that something did.
        failures = (
            await session.execute(
                select(JobStage.stage, func.count(JobStage.id))
                .join(AutomationJob, AutomationJob.id == JobStage.job_id)
                .where(
                    AutomationJob.created_at >= since,
                    JobStage.status == StageStatus.FAILED,
                )
                .group_by(JobStage.stage)
            )
        ).all()

        durations = (
            await session.execute(
                select(AutomationJob.started_at, AutomationJob.finished_at).where(
                    AutomationJob.created_at >= since,
                    AutomationJob.status == JobStatus.SUCCEEDED,
                    AutomationJob.started_at.is_not(None),
                    AutomationJob.finished_at.is_not(None),
                )
            )
        ).all()

        return PipelineStats(
            window_days=days,
            total=sum(counts.values()),
            succeeded=counts.get(JobStatus.SUCCEEDED, 0),
            failed=counts.get(JobStatus.FAILED, 0),
            dead_lettered=counts.get(JobStatus.DEAD_LETTERED, 0),
            running=counts.get(JobStatus.RUNNING, 0),
            queued=counts.get(JobStatus.QUEUED, 0) + counts.get(JobStatus.RETRYING, 0),
            failures_by_stage={str(stage): int(total) for stage, total in failures},
            median_duration_ms=_median_ms(durations),
        )


def _stage_selected(stage: Stage, options: JobOptions) -> bool:
    """Whether a job's options include this stage at all."""
    if options.only_stages:
        return stage in set(options.only_stages)
    return stage not in set(options.skip_stages)


def _median_ms(rows: Sequence[tuple[datetime | None, datetime | None]]) -> int | None:
    """Median, not mean.

    One 40-minute job on a 900-page scan drags a mean far away from what a typical
    run costs, and the typical run is what capacity planning needs.
    """
    values = sorted(
        int((finished - started).total_seconds() * 1000)
        for started, finished in rows
        if started and finished
    )
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) // 2

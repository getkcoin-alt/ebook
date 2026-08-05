"""The runner: order, resume, skip policy, failure policy.

Everything about *when* a stage runs lives here, and nothing about *what* it does.
That split is what makes a single stage re-runnable in isolation, and it is why the
stages do not know their own position in the pipeline.

## One task per job, not one per stage

The obvious design is a Celery chain — thirteen tasks, each queueing the next. This
runs the whole job in one task and checkpoints each stage to the database instead,
for three reasons:

* **The resume story is identical**, because it comes from the checkpoint either way.
  A chain does not resume from a checkpoint; it resumes from wherever the chain
  happened to break, which is only the same thing if every link also checkpoints.
* **A chain is thirteen chances to lose the thread.** Every hop is a broker round
  trip that can be redelivered, dropped past its visibility timeout, or orphaned when
  a worker dies between finishing one link and queueing the next. The failure mode is
  a job that is neither running nor failed, and nothing looking for it.
* **The source bytes stay in memory across stages.** A chain has to either re-download
  a 300MB file at every hop or pass it through the broker, and neither is acceptable
  — the first turns ten stages into ten downloads, the second puts a book in Redis.

The cost is that a single job holds one worker slot for its whole duration. That is
paid for with a queue dedicated to this work (`QUEUE_AUTOMATION`), so a twelve-minute
conversion never sits in front of a 200ms email.

## What happens when a stage fails

Three outcomes, and the distinction between them is the whole failure policy:

* **Skipped** — the stage decided there was nothing to do. Not an error. Recorded, and
  the pipeline continues.
* **Terminal** — the input is wrong and will be wrong next time. A corrupt PDF does
  not become readable on the fourth attempt. The job fails immediately; retrying
  would spend twenty minutes reaching the same answer.
* **Transient** — anything else. The job is checkpointed where it stands and requeued
  with jittered backoff, and it resumes from this stage rather than from the start.

An **optional** stage that fails transiently does not fail the job at all: it is
recorded as failed and the pipeline moves on. Blocking a publication on an AI
provider's bad afternoon turns one outage into a backlog nobody can clear.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import EventType, JobStatus, get_logger
from knowledgeos_core.events import EventPublisher
from knowledgeos_core.storage import ObjectStorage
from models import AutomationJob
from schemas import (
    AI_STAGES,
    PIPELINE,
    REQUIRED_STAGES,
    JobOptions,
    Stage,
    StageStatus,
)
from services import stages as stage_impl
from services.clients import PipelineClients
from services.jobs import JobService
from services.stages import HANDLERS, TERMINAL_ERRORS, StageContext, StageSkipped
from settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class RunOutcome:
    job_id: str
    status: JobStatus
    completed: list[str]
    skipped: list[str]
    failed: list[str]
    error: str | None = None


class PipelineRunner:
    def __init__(
        self,
        settings: Settings,
        jobs: JobService,
        clients: PipelineClients,
        storage: ObjectStorage | None = None,
        publisher: EventPublisher | None = None,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._clients = clients
        self._storage = storage
        self._publisher = publisher

    async def run(self, session: AsyncSession, job: AutomationJob) -> RunOutcome:
        """Execute one job from wherever it left off."""
        if job.is_terminal:
            # Queried rather than read off `job.completed_stages`: that property walks
            # a lazy relationship, and a lazy load under asyncio raises MissingGreenlet
            # on any job whose stages this session has not already fetched.
            done = await self._completed_stages(session, job)
            return RunOutcome(
                job_id=str(job.id),
                status=job.status,
                completed=[str(stage) for stage in PIPELINE if stage in done],
                skipped=[],
                failed=[],
                error="already finished",
            )

        options = JobOptions.model_validate(job.options or {})
        await self._jobs.mark_running(session, job)

        ctx = StageContext(
            job_id=job.id,
            book_id=job.book_id,
            source_key=job.source_key,
            original_filename=job.original_filename,
            options=options,
            settings=self._settings,
            storage=self._storage,
            clients=self._clients,
            result=dict(job.result or {}),
        )

        # The catalogue record, once, before anything reads it. The enrichment
        # stages prefer the editor's title and authors over the file's metadata, and
        # a publisher's PDF frequently carries the name of the InDesign template.
        book = await self._clients.get_book(job.book_id)
        if book:
            ctx.result["book"] = book

        completed = await self._completed_stages(session, job)
        skipped: list[str] = []
        failed: list[str] = []

        for stage in PIPELINE:
            if stage in completed and stage is not Stage.COLLECT:
                # COLLECT is the exception: its output is the source bytes, which
                # live in process memory and not in the checkpoint. A resumed run has
                # no payload until it runs again, and every later stage needs one.
                continue

            decision = self._should_run(stage, options)
            if decision is not None:
                await self._jobs.skip_stage(session, job, stage, reason=decision)
                skipped.append(str(stage))
                continue

            await self._jobs.begin_stage(session, job, stage)
            started = time.perf_counter()

            try:
                output = await HANDLERS[stage](ctx)
            except StageSkipped as exc:
                await self._jobs.skip_stage(session, job, stage, reason=exc.reason)
                skipped.append(str(stage))
                logger.info(
                    "automation.stage_skipped",
                    job_id=str(job.id),
                    stage=str(stage),
                    reason=exc.reason,
                )
                continue
            except TERMINAL_ERRORS as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                await self._jobs.fail_stage(
                    session, job, stage, error=str(exc), duration_ms=elapsed
                )
                if stage in REQUIRED_STAGES:
                    await self._jobs.fail_terminally(session, job, error=str(exc))
                    await self._emit_failed(job, stage, str(exc))
                    return RunOutcome(
                        job_id=str(job.id),
                        status=JobStatus.FAILED,
                        completed=[str(item) for item in completed],
                        skipped=skipped,
                        failed=[*failed, str(stage)],
                        error=str(exc),
                    )
                # A bad input to an optional stage is that stage's problem alone.
                failed.append(str(stage))
                continue
            except Exception as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                await self._jobs.fail_stage(
                    session, job, stage, error=str(exc), duration_ms=elapsed
                )
                if stage not in REQUIRED_STAGES:
                    failed.append(str(stage))
                    logger.warning(
                        "automation.optional_stage_failed",
                        job_id=str(job.id),
                        stage=str(stage),
                        error=str(exc),
                    )
                    continue

                # Required and transient: keep the checkpoint, back off, resume here.
                will_retry = await self._jobs.schedule_retry(session, job, error=str(exc))
                if not will_retry:
                    await self._emit_failed(job, stage, str(exc))
                return RunOutcome(
                    job_id=str(job.id),
                    status=job.status,
                    completed=[str(item) for item in completed],
                    skipped=skipped,
                    failed=[*failed, str(stage)],
                    error=str(exc),
                )

            elapsed = int((time.perf_counter() - started) * 1000)
            ctx.result.update(output)
            await self._jobs.complete_stage(
                session,
                job,
                stage,
                # Underscore-prefixed keys are transient: `_pending_uploads` carries
                # the artefact *bytes* between the stage that made them and the one
                # that writes them. Persisting those would put megabytes of binary
                # through a JSON column, and would fail before it got that far.
                output={key: value for key, value in output.items() if not key.startswith("_")},
                duration_ms=elapsed,
            )
            completed.add(stage)

        await self._jobs.succeed(session, job)
        await self._emit_completed(job, ctx.result)
        return RunOutcome(
            job_id=str(job.id),
            status=JobStatus.SUCCEEDED,
            completed=[str(stage) for stage in PIPELINE if stage in completed],
            skipped=skipped,
            failed=failed,
        )

    # ---- policy ---------------------------------------------------------

    def _should_run(self, stage: Stage, options: JobOptions) -> str | None:
        """Reason to skip, or None to run."""
        # COLLECT always runs even under `only_stages`, because every other stage
        # needs the source bytes and they are not in the checkpoint.
        if (
            options.only_stages
            and stage is not Stage.COLLECT
            and stage not in set(options.only_stages)
        ):
            return "not_in_only_stages"
        if stage in set(options.skip_stages):
            return "skipped_by_request"
        if stage in AI_STAGES and not self._settings.ai_enabled:
            return "ai_disabled"
        # `SEARCH_INDEXING_ENABLED=false` deliberately does *not* skip INDEX. That
        # stage also writes the catalogue patch, and skipping it would silently throw
        # away everything the pipeline produced. The client's index call is the part
        # that no-ops instead.
        return None

    async def _completed_stages(self, session: AsyncSession, job: AutomationJob) -> set[Stage]:
        rows = await self._jobs.stages(session, job.id)
        return {row.stage for row in rows if row.status is StageStatus.SUCCEEDED}

    # ---- events ---------------------------------------------------------

    async def _emit_completed(self, job: AutomationJob, result: dict) -> None:
        if self._publisher is None:
            return
        document = result.get("document") or {}
        await self._publisher.publish(
            EventType.AUTOMATION_JOB_COMPLETED,
            {
                "job_id": str(job.id),
                "book_id": str(job.book_id),
                "published": bool(result.get("published") is True),
                "page_count": document.get("page_count"),
                "format": (result.get("validation") or {}).get("format"),
            },
        )

    async def _emit_failed(self, job: AutomationJob, stage: Stage, error: str) -> None:
        if self._publisher is None:
            return
        await self._publisher.publish(
            EventType.AUTOMATION_JOB_FAILED,
            {
                "job_id": str(job.id),
                "book_id": str(job.book_id),
                "stage": str(stage),
                # Truncated: an event payload is a fact, not a stack trace, and the
                # stream has a memory bound that a full traceback per failure eats.
                "error": error[:500],
                "attempts": job.attempts,
            },
        )


#: Re-exported so callers do not reach into `stages` for it.
StageSkippedError = stage_impl.StageSkipped

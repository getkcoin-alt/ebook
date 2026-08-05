"""Service-to-service endpoints. HMAC-signed; private-network reachability is not
authorisation."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from deps import Jobs, Runner
from knowledgeos_core import JobStatus, MessageResponse, get_logger
from knowledgeos_core.deps import Ctx, DbSession, InternalCaller
from knowledgeos_core.logging import request_id_ctx
from schemas import JobCreate, JobDetail, JobOut, StageOut, SweepResult

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post(
    "/jobs",
    response_model=JobOut,
    summary="Queue a pipeline run (internal)",
    description=(
        "How the books service asks for an uploaded file to be processed, without "
        "the upload path having to know anything about stages or queues."
    ),
)
async def internal_create_job(
    payload: JobCreate,
    caller: InternalCaller,
    session: DbSession,
    jobs: Jobs,
    ctx: Ctx,
) -> JobOut:
    job = await jobs.create(session, payload, correlation_id=request_id_ctx.get())
    await ctx.extras["dispatch"](job.id)
    logger.info("automation.job_queued_by_service", job_id=str(job.id), caller=caller)
    return JobOut.model_validate(job)


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetail,
    summary="Job status (internal)",
    description=(
        "So the books service can show an editor where their upload has got to "
        "without proxying the whole operator API."
    ),
)
async def internal_get_job(
    job_id: uuid.UUID, caller: InternalCaller, session: DbSession, jobs: Jobs
) -> JobDetail:
    job = await jobs.get(session, job_id)
    return JobDetail(
        **JobOut.model_validate(job).model_dump(),
        stages=[StageOut.model_validate(row) for row in await jobs.stages(session, job_id)],
    )


@router.post(
    "/maintenance/drain",
    response_model=SweepResult,
    summary="Run due jobs (internal)",
    description=(
        "What the worker calls on a schedule. Claims jobs whose backoff has expired "
        "and runs them.\n\n"
        "Backoff lives in the job row rather than in a sleeping worker: a worker "
        "holding a task through a ten-minute backoff is a worker doing nothing while "
        "the queue grows behind it."
    ),
)
async def drain(
    caller: InternalCaller,
    session: DbSession,
    jobs: Jobs,
    runner: Runner,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
) -> SweepResult:
    due = await jobs.claim_due(session, limit=limit)
    ran = 0
    for job in due:
        try:
            await runner.run(session, job)
            ran += 1
        except Exception as exc:
            # The runner records the failure on the job itself; this catch exists so
            # one poisonous job does not stop the other four in the batch.
            logger.error("automation.drain_job_failed", job_id=str(job.id), error=str(exc))
    return SweepResult(retried=ran)


@router.post(
    "/maintenance/requeue-stale",
    response_model=SweepResult,
    summary="Rescue jobs whose worker died (internal)",
    description=(
        "`acks_late` covers a task the broker still knows about. It does not cover a "
        "worker that acked, started, and was then SIGKILLed — that job sits at "
        "RUNNING forever with nobody looking at it. The heartbeat is what "
        "distinguishes it from a job legitimately spending twelve minutes on a large "
        "conversion."
    ),
)
async def requeue_stale(caller: InternalCaller, session: DbSession, jobs: Jobs) -> SweepResult:
    return SweepResult(requeued_stale=await jobs.requeue_stale(session))


@router.post(
    "/maintenance/prune",
    response_model=SweepResult,
    summary="Drop old finished jobs (internal)",
    description=(
        "Dead-lettered jobs are **kept**. They are the ones somebody still has to "
        "look at, and pruning the failures while retaining the successes is exactly "
        "backwards for a table whose reason to exist is explaining what went wrong."
    ),
)
async def prune(caller: InternalCaller, session: DbSession, jobs: Jobs) -> SweepResult:
    return SweepResult(pruned_jobs=await jobs.prune(session))


@router.get(
    "/jobs",
    response_model=list[JobOut],
    summary="Jobs for a book (internal)",
    description="Lets the books service show processing state on an admin book page.",
)
async def internal_list_jobs(
    caller: InternalCaller,
    session: DbSession,
    jobs: Jobs,
    book_id: Annotated[uuid.UUID | None, Query()] = None,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[JobOut]:
    rows, _total = await jobs.list_jobs(
        session, limit=limit, offset=0, book_id=book_id, status=job_status
    )
    return [JobOut.model_validate(row) for row in rows]


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=MessageResponse,
    summary="Cancel a job (internal)",
    description="Called when a book is deleted while its file is still being processed.",
)
async def internal_cancel(
    job_id: uuid.UUID, caller: InternalCaller, session: DbSession, jobs: Jobs
) -> MessageResponse:
    job = await jobs.get(session, job_id)
    await jobs.cancel(session, job)
    return MessageResponse(message="Job cancelled.")

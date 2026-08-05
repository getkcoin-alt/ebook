"""Operator API for the ingestion pipeline.

Everything here needs `automation:run` or `automation:read`. There is no public
surface on this service at all: a customer has no reason to know that a pipeline
exists, and a job's stage history names storage keys and sibling services.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from deps import Imports, Jobs, PageOffset, Runner
from knowledgeos_core import ConflictError, JobStatus, get_logger
from knowledgeos_core.deps import Ctx, CurrentUser, DbSession, require_permission
from knowledgeos_core.logging import request_id_ctx
from schemas import (
    ImportCreate,
    ImportOut,
    ImportPage,
    JobActionResponse,
    JobCreate,
    JobDetail,
    JobOut,
    JobPage,
    PipelineStats,
    RetryRequest,
    StageOut,
)

logger = get_logger(__name__)

RUN = Depends(require_permission("automation:run"))
READ = Depends(require_permission("automation:read"))

router = APIRouter(prefix="/v1/automation", tags=["automation"])


def _detail(job, stages) -> JobDetail:  # type: ignore[no-untyped-def]
    return JobDetail(
        **JobOut.model_validate(job).model_dump(),
        stages=[StageOut.model_validate(row) for row in stages],
    )


@router.post(
    "/jobs",
    response_model=JobDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Queue a pipeline run",
    description=(
        "Takes a storage key, never a file body. A 400MB upload through the gateway "
        "is a request that cannot be retried and a proxy buffer nobody sized for it; "
        "by the time anyone asks for this, the file is already in object storage.\n\n"
        "Returns 409 if that file already has a live job. A double-clicked button "
        "must not run the pipeline twice — both runs would write derived artefacts "
        "and both would try to publish, with the loser overwriting the winner's "
        "output somewhere in the middle."
    ),
    dependencies=[RUN],
)
async def create_job(
    payload: JobCreate,
    session: DbSession,
    jobs: Jobs,
    runner: Runner,
    ctx: Ctx,
    user: CurrentUser,
) -> JobDetail:
    job = await jobs.create(
        session,
        payload,
        requested_by=uuid.UUID(user.user_id),
        correlation_id=request_id_ctx.get(),
    )
    await ctx.extras["dispatch"](job.id)
    return _detail(job, await jobs.stages(session, job.id))


@router.get(
    "/jobs",
    response_model=JobPage,
    summary="List jobs",
    dependencies=[READ],
)
async def list_jobs(
    session: DbSession,
    jobs: Jobs,
    page: PageOffset,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    book_id: Annotated[uuid.UUID | None, Query()] = None,
) -> JobPage:
    limit, offset = page
    rows, total = await jobs.list_jobs(
        session, limit=limit, offset=offset, status=job_status, book_id=book_id
    )
    return JobPage(
        items=[JobOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetail,
    summary="One job with its full stage history",
    description=(
        "The stage history is the answer to every question about a job: which stage "
        "it reached, what each produced, how long each took, and why the ones that "
        "did not run were skipped."
    ),
    dependencies=[READ],
)
async def get_job(job_id: uuid.UUID, session: DbSession, jobs: Jobs) -> JobDetail:
    job = await jobs.get(session, job_id)
    return _detail(job, await jobs.stages(session, job_id))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobActionResponse,
    summary="Requeue a settled job",
    description=(
        "Resumes from the last completed stage by default, so a job that died at "
        "stage 11 does not redo ten minutes of CPU and three dollars of tokens.\n\n"
        "Pass `from_stage` when a stage *succeeded* but produced something wrong — a "
        "description written from a bad excerpt, a cover taken from the wrong page. "
        "Without it the resume starts after the last success and reproduces exactly "
        "the same output."
    ),
    dependencies=[RUN],
)
async def retry_job(
    job_id: uuid.UUID,
    payload: RetryRequest,
    session: DbSession,
    jobs: Jobs,
    ctx: Ctx,
) -> JobActionResponse:
    job = await jobs.get(session, job_id)
    await jobs.prepare_retry(
        session, job, from_stage=payload.from_stage, reset_attempts=payload.reset_attempts
    )
    await ctx.extras["dispatch"](job.id)
    return JobActionResponse(id=job.id, status=job.status, message="Job requeued.")


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobActionResponse,
    summary="Cancel a job",
    description=(
        "Stops it being picked up again. A stage already executing on a worker runs "
        "to completion — there is no safe way to interrupt a transformation halfway "
        "and leave storage consistent."
    ),
    dependencies=[RUN],
)
async def cancel_job(job_id: uuid.UUID, session: DbSession, jobs: Jobs) -> JobActionResponse:
    job = await jobs.get(session, job_id)
    await jobs.cancel(session, job)
    return JobActionResponse(id=job.id, status=job.status, message="Job cancelled.")


@router.get(
    "/stats",
    response_model=PipelineStats,
    summary="Throughput and failures",
    description=(
        "`failures_by_stage` is the number that matters: the job status only says "
        "that something failed, and this says which stage to fix first.\n\n"
        "The duration is a **median**. One forty-minute job on a 900-page scan drags "
        "a mean far away from what a typical run costs, and the typical run is what "
        "capacity planning needs."
    ),
    dependencies=[READ],
)
async def stats(
    session: DbSession,
    jobs: Jobs,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> PipelineStats:
    return await jobs.stats(session, days=days)


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


@router.post(
    "/imports",
    response_model=ImportOut,
    status_code=status.HTTP_201_CREATED,
    summary="Bulk import books",
    description=(
        "**`dry_run` defaults to true.** An import is typically a spreadsheet edited "
        "by hand, and its errors are systematic — a shifted column, a decimal point "
        "in a price — so they are in every row. Finding that out from a validation "
        "report costs nothing; finding it out from four hundred wrong books costs an "
        "afternoon.\n\n"
        "Rows are independent. One bad row is reported with its line number and the "
        "rest proceed; a single typo on line 287 does not send an editor back to the "
        "start.\n\n"
        "A row with no `source_key` creates a catalogue entry and queues no job — a "
        "metadata-only backfill for books whose files arrive later."
    ),
    dependencies=[RUN],
)
async def create_import(
    payload: ImportCreate,
    session: DbSession,
    imports: Imports,
    ctx: Ctx,
    user: CurrentUser,
) -> ImportOut:
    outcome = await imports.run(session, payload, requested_by=uuid.UUID(user.user_id))
    for job_id in outcome.job_ids:
        await ctx.extras["dispatch"](job_id)
    return ImportOut(
        **ImportOut.model_validate(outcome.batch).model_dump(exclude={"job_ids"}),
        job_ids=outcome.job_ids,
    )


@router.get(
    "/imports",
    response_model=ImportPage,
    summary="List imports",
    dependencies=[READ],
)
async def list_imports(session: DbSession, imports: Imports, page: PageOffset) -> ImportPage:
    limit, offset = page
    rows, total = await imports.list_batches(session, limit=limit, offset=offset)
    return ImportPage(
        items=[ImportOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/imports/{import_id}",
    response_model=ImportOut,
    summary="One import with its row errors",
    dependencies=[READ],
)
async def get_import(import_id: uuid.UUID, session: DbSession, imports: Imports) -> ImportOut:
    batch = await imports.get(session, import_id)
    if batch is None:
        from knowledgeos_core import NotFoundError

        raise NotFoundError("Import not found.", details={"import_id": str(import_id)})
    return ImportOut.model_validate(batch)


@router.post(
    "/jobs/{job_id}/run",
    response_model=JobActionResponse,
    summary="Run a job now, in this process",
    description=(
        "Bypasses the queue and executes the pipeline inline. For development and "
        "for the one job an operator is watching — not for bulk work, because it "
        "holds an HTTP connection open for the duration of a conversion."
    ),
    dependencies=[RUN],
)
async def run_job_now(
    job_id: uuid.UUID, session: DbSession, jobs: Jobs, runner: Runner
) -> JobActionResponse:
    job = await jobs.get(session, job_id)
    if job.is_terminal:
        raise ConflictError("That job has already finished.", details={"status": str(job.status)})
    outcome = await runner.run(session, job)
    return JobActionResponse(
        id=job.id,
        status=outcome.status,
        message=(
            f"{len(outcome.completed)} stages completed, "
            f"{len(outcome.skipped)} skipped, {len(outcome.failed)} failed."
        ),
    )

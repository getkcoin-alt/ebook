"""Operator API for the scheduler.

Read access needs `analytics:read`; triggering a job needs `settings:write`. There is
no public surface — a customer has no reason to know the scheduler exists, and a run
record names internal paths on sibling services.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from schedule import BY_NAME

from deps import History, PageOffset, Runner
from knowledgeos_core import NotFoundError, get_logger
from knowledgeos_core.deps import (
    CurrentUser,
    DbSession,
    InternalCaller,
    require_permission,
)
from schemas import (
    JobOut,
    PruneResponse,
    RunOut,
    RunOutcome,
    RunPage,
    SchedulerHealth,
    TriggerResponse,
)

logger = get_logger(__name__)

READ = Depends(require_permission("analytics:read"))
WRITE = Depends(require_permission("settings:write"))

router = APIRouter(prefix="/v1/admin/workers", tags=["admin"])


@router.get(
    "/jobs",
    response_model=list[JobOut],
    summary="The schedule, with each job's recent health",
    description=(
        "Every entry names a sibling service and an HTTP path, never a table. A "
        "scheduler with direct database access to every schema is how a "
        "microservice platform quietly re-couples — the sweep that expires orders "
        "ends up encoding the payment service's state machine, and two deployables "
        "have to change together forever after."
    ),
    dependencies=[READ],
)
async def list_jobs(session: DbSession, history: History) -> list[JobOut]:
    return await history.list_jobs(session)


@router.get(
    "/jobs/{job_name}",
    response_model=JobOut,
    summary="One job",
    dependencies=[READ],
)
async def get_job(job_name: str, session: DbSession, history: History) -> JobOut:
    job = BY_NAME.get(job_name)
    if job is None:
        raise NotFoundError("No such scheduled job.", details={"job": job_name})
    return await history.describe(session, job)


@router.post(
    "/jobs/{job_name}/run",
    response_model=TriggerResponse,
    summary="Run a job now",
    description=(
        "Bypasses the schedule **and the single-flight lock**. An operator pressing "
        "this wants it to run now, not to be told that another replica might already "
        "be doing it — every sweep on this schedule is idempotent, so the cost of an "
        "overlap is duplicated work, not wrong data.\n\n"
        "The run is recorded like any other, with `triggered_by` set, so it is "
        "distinguishable from a scheduled firing when someone later asks why a sweep "
        "ran at an odd hour."
    ),
    dependencies=[WRITE],
)
async def trigger_job(
    job_name: str,
    session: DbSession,
    history: History,
    runner: Runner,
    user: CurrentUser,
) -> TriggerResponse:
    job = BY_NAME.get(job_name)
    if job is None:
        raise NotFoundError("No such scheduled job.", details={"job": job_name})

    actor = uuid.UUID(user.user_id)
    result = await runner.run(job, triggered_by=actor, ignore_lock=True)
    await history.record(session, runner.to_row(result, triggered_by=actor))

    logger.info(
        "workers.job_triggered",
        job=job.name,
        actor=str(actor),
        outcome=str(result.outcome),
    )
    return TriggerResponse(
        job_name=result.job_name,
        outcome=result.outcome,
        duration_ms=result.duration_ms,
        status_code=result.status_code,
        detail=result.detail,
        error=result.error,
    )


@router.get(
    "/runs",
    response_model=RunPage,
    summary="Run history",
    description=(
        "`skipped_locked` is a normal outcome, not a failure — it means another "
        "replica took the run and single-flight worked. A three-replica deployment "
        "would otherwise look like it is failing two runs in three."
    ),
    dependencies=[READ],
)
async def list_runs(
    session: DbSession,
    history: History,
    page: PageOffset,
    job_name: Annotated[str | None, Query()] = None,
    outcome: Annotated[RunOutcome | None, Query()] = None,
) -> RunPage:
    limit, offset = page
    rows, total = await history.runs(
        session, limit=limit, offset=offset, job_name=job_name, outcome=outcome
    )
    return RunPage(
        items=[RunOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/health",
    response_model=SchedulerHealth,
    summary="Whether the scheduler itself is alive",
    description=(
        "`stale_jobs` is the field worth alerting on. A job that has stopped running "
        "produces no logs and no failures — it is invisible in every signal except "
        "the absence of recent runs, which is exactly the failure a scheduler is "
        "prone to and exactly the one nobody notices for a week."
    ),
    dependencies=[READ],
)
async def scheduler_health(session: DbSession, history: History) -> SchedulerHealth:
    return await history.health(session)


internal_router = APIRouter(prefix="/internal", tags=["internal"])


@internal_router.post(
    "/maintenance/prune",
    response_model=PruneResponse,
    summary="Drop old run records (internal)",
    description=(
        "This service's own sweep, and the only one it runs against itself. A history "
        "table that grows forever is the same problem this service exists to solve "
        "for everyone else."
    ),
)
async def prune_runs(caller: InternalCaller, session: DbSession, history: History) -> PruneResponse:
    return PruneResponse(pruned=await history.prune(session))


@internal_router.get(
    "/health",
    response_model=SchedulerHealth,
    summary="Scheduler health (internal)",
    description="So the admin service can show scheduler state without a token.",
)
async def internal_health(
    caller: InternalCaller, session: DbSession, history: History
) -> SchedulerHealth:
    return await history.health(session)

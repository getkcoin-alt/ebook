"""Celery entrypoint and the dispatcher the API uses to hand work off.

The API never runs a pipeline inside a request. A conversion is minutes of CPU and an
HTTP request is not a place to spend them: the client times out, the proxy gives up,
a retry starts a second run, and nothing has a record of what happened. So creating a
job and running it are separate, and this module is the seam between them.

**Two dispatch modes, one interface.**

* `celery` — the production path. The job id goes to the broker and a worker on the
  `automation` queue picks it up. Only the id: passing the payload would put a book
  in Redis, and the job row is the source of truth anyway.
* `inline` — a background asyncio task in the API process. For local development and
  for tests, where standing up a broker to prove that stage 7 skips a scanned PDF is
  not a trade worth making.

Inline mode is **not** a production fallback dressed up as a feature. It runs a
long CPU-bound job on the event loop of the process serving requests, and there is a
warning at startup saying so.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable

from knowledgeos_core import get_logger
from knowledgeos_core.tasks import QUEUE_AUTOMATION, create_celery, install_task_observability
from settings import settings

logger = get_logger(__name__)

celery_app = create_celery(settings, name="automation")

#: Set of tasks currently running inline, held so the event loop does not garbage
#: collect a task nobody is awaiting — the classic way a fire-and-forget coroutine
#: disappears silently halfway through.
_INLINE: set[asyncio.Task] = set()


@celery_app.task(name="automation.run_job", bind=True, queue=QUEUE_AUTOMATION)
def run_job(self, job_id: str) -> dict:  # type: ignore[no-untyped-def]
    """Run one pipeline job to completion.

    One task per job rather than one per stage. The resume story comes from the
    database checkpoint either way, and a thirteen-link chain is thirteen chances for
    a redelivery, a visibility timeout or a dead worker to leave a job that is neither
    running nor failed with nothing looking for it. It also keeps the source bytes in
    memory across stages, instead of re-downloading a 300MB file at every hop.

    `asyncio.run` because the whole platform's data layer is async and Celery's worker
    is not. One loop per task, torn down with it, so nothing leaks between jobs.
    """
    return asyncio.run(_run_job_async(job_id))


async def _run_job_async(job_id: str) -> dict:
    from knowledgeos_core.db import Database
    from knowledgeos_core.http import ServiceRegistry
    from knowledgeos_core.storage import ObjectStorage
    from services import JobService, PipelineClients, PipelineRunner

    database = Database(settings)
    services = ServiceRegistry(settings)
    storage = ObjectStorage(settings)
    jobs = JobService(settings)
    runner = PipelineRunner(
        settings, jobs, PipelineClients(settings, services), storage, publisher=None
    )

    try:
        async with database.sessionmaker() as session:
            job = await jobs.get(session, uuid.UUID(job_id))
            outcome = await runner.run(session, job)
            return {
                "job_id": outcome.job_id,
                "status": str(outcome.status),
                "completed": outcome.completed,
                "skipped": outcome.skipped,
                "failed": outcome.failed,
            }
    finally:
        # Every worker process opens its own pool, and `worker_max_tasks_per_child`
        # recycles the process regularly. Not disposing here leaks a connection per
        # task until Postgres refuses new ones.
        await services.aclose()
        await database.dispose()


def build_dispatcher(ctx) -> Callable[[uuid.UUID], Awaitable[None]]:  # type: ignore[no-untyped-def]
    """Return the function the API calls to hand a job off.

    Chosen once at startup rather than per call, so the mode is visible in the boot
    log instead of being a per-request surprise.
    """
    if settings.inline_execution:

        async def _inline(job_id: uuid.UUID) -> None:
            task = asyncio.create_task(_run_inline(ctx, job_id))
            _INLINE.add(task)
            task.add_done_callback(_INLINE.discard)

        return _inline

    async def _queue(job_id: uuid.UUID) -> None:
        try:
            # `.delay` blocks on a socket write to Redis. Off the event loop, because
            # a broker that has gone slow would otherwise stall every request the
            # process is serving, not just this one.
            await asyncio.to_thread(run_job.delay, str(job_id))
        except Exception as exc:
            # The job row already exists and is QUEUED, so the worker's drain sweep
            # will find it. A failed enqueue costs latency, not the job.
            logger.error("automation.enqueue_failed", job_id=str(job_id), error=str(exc))

    return _queue


async def _run_inline(ctx, job_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    from services import JobService

    jobs: JobService = ctx.extras["jobs"]
    runner = ctx.extras["runner"]
    if ctx.database is None:
        return
    async with ctx.database.sessionmaker() as session:
        with contextlib.suppress(Exception):
            job = await jobs.get(session, job_id)
            await runner.run(session, job)


def bootstrap_worker() -> None:
    """Called from the worker entrypoint, not from the API."""
    install_task_observability(settings)

"""Celery beat schedule and the single task that executes it.

**One generic task, not one task per job.** The schedule is data (`schedule.py`), so a
new sweep is a row there rather than a function here, a registered task name, a beat
entry and a deploy of this service. The alternative accumulates thirteen nearly
identical functions that differ only in a URL, and the fourteenth is always
copy-pasted with one line unchanged.

The task takes the job **name**, not the job. A `ScheduledJob` sent through the broker
would be a snapshot of the schedule as it was when beat started — so a corrected
timeout or a disabled job would keep firing with its old configuration until beat
restarted. Resolving the name at execution time means the running code is always the
deployed schedule.
"""

from __future__ import annotations

import asyncio
import socket

from celery.schedules import crontab
from schedule import BY_NAME, enabled_jobs

from knowledgeos_core import get_logger
from knowledgeos_core.tasks import QUEUE_MAINTENANCE, create_celery, install_task_observability
from settings import settings

logger = get_logger(__name__)

celery_app = create_celery(settings, name="workers")


def build_beat_schedule() -> dict:
    """The beat entries, from the declarative schedule.

    Built at import time so a duplicate or malformed entry fails the process on boot
    rather than at three in the morning on the one job that reached it.
    """
    entries = {}
    for job in enabled_jobs(settings.disabled_job_set):
        entries[job.name] = {
            "task": "workers.run_scheduled",
            "schedule": crontab(minute=job.minute, hour=job.hour, day_of_week=job.day_of_week),
            "args": (job.name,),
            "options": {
                "queue": QUEUE_MAINTENANCE,
                # If beat was down when this should have fired, run it once on
                # recovery rather than replaying every missed tick. A worker that
                # comes back after an hour must not immediately fire twelve copies of
                # a five-minute sweep.
                "expires": max(60, int(job.timeout)),
            },
        }
    return entries


celery_app.conf.beat_schedule = build_beat_schedule()
celery_app.conf.beat_schedule_filename = settings.beat_schedule_path


@celery_app.task(
    name="workers.run_scheduled",
    bind=True,
    queue=QUEUE_MAINTENANCE,
    # The task never raises — the runner returns its outcome — so autoretry would
    # never fire anyway. Switched off explicitly so nobody has to work that out.
    autoretry_for=(),
    max_retries=0,
)
def run_scheduled(self, job_name: str) -> dict:  # type: ignore[no-untyped-def]
    """Run one scheduled job and record the outcome.

    `asyncio.run` because the platform's data and HTTP layers are async and Celery's
    worker is not. One loop per task, torn down with it, so nothing leaks between runs.
    """
    return asyncio.run(_run_async(job_name))


async def _run_async(job_name: str) -> dict:
    from knowledgeos_core.db import Database
    from knowledgeos_core.http import ServiceRegistry
    from knowledgeos_core.redis import RedisClient
    from services import HistoryService, JobRunner

    job = BY_NAME.get(job_name)
    if job is None:
        # The schedule changed under a beat that has not restarted. Not an error
        # worth retrying — the entry is simply gone.
        logger.warning("workers.unknown_job", job=job_name)
        return {"job": job_name, "outcome": "unknown"}

    database = Database(settings)
    services = ServiceRegistry(settings)
    redis = RedisClient(settings)
    runner = JobRunner(settings, services, redis, worker_id=socket.gethostname())
    history = HistoryService(settings)

    try:
        result = await runner.run(job)
        async with database.sessionmaker() as session:
            await history.record(session, runner.to_row(result))
        return {
            "job": job.name,
            "outcome": str(result.outcome),
            "duration_ms": result.duration_ms,
            "status": result.status_code,
        }
    finally:
        # `worker_max_tasks_per_child` recycles the process regularly, and every task
        # opens its own pool. Not disposing here leaks a connection per task until
        # Postgres refuses new ones.
        await services.aclose()
        await redis.close()
        await database.dispose()


def bootstrap_worker() -> None:
    """Called from the worker and beat entrypoints, not from the API."""
    install_task_observability(settings)
    logger.info(
        "workers.schedule_loaded",
        jobs=len(celery_app.conf.beat_schedule),
        disabled=sorted(settings.disabled_job_set),
    )

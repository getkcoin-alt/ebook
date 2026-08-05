"""Workers service entrypoint.

The platform's scheduler. It calls `/internal/maintenance/*` on the services that own
the work, on a schedule, and records what happened.

**It never touches another service's tables.** That is the whole design. A scheduler
with direct database access to every schema is how a microservice platform quietly
re-couples — the sweep that expires orders ends up encoding the payment service's
state machine, and the constraint that makes schema-per-service worth anything (only
one service writes to a schema, ADR 0002) is broken by the one process nobody thinks
of as a service.

This process serves the operator API. The schedule is executed by Celery beat and a
worker, both started from `tasks.py`:

    celery -A tasks.celery_app beat
    celery -A tasks.celery_app worker -Q maintenance

Running the API without them is a perfectly valid deployment — you just have no
scheduler, which `/v1/admin/workers/health` will say clearly rather than leaving
someone to discover it a week later.
"""

from __future__ import annotations

import socket

from schedule import SCHEDULE, enabled_jobs

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import admin_router, internal_router
from services import HistoryService, JobRunner
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    ctx.extras.update(
        {
            "history": HistoryService(settings),
            "runner": JobRunner(settings, ctx.services, ctx.redis, worker_id=socket.gethostname()),
        }
    )

    active = enabled_jobs(settings.disabled_job_set)
    if disabled := sorted(settings.disabled_job_set):
        # Logged rather than left implicit: "why has the reconcile not run" is a
        # question whose answer is usually in this list.
        logger.info("workers.jobs_disabled", jobs=disabled)

    unknown = settings.disabled_job_set - {job.name for job in SCHEDULE}
    if unknown:
        # A typo in DISABLED_JOBS silently disables nothing, and the sweep someone
        # meant to switch off keeps running.
        logger.warning("workers.unknown_disabled_jobs", jobs=sorted(unknown))

    logger.info("workers.ready", scheduled=len(active), total=len(SCHEDULE))


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        auth=True,
        service_clients=True,
    ),
    routers=[admin_router, internal_router],
    on_startup=[_bootstrap],
    description=(
        "Platform scheduler: drives each service's maintenance sweeps over HTTP and "
        "records what ran."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

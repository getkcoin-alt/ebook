"""Automation service entrypoint.

A book goes in as a file in object storage; a prepared, described, tagged, indexed
product comes out. Thirteen stages, each checkpointed, so a failure at stage 11
resumes at stage 11 rather than redoing ten minutes of CPU and three dollars of
tokens.

The two decisions that shape everything else:

**Required stages block; enrichment stages do not.** A file that failed validation is
not publishable. A book with no AI tags is. Blocking a publication on a model
provider's bad afternoon turns one outage into a backlog nobody can clear.

**Nothing publishes itself by default.** `AUTO_PUBLISH` is off, so a finished job
leaves the book ready for a human to approve. A pipeline that publishes whatever it
is handed puts machine-written copy in front of customers with nobody having read it,
and the first time a model hallucinates an award into a description, it is on the
storefront.
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import internal_router, jobs_router
from services import (
    AutomationEventHandler,
    ImportService,
    JobService,
    PipelineClients,
    PipelineRunner,
    register_consumers,
)
from settings import settings
from tasks import build_dispatcher

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    jobs = JobService(settings)
    clients = PipelineClients(settings, ctx.services)
    runner = PipelineRunner(settings, jobs, clients, ctx.storage, publisher=ctx.publisher)

    ctx.extras.update(
        {
            "jobs": jobs,
            "clients": clients,
            "runner": runner,
            "imports": ImportService(settings, jobs, clients),
            "dispatch": build_dispatcher(ctx),
        }
    )

    if ctx.consumer is not None and ctx.database is not None:
        register_consumers(
            ctx.consumer, ctx.database.sessionmaker, AutomationEventHandler(settings, jobs)
        )
        logger.info("automation.event_consumer_wired")

    if settings.inline_execution:
        # Loud, because inline mode runs a CPU-bound job on the loop serving
        # requests. It is a development convenience, not a way to run without a
        # worker fleet.
        logger.warning(
            "automation.inline_execution",
            hint="Jobs run in the API process. Set INLINE_EXECUTION=false in production.",
        )
    if settings.auto_publish:
        logger.warning(
            "automation.auto_publish_enabled",
            hint="Finished jobs publish without human review.",
        )
    if not settings.ai_enabled:
        logger.info("automation.ai_stages_disabled")

    logger.info(
        "automation.ready",
        accepted_formats=settings.accepted_format_set,
        max_source_mb=settings.max_source_bytes // (1024 * 1024),
        auto_publish=settings.auto_publish,
    )


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        storage=True,
        auth=True,
        events=True,
        event_consumer=settings.event_consumer_group if settings.events_enabled else None,
        service_clients=True,
    ),
    routers=[jobs_router, internal_router],
    on_startup=[_bootstrap],
    description=(
        "Ingestion pipeline: collect, validate, extract, enrich, transform, upload, "
        "index and publish."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

"""Search service entrypoint.

Full-text search, autocomplete, facets, related books, trending and search
analytics — backed by Meilisearch.

**The index is a cache, not a source of truth.** Every document in it can be rebuilt
from the books service. That is what makes it safe to drop and rebuild an index
during an incident, and it is why nothing here is the authoritative record of
anything except analytics.

**A search outage is not a platform outage.** Meilisearch being unreachable produces
clean 503s on the query path and nothing else: the service still starts, still passes
its liveness probe, and still serves `/v1/admin/search/health` so an operator can see
what is wrong. Index setup at boot is best-effort for the same reason — a search
engine that is down must not stop the service from starting.
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import admin_router, internal_router, search_router
from services import (
    AnalyticsService,
    CatalogueGateway,
    Indexer,
    MeiliClient,
    QueryService,
    TrendingService,
    assert_filters_are_indexable,
    register_handlers,
)
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    # Fails fast, at import-adjacent time, if a filter the API accepts was never
    # declared filterable on the index. The alternative is a 400 from Meilisearch
    # surfacing as a 500 on a live search, for one facet nobody tested.
    assert_filters_are_indexable()

    meili = MeiliClient(settings)
    catalogue = (
        CatalogueGateway(ctx.services, page_size=settings.reconcile_page_size)
        if ctx.services is not None
        else None
    )
    indexer = Indexer(
        settings,
        meili,
        ctx.require_db().sessionmaker,
        catalogue=catalogue,
    )

    ctx.extras.update(
        {
            "meili": meili,
            "catalogue": catalogue,
            "indexer": indexer,
            "queries": QueryService(settings, meili),
            "trending": TrendingService(ctx.require_redis().client, settings),
            "analytics": AnalyticsService(settings),
        }
    )

    # Best-effort: creates the indexes and pushes their configuration if the engine
    # is reachable, and logs and moves on if it is not.
    await indexer.ensure_indexes()

    # Catalogue changes arrive as events rather than as a call from the books
    # service: an index that is briefly stale is a much smaller problem than a
    # publish that fails because search was mid-deploy.
    if ctx.consumer is not None:
        register_handlers(ctx.consumer, indexer)
        logger.info("search.index_consumer_wired")

    logger.info(
        "search.ready",
        indexes=list(settings.index_names),
        semantic=settings.semantic_enabled,
        analytics=settings.analytics_enabled,
        trending=settings.trending_enabled,
    )


async def _shutdown(ctx: AppContext) -> None:
    meili = ctx.extras.get("meili")
    if meili is not None:
        await meili.aclose()


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        auth=True,
        events=True,
        event_consumer=settings.event_consumer_group if settings.events_enabled else None,
        # Reconciliation walks the books service's own listing endpoints; the index
        # is never rebuilt from another service's schema.
        service_clients=True,
    ),
    routers=[search_router, admin_router, internal_router],
    on_startup=[_bootstrap],
    on_shutdown=[_shutdown],
    description=(
        "Full-text search, autocomplete, facets, related books, trending queries "
        "and search analytics, backed by Meilisearch."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

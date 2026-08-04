"""Book service entrypoint.

The catalogue, the reader's shelf, and the entitlements that decide who may open
which file. Highest read traffic on the platform.
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import (
    admin_router,
    authors_router,
    catalogue_router,
    categories_router,
    entitlements_router,
    internal_router,
    library_router,
    lists_router,
    publishers_router,
    reading_router,
    reviews_router,
)
from services import (
    CatalogueCache,
    CatalogueService,
    EntitlementEventHandler,
    EntitlementService,
    ListService,
    ReadingService,
    ReviewService,
    TaxonomyService,
    register_consumers,
)
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    cache = CatalogueCache(ctx.redis, settings)
    entitlements = EntitlementService(settings)

    ctx.extras.update(
        {
            "cache": cache,
            "catalogue": CatalogueService(settings, cache),
            "taxonomy": TaxonomyService(settings, cache),
            "reviews": ReviewService(settings),
            "reading": ReadingService(settings),
            "lists": ListService(settings),
            "entitlements": entitlements,
        }
    )

    # Purchases arrive as events, not as a synchronous call from payment: if payment
    # called us directly while we were mid-deploy, a customer would have paid for a
    # book they cannot open. The handler is idempotent on event id.
    if ctx.consumer is not None and ctx.database is not None:
        register_consumers(
            ctx.consumer,
            ctx.database.sessionmaker,
            EntitlementEventHandler(settings, entitlements),
        )
        logger.info("books.entitlement_consumer_wired")

    logger.info("books.ready", cache_enabled=cache.enabled)


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        storage=True,
        auth=True,
        events=True,
        event_consumer=settings.event_consumer_group if settings.events_enabled else None,
    ),
    routers=[
        catalogue_router,
        library_router,
        reviews_router,
        reading_router,
        lists_router,
        authors_router,
        publishers_router,
        categories_router,
        admin_router,
        entitlements_router,
        internal_router,
    ],
    on_startup=[_bootstrap],
    description=(
        "Catalogue, authors, categories, reviews, bookmarks, reading progress, "
        "wishlists, collections and the entitlements that gate downloads."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

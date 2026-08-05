"""Admin service entrypoint.

The operator's view of the platform, and the home of feature flags.

**It owns almost no data.** The dashboard is assembled from the services that own the
numbers — revenue is the payment service's figure, index health is the search
service's. Storing a second copy here would be a second copy that drifts, and when the
two disagree nobody can say which is right.

The two tables it does own are feature flags and their audit history, because every
service reads flags and none owns them. Putting them in any one service's schema would
make that service a dependency of every other for a reason unrelated to its domain.

The security decision worth knowing: this service **forwards the operator's own bearer
token** when it fans out. The alternative — calling siblings under its own HMAC
identity — makes it a confused deputy, handing anyone who can reach an admin endpoint
data from every service regardless of what they may see in those services.
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import dashboard_router, flags_router, internal_router
from services import Aggregator, FlagService
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    ctx.extras.update(
        {
            "aggregator": Aggregator(settings, ctx.services, ctx.redis),
            "flags": FlagService(settings, ctx.redis),
        }
    )

    if ctx.redis is None:
        # Loud, because every dashboard render then fans out to every service. A few
        # operators with the page open produce more internal traffic than the
        # storefront.
        logger.warning(
            "admin.no_cache",
            hint="Redis is not configured; every dashboard render fans out live.",
        )

    logger.info("admin.ready", panels=settings.panel_services)


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        auth=True,
        service_clients=True,
    ),
    routers=[dashboard_router, flags_router, internal_router],
    on_startup=[_bootstrap],
    description=(
        "Operator dashboard aggregated across every service, plus platform feature flags."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

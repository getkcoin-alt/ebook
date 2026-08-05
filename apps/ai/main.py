"""AI service entrypoint.

Generated book copy, a catalogue-grounded assistant, recommendations, content
moderation and embeddings.

**This is the only service on the platform that fails by spending money.** Every
other one fails by becoming unavailable, which is loud and obvious. A retry loop
against a paid model is silent until the invoice arrives, so the daily cost ceiling
is a hard stop rather than a target: reaching it returns 503 and serves nothing. An
AI feature that is off for the rest of the day is a worse product; an uncapped one is
a worse business.

The service also degrades honestly. No provider configured, budget exhausted, or a
provider rate-limited — each produces a clear 503 or a flagged fallback, never a
silent empty answer that looks like the model had nothing to say.
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import admin_router, assistant_router, internal_router
from services import (
    BudgetService,
    ChatService,
    Generator,
    ModerationService,
    ProviderRegistry,
    ReportingService,
)
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    providers = ProviderRegistry(settings)
    budget = BudgetService(settings)
    generator = Generator(settings, providers, budget, ctx.redis)

    ctx.extras.update(
        {
            "providers": providers,
            "budget": budget,
            "reporting": ReportingService(settings),
            "generator": generator,
            "moderation": ModerationService(settings, generator),
            "chat": ChatService(settings, generator, providers, budget, ctx.services),
        }
    )

    if not providers.available:
        # Not fatal — the service boots, reports itself unavailable through
        # /v1/ai/status, and returns clean 503s. A half-configured deployment
        # should pass its health check rather than crash-loop.
        logger.warning(
            "ai.no_provider_configured",
            hint="Set ANTHROPIC_API_KEY or OPENAI_API_KEY; AI features return 503 until then.",
        )

    logger.info(
        "ai.ready",
        providers=providers.available,
        daily_limit_usd=settings.daily_cost_limit_usd,
        cache_enabled=settings.cache_enabled,
        moderation=settings.moderation_enabled,
        moderation_fail_closed=settings.moderation_fail_closed,
    )


async def _shutdown(ctx: AppContext) -> None:
    providers = ctx.extras.get("providers")
    if providers is not None:
        await providers.aclose()


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        auth=True,
        events=True,
        # Chat grounds its answers in real catalogue results, and book-scoped
        # conversations check entitlement with the books service first.
        service_clients=True,
    ),
    routers=[assistant_router, admin_router, internal_router],
    on_startup=[_bootstrap],
    on_shutdown=[_shutdown],
    description=(
        "Generated book copy, a catalogue-grounded assistant, recommendations, "
        "moderation and embeddings — with a hard daily cost ceiling."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

"""Notification service entrypoint.

Email, SMS, WhatsApp, push and in-app messages — templates, preferences, delivery
tracking and suppression.

Almost nothing calls this service directly. It **consumes platform events**: a user
registers, an order is paid, a refund is issued, and this service decides those facts
deserve a message. That indirection is the point — the auth service should not know
what a welcome email says, and the payment service should not know that a receipt
exists, or every service grows its own copy of the platform's tone of voice.

The one rule that outranks everything else here: an address on the suppression list
is never contacted again, transactional or not. Continuing to mail an address that
issued a spam complaint costs the sending domain its reputation, and that takes every
other message down with it — password resets included.
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import admin_router, inbox_router, internal_router
from services import (
    ChannelRegistry,
    Dispatcher,
    InboxService,
    NotificationEventHandler,
    PreferenceService,
    TemplateService,
    register_consumers,
)
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    channels = ChannelRegistry(settings)
    templates = TemplateService(settings)
    preferences = PreferenceService(settings)
    dispatcher = Dispatcher(settings, templates, preferences, channels)

    ctx.extras.update(
        {
            "channels": channels,
            "templates": templates,
            "preferences": preferences,
            "dispatcher": dispatcher,
            "inbox": InboxService(settings),
        }
    )

    if ctx.consumer is not None and ctx.database is not None:
        register_consumers(
            ctx.consumer,
            ctx.database.sessionmaker,
            NotificationEventHandler(settings, dispatcher),
        )
        logger.info("notifications.event_consumer_wired")

    if not settings.sending_enabled:
        # Loud, because a deployment that silently stops sending looks healthy from
        # every angle except the customer's.
        logger.warning("notifications.sending_disabled", hint="SENDING_ENABLED=false")
    if settings.email_provider == "console" and settings.is_production:
        logger.warning(
            "notifications.console_email_in_production",
            hint="Set EMAIL_PROVIDER=smtp or resend; nothing is actually being delivered.",
        )
    if settings.unsubscribe_secret.startswith("dev-"):
        logger.warning(
            "notifications.default_unsubscribe_secret",
            hint="Set UNSUBSCRIBE_SECRET; unsubscribe links are forgeable without it.",
        )

    logger.info(
        "notifications.ready",
        channels=settings.enabled_channels,
        email_provider=settings.email_provider,
        sending_enabled=settings.sending_enabled,
    )


async def _shutdown(ctx: AppContext) -> None:
    channels = ctx.extras.get("channels")
    if channels is not None:
        await channels.aclose()


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        auth=True,
        events=True,
        event_consumer=settings.event_consumer_group if settings.events_enabled else None,
    ),
    routers=[inbox_router, admin_router, internal_router],
    on_startup=[_bootstrap],
    on_shutdown=[_shutdown],
    description=(
        "Email, SMS, WhatsApp, push and in-app notifications with templates, "
        "per-category preferences, delivery tracking and suppression."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

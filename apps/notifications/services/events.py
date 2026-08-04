"""Turning platform events into messages.

This is the service's main input. Almost nothing calls the notification API
directly: a user registers, an order is paid, a refund is issued, and *this* module
decides that those facts deserve a message.

That indirection is the point. The auth service should not know what a welcome email
says, and the payment service should not know that a receipt exists — otherwise
every service grows its own copy of the platform's tone of voice, and changing a
subject line becomes a five-service deploy.

**Idempotency is mandatory.** Redis Streams deliver at least once, so a redelivered
``order.paid`` must not send a second receipt. The ``processed_events`` ledger row is
written in the same transaction as the effect: either both land or neither does.
Writing it afterwards drops the message when the process dies in between; committing
it separately sends a duplicate.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledgeos_core import Event, EventConsumer, EventType, NotificationChannel, get_logger
from models import ProcessedEvent
from schemas import SendRequest
from services.dispatcher import Dispatcher
from settings import Settings

logger = get_logger(__name__)

#: Event type -> (template key, channels). One table rather than a handler per
#: event: the mapping *is* the logic, and a table can be read at a glance.
ROUTES: dict[str, tuple[str, tuple[NotificationChannel, ...]]] = {
    EventType.USER_REGISTERED: (
        "account.welcome",
        (NotificationChannel.IN_APP, NotificationChannel.EMAIL),
    ),
    EventType.USER_VERIFIED: ("account.verified", (NotificationChannel.IN_APP,)),
    EventType.PASSWORD_RESET_REQUESTED: (
        "account.password_reset",
        # Email only, deliberately: someone locked out of their account cannot
        # read an in-app notification, and putting a reset link in one would leave
        # it sitting in a session that may not be theirs.
        (NotificationChannel.EMAIL,),
    ),
    EventType.ORDER_PAID: (
        "order.receipt",
        (NotificationChannel.IN_APP, NotificationChannel.EMAIL),
    ),
    EventType.ORDER_REFUNDED: (
        "order.refund",
        (NotificationChannel.IN_APP, NotificationChannel.EMAIL),
    ),
    EventType.PAYMENT_FAILED: (
        "order.payment_failed",
        (NotificationChannel.IN_APP, NotificationChannel.EMAIL),
    ),
    EventType.SUBSCRIPTION_ACTIVATED: (
        "subscription.activated",
        (NotificationChannel.IN_APP, NotificationChannel.EMAIL),
    ),
    EventType.SUBSCRIPTION_CANCELLED: (
        "subscription.cancelled",
        (NotificationChannel.IN_APP, NotificationChannel.EMAIL),
    ),
    EventType.BOOK_PUBLISHED: ("book.published", (NotificationChannel.IN_APP,)),
    EventType.AUTOMATION_JOB_FAILED: ("automation.failed", (NotificationChannel.IN_APP,)),
    #: The generic escape hatch: a service that wants a specific message asks for
    #: one by name rather than inventing an event type for it.
    EventType.NOTIFICATION_REQUESTED: ("", ()),
}


@dataclass(slots=True)
class Instruction:
    """A normalised "send this to that person" derived from an event."""

    user_id: uuid.UUID
    template_key: str
    channels: tuple[NotificationChannel, ...]
    variables: dict[str, Any]
    email: str | None = None
    phone: str | None = None


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def build_instruction(event: Event) -> Instruction | None:
    """Map one event onto a send, or ``None`` when it warrants no message."""
    payload = event.payload or {}
    user_id = _as_uuid(payload.get("user_id"))
    if user_id is None:
        return None

    if event.type == EventType.NOTIFICATION_REQUESTED:
        # An explicit request carries its own template and channels.
        template_key = str(payload.get("template_key") or "")
        if not template_key:
            return None
        raw_channels = payload.get("channels") or ["in_app"]
        channels = tuple(
            NotificationChannel(channel)
            for channel in raw_channels
            if channel in set(NotificationChannel)
        )
    else:
        route = ROUTES.get(event.type)
        if route is None:
            return None
        template_key, channels = route
        if not template_key:
            return None

    # The whole payload becomes the variable set, so a template can reference
    # anything the producing service saw fit to publish without this module
    # needing to know the shape of every event.
    variables = {key: value for key, value in payload.items() if not key.startswith("_")}
    variables.setdefault("user_id", str(user_id))

    return Instruction(
        user_id=user_id,
        template_key=template_key,
        channels=channels or (NotificationChannel.IN_APP,),
        variables=variables,
        email=str(payload.get("email") or "") or None,
        phone=str(payload.get("phone") or "") or None,
    )


class NotificationEventHandler:
    def __init__(self, settings: Settings, dispatcher: Dispatcher) -> None:
        self._settings = settings
        self._dispatcher = dispatcher

    async def claim(self, session: AsyncSession, event: Event) -> bool:
        """Record the event id. ``False`` means it was already processed."""
        existing = await session.get(ProcessedEvent, event.id)
        if existing is not None:
            return False
        session.add(ProcessedEvent(event_id=event.id, event_type=event.type))
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # Two replicas racing the same redelivery. The loser stops here.
            logger.info("event.duplicate", event_id=event.id, event_type=event.type)
            return False
        return True

    async def handle(self, session: AsyncSession, event: Event) -> bool:
        """Process one event. Returns whether a message was sent."""
        if not await self.claim(session, event):
            return False

        instruction = build_instruction(event)
        if instruction is None:
            # Marked processed regardless: an event we cannot act on will not become
            # actionable on redelivery, and leaving it unacknowledged has it retried
            # five times and then dead-lettered for no reason.
            await session.commit()
            logger.debug("notification.event_ignored", event_type=event.type)
            return False

        try:
            await self._dispatcher.send(
                session,
                SendRequest(
                    user_id=instruction.user_id,
                    template_key=instruction.template_key,
                    variables=instruction.variables,
                    channels=list(instruction.channels),
                    email=instruction.email,
                    phone=instruction.phone,
                ),
            )
        except Exception as exc:
            # A missing template must not dead-letter the event and block the
            # consumer group behind it. The ledger row commits, so this is not
            # retried — the fix is to add the template, then replay deliberately.
            await session.rollback()
            async with session.begin():
                session.add(ProcessedEvent(event_id=event.id, event_type=event.type))
            logger.warning(
                "notification.event_send_failed",
                event_type=event.type,
                template_key=instruction.template_key,
                error=str(exc),
            )
            return False

        logger.info(
            "notification.event_handled",
            event_type=event.type,
            template_key=instruction.template_key,
            user_id=str(instruction.user_id),
        )
        return True


def register_consumers(
    consumer: EventConsumer,
    sessionmaker: async_sessionmaker[AsyncSession],
    handler: NotificationEventHandler,
) -> Callable[[Event], Awaitable[None]]:
    """Subscribe one handler to every routed event, with a session per event."""

    async def _handle(event: Event) -> None:
        async with sessionmaker() as session:
            try:
                await handler.handle(session, event)
            except Exception:
                await session.rollback()
                # Re-raised so the consumer leaves it unacknowledged and XAUTOCLAIM
                # redelivers; the ledger row rolled back with it, so the retry is
                # not mistaken for a duplicate.
                raise

    for event_type in ROUTES:
        consumer.on(event_type)(_handle)
    return _handle

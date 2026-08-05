"""Sending a notification.

One path, used by every caller — the internal API, the event consumers and the
retry sweep all go through :meth:`Dispatcher.send`. A second path would eventually
skip a preference check or a suppression check, and the failure mode is mailing
someone who asked you not to.

The order of operations is the design:

1. Resolve the template. No template, nothing to send.
2. Render it. **Missing variables abort the send** — a customer receiving
   ``Hi {{first_name}},`` is worse than a customer receiving nothing, because the
   first is an apology and the second is an alert.
3. Ask :class:`PreferenceService` whether this channel may be used. A refusal is
   recorded as a `skipped` delivery with the reason, so "why did they not get their
   receipt?" is answerable from the database.
4. Send, record the outcome, and schedule a retry only when retrying could help.

**In-app is written first, and separately.** It is a database row rather than a
network call, so it is the channel most likely to succeed — and having the message
visible in the app while the email is still retrying is the right failure mode.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import NotFoundError, NotificationChannel, ValidationError, get_logger
from models import Delivery, DeviceToken, Notification
from schemas import DeliveryStatus, SendRequest, SuppressionReason
from services.channels import ChannelRegistry, Message
from services.preferences import PreferenceService
from services.templates import RenderedMessage, TemplateService, render_message
from settings import Settings

logger = get_logger(__name__)

#: Channels that need a destination the caller has to supply. In-app does not:
#: the user id is the destination.
NEEDS_DESTINATION = {
    NotificationChannel.EMAIL,
    NotificationChannel.SMS,
    NotificationChannel.WHATSAPP,
    NotificationChannel.PUSH,
}


@dataclass(slots=True)
class DispatchResult:
    notification: Notification | None = None
    deliveries: list[Delivery] = field(default_factory=list)
    skipped_reason: str | None = None


def unsubscribe_token(secret: str, user_id: uuid.UUID) -> str:
    """A signed, self-verifying unsubscribe handle.

    ``<user_id>.<hmac>``. The signature is what stops a URL containing an id being
    editable into someone else's unsubscribe — which is otherwise a one-line way to
    silence every customer on the platform.
    """
    digest = hmac.new(secret.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{digest}"


def verify_unsubscribe_token(secret: str, token: str) -> uuid.UUID | None:
    raw_id, _, signature = token.partition(".")
    if not signature:
        return None
    expected = hmac.new(secret.encode(), raw_id.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        return uuid.UUID(raw_id)
    except ValueError:
        return None


class Dispatcher:
    def __init__(
        self,
        settings: Settings,
        templates: TemplateService,
        preferences: PreferenceService,
        channels: ChannelRegistry,
    ) -> None:
        self._settings = settings
        self._templates = templates
        self._preferences = preferences
        self._channels = channels

    # ---- destinations ----------------------------------------------------

    async def _push_tokens(self, session: AsyncSession, user_id: uuid.UUID) -> list[str]:
        stmt = select(DeviceToken.token).where(
            DeviceToken.user_id == user_id, DeviceToken.is_active.is_(True)
        )
        return list((await session.execute(stmt)).scalars().all())

    def _destination_for(self, channel: NotificationChannel, request: SendRequest) -> str | None:
        if channel is NotificationChannel.EMAIL:
            return request.email or str(request.variables.get("email") or "") or None
        if channel in (NotificationChannel.SMS, NotificationChannel.WHATSAPP):
            return request.phone or str(request.variables.get("phone") or "") or None
        return None

    # ---- the send --------------------------------------------------------

    async def send(self, session: AsyncSession, request: SendRequest) -> DispatchResult:
        """Render and dispatch one message across the requested channels.

        Commits. Callers are event handlers and HTTP requests that own nothing else
        in the transaction, and a partially-recorded send is worse than a
        fully-recorded one.
        """
        # Coerced back to real enum members before anything compares them.
        # BaseSchema sets use_enum_values=True, so a `channels` list the caller
        # actually sent arrives as plain strings — and every `is` comparison below
        # would silently never match, which looks exactly like "email is disabled".
        channels = [
            NotificationChannel(channel)
            for channel in (
                request.channels or [NotificationChannel.IN_APP, NotificationChannel.EMAIL]
            )
        ]
        variables = dict(request.variables)
        variables.setdefault("support_email", self._settings.support_email)
        variables.setdefault("from_name", self._settings.from_name)
        # Every account email has to link back into the app, and none of the events
        # carry a base URL — they carry a bare `reset_token` or `verification_token`.
        # Without this each template has to hardcode the domain, so moving the
        # frontend means editing every row instead of one variable.
        variables.setdefault("frontend_url", str(self._settings.frontend_url).rstrip("/"))

        unsubscribe_url = (
            f"{self._settings.unsubscribe_url.rstrip('/')}"
            f"?token={unsubscribe_token(self._settings.unsubscribe_secret, request.user_id)}"
        )
        variables.setdefault("unsubscribe_url", unsubscribe_url)

        result = DispatchResult()
        category = "general"
        template = None
        rendered = None

        # In-app first: it is a row rather than a network call, so it is the channel
        # most likely to land. A message visible in the app while the email retries
        # is the right way round.
        if NotificationChannel.IN_APP in channels:
            # Tolerates a missing in-app template the same way the other channels
            # do. A receipt that has an email body but no in-app one must still
            # reach the customer's inbox — failing the whole send because one
            # channel has no copy written for it is the wrong trade.
            template = await self._templates.find(
                session,
                key=request.template_key,
                channel=NotificationChannel.IN_APP,
                locale=request.locale,
            )
            rendered = None
            if template is not None:
                rendered = self._render_or_raise(template, request, variables)
                category = template.category

        if rendered is not None and template is not None:
            decision = await self._preferences.may_send(
                session,
                user_id=request.user_id,
                category=category,
                channel=NotificationChannel.IN_APP,
                destination=None,
            )
            if decision.allowed:
                result.notification = Notification(
                    user_id=request.user_id,
                    template_key=request.template_key,
                    category=category,
                    title=(rendered.subject or request.template_key)[:300],
                    body=rendered.body_text,
                    action_url=str(variables.get("action_url") or "") or None,
                    icon=str(variables.get("icon") or "") or None,
                    data=variables,
                )
                session.add(result.notification)
                await session.flush()
            else:
                result.skipped_reason = decision.reason

        found_any_template = template is not None
        for channel in channels:
            if channel is NotificationChannel.IN_APP:
                continue
            deliveries, had_template = await self._dispatch_channel(
                session, request, channel, variables, notification=result.notification
            )
            found_any_template = found_any_template or had_template
            result.deliveries.extend(deliveries)

        if not found_any_template:
            # No template for *any* requested channel. That is a configuration
            # error in the calling service, not a message the customer declined,
            # and it needs to be loud — a silent success here means a receipt
            # nobody ever notices is missing.
            raise NotFoundError(
                "No active template for that key on any requested channel.",
                details={
                    "template_key": request.template_key,
                    "channels": [str(channel) for channel in channels],
                },
            )
        if not result.notification and not result.deliveries:
            result.skipped_reason = result.skipped_reason or "no_channel_available"

        await session.commit()
        logger.info(
            "notification.sent",
            user_id=str(request.user_id),
            template_key=request.template_key,
            category=category,
            channels=[str(delivery.channel) for delivery in result.deliveries],
            in_app=result.notification is not None,
        )
        return result

    def _render_or_raise(
        self, template: object, request: SendRequest, variables: dict
    ) -> RenderedMessage:
        """Render, refusing to return a message with an unfilled placeholder.

        A customer seeing a raw ``{{first_name}}`` is a support ticket and an
        embarrassment; a 422 here is a bug report with the missing names attached.
        """
        rendered = render_message(template, variables)  # type: ignore[arg-type]
        if rendered.missing:
            raise ValidationError(
                "The template is missing required variables.",
                details={
                    "template_key": request.template_key,
                    "missing": list(rendered.missing),
                },
            )
        return rendered

    async def _dispatch_channel(
        self,
        session: AsyncSession,
        request: SendRequest,
        channel: NotificationChannel,
        variables: dict,
        *,
        notification: Notification | None,
    ) -> tuple[list[Delivery], bool]:
        """Returns the deliveries and whether a template existed for this channel.

        The caller needs the second value to tell "the user declined this channel"
        apart from "nobody ever wrote copy for it".
        """
        provider = self._channels.get(channel)
        if provider is None:
            logger.info("notification.channel_unavailable", channel=str(channel))
            return [], False

        template = await self._templates.find(
            session, key=request.template_key, channel=channel, locale=request.locale
        )
        if template is None:
            # A template for one channel and not another is normal — a push message
            # has no HTML body and a receipt has no push variant.
            return [], False
        rendered = self._render_or_raise(template, request, variables)

        destinations: list[str] = []
        if channel is NotificationChannel.PUSH:
            destinations = await self._push_tokens(session, request.user_id)
        elif destination := self._destination_for(channel, request):
            destinations = [destination]

        if not destinations and channel in NEEDS_DESTINATION:
            logger.info(
                "notification.no_destination", channel=str(channel), user_id=str(request.user_id)
            )
            return [], True

        deliveries: list[Delivery] = []
        for destination in destinations:
            decision = await self._preferences.may_send(
                session,
                user_id=request.user_id,
                category=template.category,
                channel=channel,
                destination=destination,
            )
            delivery = Delivery(
                notification_id=notification.id if notification else None,
                user_id=request.user_id,
                channel=channel,
                destination=destination[:320],
                subject=(rendered.subject or "")[:300] or None,
                body_text=rendered.body_text,
                body_html=rendered.body_html,
                status=DeliveryStatus.QUEUED,
            )
            session.add(delivery)
            await session.flush()

            if not decision.allowed:
                # Recorded rather than dropped: a support question about a missing
                # email is answered from this row.
                delivery.status = DeliveryStatus.SKIPPED
                delivery.error = decision.reason
                deliveries.append(delivery)
                continue

            await self._attempt(session, delivery, rendered, variables)
            deliveries.append(delivery)

        return deliveries, True

    async def _attempt(
        self,
        session: AsyncSession,
        delivery: Delivery,
        rendered: RenderedMessage,
        variables: dict,
    ) -> Delivery:
        provider = self._channels.get(delivery.channel)
        if provider is None:
            delivery.status = DeliveryStatus.SKIPPED
            delivery.error = "channel_unavailable"
            return delivery

        delivery.status = DeliveryStatus.SENDING
        delivery.attempts += 1
        message = Message(
            destination=delivery.destination,
            subject=rendered.subject,
            body_text=rendered.body_text,
            body_html=rendered.body_html,
            metadata={
                "unsubscribe_url": variables.get("unsubscribe_url"),
                "action_url": variables.get("action_url"),
                "icon": variables.get("icon"),
            },
        )
        result = await provider.send(message)
        delivery.provider = result.provider

        if result.success:
            delivery.status = DeliveryStatus.SENT
            delivery.sent_at = datetime.now(UTC)
            delivery.next_attempt_at = None
            delivery.error = None
            if result.provider_message_id:
                await self._set_provider_id(session, delivery, result.provider_message_id)
            return delivery

        delivery.error = (result.error or "")[:1000] or None

        exhausted = delivery.attempts >= self._settings.max_delivery_attempts
        if result.permanent or exhausted:
            delivery.status = DeliveryStatus.BOUNCED
            delivery.failed_at = datetime.now(UTC)
            delivery.next_attempt_at = None
            if result.permanent and delivery.channel is NotificationChannel.EMAIL:
                # The receiving server already said this mailbox does not exist.
                # Asking again looks like a dictionary attack and is how a sending
                # domain gets blocklisted.
                await self._preferences.suppress(
                    session,
                    channel=delivery.channel,
                    destination=delivery.destination,
                    reason=SuppressionReason.HARD_BOUNCE,
                    detail=delivery.error,
                    user_id=delivery.user_id,
                )
            if result.permanent and delivery.channel is NotificationChannel.PUSH:
                await self._deactivate_token(session, delivery.destination)
            logger.warning(
                "notification.delivery_failed_permanently",
                channel=str(delivery.channel),
                attempts=delivery.attempts,
                permanent=result.permanent,
            )
            return delivery

        delivery.status = DeliveryStatus.FAILED
        # Exponential backoff. A provider having a bad minute should not receive the
        # same volume of retries a second later.
        delay = self._settings.retry_base_seconds * (2 ** (delivery.attempts - 1))
        delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        logger.info(
            "notification.delivery_retry_scheduled",
            channel=str(delivery.channel),
            attempts=delivery.attempts,
            retry_in_seconds=delay,
        )
        return delivery

    async def _set_provider_id(
        self, session: AsyncSession, delivery: Delivery, provider_message_id: str
    ) -> None:
        """Record the provider's id, tolerating a collision.

        ``(provider, provider_message_id)`` is unique so a redelivered status
        webhook maps to one row. The console provider derives its id from the
        message, so two identical sends genuinely collide — which is not worth
        failing a delivery over.
        """
        savepoint = await session.begin_nested()
        delivery.provider_message_id = provider_message_id[:191]
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError:
            await savepoint.rollback()
            delivery.provider_message_id = None
            logger.debug("notification.provider_id_collision", provider_id=provider_message_id)

    async def _deactivate_token(self, session: AsyncSession, token: str) -> None:
        """Retire a push token the provider rejected.

        Continuing to push to uninstalled apps gets a sender throttled by FCM.
        """
        stmt = select(DeviceToken).where(DeviceToken.token == token)
        device = (await session.execute(stmt)).scalars().one_or_none()
        if device is not None:
            device.is_active = False
            await session.flush()

    # ---- retries ---------------------------------------------------------

    async def retry_pending(self, session: AsyncSession, *, batch: int = 100) -> int:
        """Re-attempt deliveries whose backoff has elapsed.

        Driven by the worker. Retrying inside the request that failed would make a
        customer wait out an exponential backoff before their page loads.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Delivery)
            .where(
                Delivery.status == DeliveryStatus.FAILED,
                Delivery.next_attempt_at.is_not(None),
                Delivery.next_attempt_at <= now,
            )
            .order_by(Delivery.next_attempt_at)
            .limit(batch)
        )
        pending = list((await session.execute(stmt)).scalars().all())

        for delivery in pending:
            # Re-sent from the stored body, so a retry is the same message the
            # first attempt was — not a re-render against variables that may have
            # changed, and not a truncated preview.
            rendered = RenderedMessage(
                subject=delivery.subject,
                body_text=delivery.body_text,
                body_html=delivery.body_html,
            )
            await self._attempt(session, delivery, rendered, {})

        if pending:
            await session.commit()
            logger.info("notification.retry_batch", count=len(pending))
        return len(pending)

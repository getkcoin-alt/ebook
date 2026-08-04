"""Webhook ingestion.

A webhook endpoint is a public URL that grants access to paid files. Everything here
follows from that.

**Order of operations, and why it is not negotiable:**

1. Verify the signature over the *raw request body*. Not the parsed JSON — re-serialising
   changes bytes (key order, whitespace, unicode escaping) and the signature will not
   match, so a correct implementation has to hash what actually arrived.
2. Record the event, verified or not. An unverified event is evidence: a burst of them
   is someone probing the endpoint, and that is worth being able to see.
3. Reject unverified events *before* any state changes.
4. Deduplicate on ``(provider, event_id)``. Providers redeliver aggressively — a
   successful payment webhook may arrive five times.
5. Process, and record the outcome on the event row.

**Always answer 2xx once the signature verifies**, even if processing failed. A 5xx
makes the provider retry with backoff for days, which turns one broken handler into
a stampede. Failures are recorded on the row and replayed by an operator, on purpose,
after a fix.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import EventPublisher, PaymentProvider, get_logger
from models import WebhookEvent
from schemas import PaymentStatus
from services.affiliates import AffiliateService
from services.orders import OrderService
from services.payments import PaymentService
from services.providers import GatewayRegistry, NormalisedEvent
from services.refunds import RefundService
from services.subscriptions import SubscriptionService
from settings import Settings

logger = get_logger(__name__)

#: Event names that mean "the money arrived", across both providers.
SUCCESS_EVENTS = {
    "payment.captured",
    "order.paid",
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "payment_intent.succeeded",
}

#: Event names that mean the attempt failed.
FAILURE_EVENTS = {
    "payment.failed",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
    "payment_intent.payment_failed",
    "payment_intent.canceled",
}


@dataclass(slots=True)
class WebhookOutcome:
    accepted: bool
    duplicate: bool = False
    event_id: str | None = None
    #: Set when the event settled an order, so the caller can publish afterwards.
    settled_order_id: uuid.UUID | None = None


class WebhookService:
    def __init__(
        self,
        settings: Settings,
        gateways: GatewayRegistry,
        orders: OrderService,
        payments: PaymentService,
        refunds: RefundService,
        subscriptions: SubscriptionService,
        affiliates: AffiliateService,
    ) -> None:
        self._settings = settings
        self._gateways = gateways
        self._orders = orders
        self._payments = payments
        self._refunds = refunds
        self._subscriptions = subscriptions
        self._affiliates = affiliates

    # ---- persistence ----------------------------------------------------

    async def _record(
        self,
        session: AsyncSession,
        *,
        provider: PaymentProvider,
        event: NormalisedEvent,
        payload: dict,
        signature_valid: bool,
    ) -> tuple[WebhookEvent, bool]:
        """Store the event. Returns ``(row, is_new)``.

        The unique constraint on ``(provider, event_id)`` is what makes processing
        exactly-once despite at-least-once delivery, so the insert races are resolved
        by the database rather than by a check-then-act in Python.
        """
        # The SAVEPOINT is opened *before* the row is added. begin_nested() autoflushes
        # pending state first, so adding the row beforehand would emit the INSERT
        # outside the savepoint — and the IntegrityError would escape this try block
        # and take the surrounding transaction with it.
        savepoint = await session.begin_nested()
        row = WebhookEvent(
            provider=provider,
            event_id=event.event_id or f"unsigned:{uuid.uuid4()}",
            event_type=event.event_type,
            signature_valid=signature_valid,
            payload=payload,
        )
        session.add(row)
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError:
            await savepoint.rollback()
            existing = (
                (
                    await session.execute(
                        select(WebhookEvent).where(
                            WebhookEvent.provider == provider,
                            WebhookEvent.event_id == event.event_id,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if existing is None:
                raise
            return existing, False
        return row, True

    # ---- ingestion ------------------------------------------------------

    async def ingest(
        self,
        session: AsyncSession,
        *,
        provider: PaymentProvider,
        body: bytes,
        headers: dict[str, str],
        payload: dict,
        publisher: EventPublisher | None = None,
    ) -> WebhookOutcome:
        """Verify, record, deduplicate and process one webhook.

        Commits, because the provider must not be able to re-trigger work we already
        did just because our response was slow to reach it.
        """
        gateway = self._gateways.get(provider)
        signature_valid = gateway.verify_webhook(body=body, headers=headers)
        event = gateway.parse_webhook(payload, headers)

        row, is_new = await self._record(
            session,
            provider=provider,
            event=event,
            payload=payload,
            signature_valid=signature_valid,
        )

        if not signature_valid:
            # Kept as evidence, then refused. Committing the row before returning
            # means the attempt is visible even though nothing was processed.
            await session.commit()
            logger.warning(
                "webhook.signature_invalid",
                provider=provider.value,
                event_type=event.event_type,
            )
            return WebhookOutcome(accepted=False, event_id=event.event_id)

        if not is_new and row.processed:
            await session.commit()
            logger.info("webhook.duplicate", provider=provider.value, event_id=event.event_id)
            return WebhookOutcome(accepted=True, duplicate=True, event_id=event.event_id)

        settled_order_id: uuid.UUID | None = None
        try:
            settled_order_id = await self._process(session, provider=provider, event=event)
            row.processed = True
            row.processed_at = datetime.now(UTC)
            row.error = None
        except Exception as exc:
            # Roll back the effects, keep the record, and let an operator replay it.
            # Retrying at the provider's discretion would hammer a handler that is
            # deterministically broken.
            await session.rollback()
            row = await self._reload_or_recreate(session, provider, event, payload)
            row.processed = False
            row.error = str(exc)[:2000]
            await session.commit()
            logger.exception(
                "webhook.processing_failed",
                provider=provider.value,
                event_id=event.event_id,
                event_type=event.event_type,
            )
            return WebhookOutcome(accepted=True, event_id=event.event_id)

        await session.commit()
        logger.info(
            "webhook.processed",
            provider=provider.value,
            event_id=event.event_id,
            event_type=event.event_type,
            settled=settled_order_id is not None,
        )
        return WebhookOutcome(
            accepted=True, event_id=event.event_id, settled_order_id=settled_order_id
        )

    async def _reload_or_recreate(
        self,
        session: AsyncSession,
        provider: PaymentProvider,
        event: NormalisedEvent,
        payload: dict,
    ) -> WebhookEvent:
        """After a rollback the event row is gone with everything else. Put it back
        so the failure is recorded rather than silently lost."""
        existing = (
            (
                await session.execute(
                    select(WebhookEvent).where(
                        WebhookEvent.provider == provider, WebhookEvent.event_id == event.event_id
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            return existing
        row = WebhookEvent(
            provider=provider,
            event_id=event.event_id or f"failed:{uuid.uuid4()}",
            event_type=event.event_type,
            signature_valid=True,
            payload=payload,
        )
        session.add(row)
        return row

    # ---- dispatch -------------------------------------------------------

    async def _process(
        self, session: AsyncSession, *, provider: PaymentProvider, event: NormalisedEvent
    ) -> uuid.UUID | None:
        """Route a normalised event. Returns the order id if one was settled."""
        if event.category == "refund":
            await self._handle_refund(session, provider=provider, event=event)
            return None
        if event.category == "subscription":
            await self._handle_subscription(session, provider=provider, event=event)
            return None
        if event.category == "payment":
            return await self._handle_payment(session, provider=provider, event=event)
        logger.info("webhook.ignored", provider=provider.value, event_type=event.event_type)
        return None

    async def _handle_payment(
        self, session: AsyncSession, *, provider: PaymentProvider, event: NormalisedEvent
    ) -> uuid.UUID | None:
        order = await self._orders.find_by_provider_reference(
            session,
            provider=provider,
            provider_order_id=event.provider_order_id,
            order_id=event.order_id,
        )
        if order is None:
            # Not ours, or a test event from the provider's dashboard. Recorded and
            # ignored rather than treated as an error.
            logger.warning(
                "webhook.order_not_found",
                provider=provider.value,
                provider_order_id=event.provider_order_id,
                event_type=event.event_type,
            )
            return None

        succeeded = event.event_type in SUCCESS_EVENTS or event.status == PaymentStatus.CAPTURED
        if succeeded and event.provider_payment_id:
            result = await self._payments.settle(
                session,
                order=order,
                provider=provider,
                provider_payment_id=event.provider_payment_id,
                amount_minor=event.amount_minor,
                currency=event.currency,
                method=event.method,
                raw_payload=event.raw,
            )
            return order.id if result.newly_paid else None

        if event.event_type in FAILURE_EVENTS or event.status == PaymentStatus.FAILED:
            await self._payments.fail(
                session,
                order=order,
                provider=provider,
                provider_payment_id=event.provider_payment_id,
                reason=event.failure_message,
                failure_code=event.failure_code,
                amount_minor=event.amount_minor,
                raw_payload=event.raw,
            )
            return None

        # Intermediate states — authorized, processing — are recorded so the payment
        # row tracks the attempt, but they do not settle anything.
        if event.provider_payment_id and event.status is not None:
            await self._payments.record_payment(
                session,
                order=order,
                provider=provider,
                provider_payment_id=event.provider_payment_id,
                status=event.status,
                amount_minor=event.amount_minor or order.total_minor,
                currency=event.currency or order.currency,
                method=event.method,
                raw_payload=event.raw,
            )
        return None

    async def _handle_refund(
        self, session: AsyncSession, *, provider: PaymentProvider, event: NormalisedEvent
    ) -> None:
        if not event.provider_refund_id:
            return
        failed = "fail" in event.event_type
        await self._refunds.mark_settled(
            session,
            provider=provider,
            provider_refund_id=event.provider_refund_id,
            succeeded=not failed,
            raw_payload=event.raw,
        )

    async def _handle_subscription(
        self, session: AsyncSession, *, provider: PaymentProvider, event: NormalisedEvent
    ) -> None:
        if not event.provider_subscription_id:
            return
        obj = (event.raw.get("data", {}) or {}).get("object", {}) or {}
        await self._subscriptions.apply_provider_status(
            session,
            provider=provider,
            provider_subscription_id=event.provider_subscription_id,
            provider_status=str(obj.get("status") or event.event_type.rsplit(".", 1)[-1]),
            current_period_start=_epoch(obj.get("current_period_start")),
            current_period_end=_epoch(obj.get("current_period_end")),
        )

    # ---- replay ---------------------------------------------------------

    async def replay(self, session: AsyncSession, webhook_event_id: uuid.UUID) -> WebhookOutcome:
        """Re-run a stored event after fixing whatever made it fail.

        Only ever runs against an event whose signature already verified — a replay
        must not become a way to launder an unsigned payload into the system.
        """
        row = await session.get(WebhookEvent, webhook_event_id)
        if row is None or not row.signature_valid:
            return WebhookOutcome(accepted=False)

        gateway = self._gateways.get(row.provider)
        event = gateway.parse_webhook(row.payload, {})
        settled = await self._process(session, provider=row.provider, event=event)
        row.processed = True
        row.processed_at = datetime.now(UTC)
        row.error = None
        await session.commit()
        logger.info("webhook.replayed", webhook_event_id=str(webhook_event_id))
        return WebhookOutcome(accepted=True, event_id=row.event_id, settled_order_id=settled)

    async def list_events(
        self,
        session: AsyncSession,
        *,
        provider: PaymentProvider | None = None,
        processed: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WebhookEvent]:
        conditions = []
        if provider is not None:
            conditions.append(WebhookEvent.provider == provider)
        if processed is not None:
            conditions.append(WebhookEvent.processed.is_(processed))
        stmt = (
            select(WebhookEvent)
            .where(*conditions)
            .order_by(WebhookEvent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all())


def _epoch(value: object) -> datetime | None:
    """Stripe sends period boundaries as Unix seconds."""
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None

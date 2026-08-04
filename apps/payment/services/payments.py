"""Settlement: turning a gateway callback into a paid order.

This is the most safety-critical path in the service, and everything about it is
built around one fact: **a payment confirmation can arrive more than once**. Razorpay
and Stripe both redeliver webhooks, the browser posts its own verification when the
checkout modal closes, and an operator may replay a stored event. All three land
here.

Idempotency is enforced in three independent places, deliberately:

1. ``payments`` is unique on ``(provider, provider_payment_id)`` — the database
   refuses to record one capture twice.
2. :meth:`OrderService.mark_paid` returns ``False`` if the order was already settled,
   so the side effects run exactly once.
3. Every downstream effect is itself idempotent — the invoice is looked up before it
   is issued, the affiliate conversion is unique per order, and the entitlement grant
   on the books side is unique per ``(user, book, source, order)``.

Any one of the three would usually be enough. Together they mean no single mistake
double-grants a book or double-issues an invoice.

**Events are published after the transaction commits.** Publishing inside it would
announce a payment that then rolled back, and a book granted for money that was
never taken is far worse than a book granted a second late.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    EventPublisher,
    EventType,
    OrderStatus,
    PaymentProvider,
    get_logger,
)
from models import Invoice, Order, Payment
from schemas import PaymentStatus
from services.affiliates import AffiliateService
from services.invoices import InvoiceService
from services.orders import OrderService
from settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class SettlementResult:
    """What actually happened, so the caller knows whether to announce it."""

    order: Order
    payment: Payment | None = None
    invoice: Invoice | None = None
    #: False when this confirmation was a duplicate. The caller publishes nothing.
    newly_paid: bool = False


class PaymentService:
    def __init__(
        self,
        settings: Settings,
        orders: OrderService,
        invoices: InvoiceService,
        affiliates: AffiliateService,
    ) -> None:
        self._settings = settings
        self._orders = orders
        self._invoices = invoices
        self._affiliates = affiliates

    # ---- payment rows ---------------------------------------------------

    async def find_payment(
        self, session: AsyncSession, *, provider: PaymentProvider | str, provider_payment_id: str
    ) -> Payment | None:
        stmt = select(Payment).where(
            Payment.provider == provider, Payment.provider_payment_id == provider_payment_id
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def record_payment(
        self,
        session: AsyncSession,
        *,
        order: Order,
        provider: PaymentProvider,
        provider_payment_id: str,
        status: PaymentStatus,
        amount_minor: int,
        currency: str,
        method: str | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        raw_payload: dict | None = None,
    ) -> tuple[Payment, bool]:
        """Insert or update the payment row. Returns ``(payment, created)``.

        On redelivery the existing row is *updated* rather than duplicated, because
        a provider legitimately sends the same payment id at ``authorized`` and again
        at ``captured``. Those are the same payment moving forward, not two payments.
        """
        existing = await self.find_payment(
            session, provider=provider, provider_payment_id=provider_payment_id
        )
        if existing is not None:
            existing.status = status
            existing.method = method or existing.method
            existing.failure_code = failure_code or existing.failure_code
            existing.failure_message = failure_message or existing.failure_message
            if raw_payload:
                existing.raw_payload = raw_payload
            if status == PaymentStatus.CAPTURED and existing.captured_at is None:
                existing.captured_at = datetime.now(UTC)
            await session.flush()
            return existing, False

        # A SAVEPOINT: two webhook deliveries can race between the SELECT above and
        # this INSERT, and losing that race must not roll back the settlement.
        #
        # Opened *before* the row is added: begin_nested() autoflushes pending state
        # first, so adding the payment beforehand would emit the INSERT outside the
        # savepoint and the IntegrityError would escape the try below.
        savepoint = await session.begin_nested()
        payment = Payment(
            order_id=order.id,
            user_id=order.user_id,
            provider=provider,
            provider_payment_id=provider_payment_id,
            status=status,
            amount_minor=amount_minor,
            currency=currency,
            method=method,
            failure_code=failure_code,
            failure_message=failure_message,
            raw_payload=raw_payload or {},
            captured_at=datetime.now(UTC) if status == PaymentStatus.CAPTURED else None,
        )
        session.add(payment)
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError:
            await savepoint.rollback()
            settled = await self.find_payment(
                session, provider=provider, provider_payment_id=provider_payment_id
            )
            if settled is None:
                raise
            logger.info(
                "payment.duplicate_insert_resolved", provider_payment_id=provider_payment_id
            )
            return settled, False
        return payment, True

    # ---- settlement -----------------------------------------------------

    async def settle(
        self,
        session: AsyncSession,
        *,
        order: Order,
        provider: PaymentProvider,
        provider_payment_id: str,
        amount_minor: int | None = None,
        currency: str | None = None,
        method: str | None = None,
        raw_payload: dict | None = None,
    ) -> SettlementResult:
        """Mark an order paid and run every downstream effect exactly once.

        Does not commit — the caller owns the transaction, so the payment row, the
        order status, the invoice and the affiliate credit either all land or none
        of them do.
        """
        charged = amount_minor if amount_minor is not None else order.total_minor

        # An amount that disagrees with the order is recorded and flagged, not
        # rejected: the money has already moved, and refusing to record it would
        # leave a real payment with no row anywhere. Reconciliation is a human job.
        if charged != order.total_minor:
            logger.error(
                "payment.amount_mismatch",
                order_id=str(order.id),
                order_total_minor=order.total_minor,
                charged_minor=charged,
                provider=provider.value,
            )

        payment, _created = await self.record_payment(
            session,
            order=order,
            provider=provider,
            provider_payment_id=provider_payment_id,
            status=PaymentStatus.CAPTURED,
            amount_minor=charged,
            currency=(currency or order.currency).upper(),
            method=method,
            raw_payload=raw_payload,
        )

        newly_paid = await self._orders.mark_paid(session, order)
        if not newly_paid:
            logger.info("payment.settlement_skipped_duplicate", order_id=str(order.id))
            return SettlementResult(order=order, payment=payment, newly_paid=False)

        invoice = await self._invoices.for_order(session, order)
        await self._affiliates.record(session, order)

        return SettlementResult(order=order, payment=payment, invoice=invoice, newly_paid=True)

    async def fail(
        self,
        session: AsyncSession,
        *,
        order: Order,
        provider: PaymentProvider,
        provider_payment_id: str | None,
        reason: str | None = None,
        failure_code: str | None = None,
        amount_minor: int | None = None,
        raw_payload: dict | None = None,
    ) -> Order:
        """Record a failed attempt.

        The attempt is kept even though it did not settle: a customer whose card was
        declined three times and then complains is answered from this table.
        """
        if provider_payment_id:
            await self.record_payment(
                session,
                order=order,
                provider=provider,
                provider_payment_id=provider_payment_id,
                status=PaymentStatus.FAILED,
                amount_minor=amount_minor if amount_minor is not None else order.total_minor,
                currency=order.currency,
                failure_code=failure_code,
                failure_message=reason,
                raw_payload=raw_payload,
            )
        return await self._orders.mark_failed(session, order, reason=reason)

    # ---- announcements --------------------------------------------------

    @staticmethod
    def order_event_payload(order: Order) -> dict:
        """The payload the rest of the platform reacts to.

        ``book_ids`` is the field the books service grants access from, and
        ``order_id`` is what makes that grant idempotent across redeliveries. Both
        are load-bearing — this is a cross-service contract, not a log line.
        """
        return {
            "order_id": str(order.id),
            "order_number": order.order_number,
            "user_id": str(order.user_id),
            "book_ids": [str(item.book_id) for item in order.items],
            "items": [
                {
                    "book_id": str(item.book_id),
                    "title": item.title,
                    "quantity": item.quantity,
                    "unit_price_minor": item.unit_price_minor,
                }
                for item in order.items
            ],
            "total_minor": order.total_minor,
            "currency": order.currency,
            "provider": order.provider.value if order.provider else None,
            "email": order.billing_email,
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        }

    async def announce_paid(
        self, publisher: EventPublisher | None, result: SettlementResult
    ) -> None:
        """Publish after the commit. Never inside the transaction.

        A failed publish is logged rather than raised: the money has been taken and
        the order is settled in the database. Re-raising here would turn a delivered
        purchase into a 500 for the customer, and the webhook would be redelivered
        anyway — the database, not the event, is the source of truth.
        """
        if publisher is None or not result.newly_paid:
            return
        payload = self.order_event_payload(result.order)
        if result.invoice is not None:
            payload["invoice_number"] = result.invoice.invoice_number
        if result.payment is not None:
            payload["payment_id"] = str(result.payment.provider_payment_id)
        try:
            # Both are published because they mean different things to different
            # consumers: payment.succeeded is the money, order.paid is the fulfilment.
            await publisher.publish(EventType.PAYMENT_SUCCEEDED, payload)
            await publisher.publish(EventType.ORDER_PAID, payload)
        except Exception:
            logger.exception("payment.announce_failed", order_id=str(result.order.id))

    async def announce_failed(
        self, publisher: EventPublisher | None, order: Order, reason: str | None
    ) -> None:
        if publisher is None:
            return
        try:
            await publisher.publish(
                EventType.PAYMENT_FAILED,
                {
                    "order_id": str(order.id),
                    "order_number": order.order_number,
                    "user_id": str(order.user_id),
                    "email": order.billing_email,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception("payment.announce_failed_event_error", order_id=str(order.id))

    async def announce_refunded(
        self,
        publisher: EventPublisher | None,
        order: Order,
        *,
        amount_minor: int,
        revoke_entitlements: bool,
    ) -> None:
        if publisher is None:
            return
        try:
            await publisher.publish(
                EventType.ORDER_REFUNDED,
                {
                    "order_id": str(order.id),
                    "order_number": order.order_number,
                    "user_id": str(order.user_id),
                    "book_ids": [str(item.book_id) for item in order.items],
                    "amount_minor": amount_minor,
                    "currency": order.currency,
                    "fully_refunded": order.status == OrderStatus.REFUNDED,
                    # The books service revokes access off this flag. A partial
                    # refund of a multi-book order does not necessarily mean the
                    # customer loses everything, so the decision is made here.
                    "revoke_entitlements": revoke_entitlements,
                    "email": order.billing_email,
                },
            )
        except Exception:
            logger.exception("payment.announce_refund_failed", order_id=str(order.id))


def order_book_ids(order: Order) -> list[uuid.UUID]:
    return [item.book_id for item in order.items]

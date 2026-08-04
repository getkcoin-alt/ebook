"""Refunds.

A refund is a privileged, irreversible movement of money, so three things are always
true here:

* **It is attributable.** ``actor_id`` records who authorised it. "The system issued
  a refund" is not an acceptable answer to an auditor.
* **It cannot exceed what remains.** The check is on ``refundable_minor``, and the
  database backs it with ``refunded_minor <= total_minor``. Two concurrent refunds
  that each pass the application check are still stopped by the constraint.
* **It is idempotent at the gateway.** The provider is called with an idempotency
  key derived from the refund row's own id, so a retried request returns the original
  refund rather than issuing a second one.

Revoking access is a separate decision from moving the money, and it is announced as
an event rather than done here — this service does not own entitlements.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    OrderStatus,
    PaymentProvider,
    get_logger,
)
from models import Order, Payment, Refund
from schemas import PaymentStatus, RefundStatus
from services.affiliates import AffiliateService
from services.orders import OrderService
from services.providers import GatewayRegistry
from settings import Settings

logger = get_logger(__name__)


class RefundService:
    def __init__(
        self,
        settings: Settings,
        orders: OrderService,
        gateways: GatewayRegistry,
        affiliates: AffiliateService,
    ) -> None:
        self._settings = settings
        self._orders = orders
        self._gateways = gateways
        self._affiliates = affiliates

    async def _capturable_payment(self, session: AsyncSession, order: Order) -> Payment | None:
        """The captured payment to refund against.

        Largest capture first: refunding a ₹500 order against a ₹100 partial capture
        fails at the gateway, and the error it returns is not obviously about this.
        """
        stmt = (
            select(Payment)
            .where(
                Payment.order_id == order.id,
                Payment.status.in_([PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED]),
            )
            .order_by(Payment.amount_minor.desc(), Payment.created_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def create(
        self,
        session: AsyncSession,
        *,
        order: Order,
        amount_minor: int | None,
        reason: str | None,
        actor_id: uuid.UUID | None,
    ) -> Refund:
        """Issue a refund. Does not commit — the caller owns the transaction."""
        if order.status not in (OrderStatus.PAID, OrderStatus.PARTIALLY_REFUNDED):
            raise ConflictError(
                "Only a paid order can be refunded.",
                code="order_not_refundable",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        amount = amount_minor if amount_minor is not None else order.refundable_minor
        if amount <= 0:
            raise BadRequestError(
                "This order has already been fully refunded.", code="nothing_to_refund"
            )
        if amount > order.refundable_minor:
            raise BadRequestError(
                "That is more than remains refundable on this order.",
                code="refund_exceeds_remaining",
                details={"refundable_minor": order.refundable_minor, "requested_minor": amount},
            )

        payment = await self._capturable_payment(session, order)
        provider = order.provider or PaymentProvider.MANUAL

        refund = Refund(
            order_id=order.id,
            payment_id=payment.id if payment else None,
            provider=provider,
            amount_minor=amount,
            currency=order.currency,
            status=RefundStatus.PENDING,
            reason=reason,
            actor_id=actor_id,
        )
        session.add(refund)
        # Flushed before the gateway call so the refund's own id exists and can serve
        # as the idempotency key. Without it a timeout-and-retry would refund twice.
        await session.flush()

        if payment is None:
            # Nothing was captured through a gateway — a free order, or one settled
            # offline. The ledger still moves; the money movement is manual.
            refund.status = RefundStatus.SUCCEEDED
            refund.processed_at = datetime.now(UTC)
            logger.info(
                "refund.recorded_without_gateway", order_id=str(order.id), amount_minor=amount
            )
        else:
            gateway = self._gateways.get(provider)
            refund.status = RefundStatus.PROCESSING
            result = await gateway.refund(
                provider_payment_id=payment.provider_payment_id,
                amount_minor=amount,
                currency=order.currency,
                reason=reason,
                idempotency_key=str(refund.id),
            )
            refund.provider_refund_id = result.provider_refund_id
            refund.raw_payload = result.raw
            # Gateways settle refunds asynchronously and confirm by webhook. Anything
            # not already terminal stays PROCESSING until that arrives.
            if result.status in ("processed", "succeeded", "refunded"):
                refund.status = RefundStatus.SUCCEEDED
                refund.processed_at = datetime.now(UTC)

            payment.status = (
                PaymentStatus.REFUNDED
                if amount >= payment.amount_minor
                else PaymentStatus.PARTIALLY_REFUNDED
            )

        await self._orders.apply_refund(session, order, amount)
        if order.status == OrderStatus.REFUNDED:
            # Commission on a fully refunded sale is not earned.
            await self._affiliates.reverse(session, order.id)

        await session.flush()
        logger.info(
            "refund.created",
            order_id=str(order.id),
            refund_id=str(refund.id),
            amount_minor=amount,
            actor_id=str(actor_id) if actor_id else None,
            provider=provider.value,
        )
        return refund

    async def mark_settled(
        self,
        session: AsyncSession,
        *,
        provider: PaymentProvider,
        provider_refund_id: str,
        succeeded: bool,
        raw_payload: dict | None = None,
    ) -> Refund | None:
        """Apply a refund webhook. Returns ``None`` when the refund is unknown."""
        stmt = select(Refund).where(
            Refund.provider == provider, Refund.provider_refund_id == provider_refund_id
        )
        refund = (await session.execute(stmt)).scalars().one_or_none()
        if refund is None:
            logger.warning(
                "refund.webhook_for_unknown_refund", provider_refund_id=provider_refund_id
            )
            return None
        if refund.status in (RefundStatus.SUCCEEDED, RefundStatus.FAILED):
            return refund

        refund.status = RefundStatus.SUCCEEDED if succeeded else RefundStatus.FAILED
        refund.processed_at = datetime.now(UTC)
        if raw_payload:
            refund.raw_payload = raw_payload
        await session.flush()
        logger.info("refund.settled", refund_id=str(refund.id), succeeded=succeeded)
        return refund

    async def get(self, session: AsyncSession, refund_id: uuid.UUID) -> Refund:
        refund = await session.get(Refund, refund_id)
        if refund is None:
            raise NotFoundError("Refund not found.", details={"refund_id": str(refund_id)})
        return refund

    async def list_for_order(self, session: AsyncSession, order_id: uuid.UUID) -> list[Refund]:
        stmt = select(Refund).where(Refund.order_id == order_id).order_by(Refund.created_at.desc())
        return list((await session.execute(stmt)).scalars().all())

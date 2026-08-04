"""Order lifecycle.

An order is created *before* the customer is sent to a gateway, and it is the record
the gateway's callback is reconciled against. Creating it afterwards — on the
webhook — would mean a successful payment could arrive with nothing to attach it to.

State moves in one direction:

    pending -> awaiting_payment -> paid -> (partially_)refunded
                              \\-> failed
                              \\-> cancelled

Nothing here deletes. A cancelled order stays, an expired one stays, and a refund is
a new row plus a status change. This is financial data: the history is the point.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from knowledgeos_core import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    OrderStatus,
    PaymentProvider,
    decode_cursor,
    encode_cursor,
    get_logger,
)
from models import Order, OrderItem
from schemas import OrderCreate
from services.coupons import CouponService
from services.pricing import PricedCart
from services.providers import GatewayRegistry
from settings import Settings

logger = get_logger(__name__)

#: Statuses from which a customer may still walk away.
CANCELLABLE = {OrderStatus.PENDING, OrderStatus.AWAITING_PAYMENT}
#: Statuses that mean money moved.
SETTLED = {OrderStatus.PAID, OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED}


def generate_order_number(prefix: str) -> str:
    """A human-readable, unguessable order reference.

    ``KOS-20260804-7QK4M2XA``. The date makes support conversations easy; the random
    tail means an order number cannot be enumerated by incrementing one. A sequential
    counter would leak daily sales volume to anyone who buys twice.
    """
    # Crockford-ish alphabet: no I, O, 1 or 0, so a number read aloud or copied off a
    # printed invoice does not come back wrong.
    alphabet = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
    tail = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{prefix}-{datetime.now(UTC):%Y%m%d}-{tail}"


def _decode_order_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Unpack a keyset cursor into its (created_at, id) pair."""
    data = decode_cursor(cursor)
    try:
        created_at = datetime.fromisoformat(str(data["v"]))
        last_id = uuid.UUID(str(data["id"]))
    except (KeyError, ValueError) as exc:
        raise BadRequestError("The pagination cursor is malformed.") from exc
    # SQLite returns naive datetimes, so an aware cursor value would fail to compare.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at, last_id


class OrderService:
    def __init__(
        self, settings: Settings, coupons: CouponService, gateways: GatewayRegistry
    ) -> None:
        self._settings = settings
        self._coupons = coupons
        self._gateways = gateways

    # ---- reads ----------------------------------------------------------

    async def get(self, session: AsyncSession, order_id: uuid.UUID) -> Order:
        stmt = select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        order = (await session.execute(stmt)).scalars().one_or_none()
        if order is None:
            raise NotFoundError("Order not found.", details={"order_id": str(order_id)})
        return order

    async def get_for_user(
        self, session: AsyncSession, order_id: uuid.UUID, user_id: uuid.UUID
    ) -> Order:
        """Ownership is checked here, not in the router.

        Returning 404 rather than 403 for someone else's order is deliberate: a 403
        confirms the order exists, which turns the endpoint into an oracle for
        probing order ids.
        """
        order = await self.get(session, order_id)
        if order.user_id != user_id:
            raise NotFoundError("Order not found.", details={"order_id": str(order_id)})
        return order

    async def get_by_number(self, session: AsyncSession, order_number: str) -> Order:
        stmt = (
            select(Order)
            .where(Order.order_number == order_number)
            .options(selectinload(Order.items))
        )
        order = (await session.execute(stmt)).scalars().one_or_none()
        if order is None:
            raise NotFoundError("Order not found.", details={"order_number": order_number})
        return order

    async def find_by_provider_reference(
        self,
        session: AsyncSession,
        *,
        provider: PaymentProvider | str,
        provider_order_id: str | None = None,
        order_id: uuid.UUID | None = None,
    ) -> Order | None:
        """Locate the order a webhook is about.

        Two routes, because providers are inconsistent about what they echo back:
        our own id from the metadata we set, or the gateway's order id we stored at
        creation. Either identifies the order; neither is guaranteed to be present.
        """
        conditions = []
        if order_id is not None:
            conditions.append(Order.id == order_id)
        if provider_order_id:
            conditions.append(Order.provider_order_id == provider_order_id)
        if not conditions:
            return None
        stmt = (
            select(Order)
            .where(or_(*conditions))
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def find_by_idempotency_key(
        self, session: AsyncSession, key: str, user_id: uuid.UUID
    ) -> Order | None:
        stmt = (
            select(Order)
            .where(Order.idempotency_key == key, Order.user_id == user_id)
            .options(selectinload(Order.items))
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def list_for_user(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        cursor: str | None = None,
        limit: int = 20,
        status: OrderStatus | None = None,
    ) -> tuple[list[Order], str | None, bool]:
        """Keyset pagination on (created_at, id).

        Offset paging on an order history is wrong for the same reason it is wrong
        on any feed: a new order shifts every page boundary, so page 2 re-shows a
        row the customer already saw.
        """
        conditions = [Order.user_id == user_id]
        if status is not None:
            conditions.append(Order.status == status)
        if cursor:
            last_created, last_id = _decode_order_cursor(cursor)
            conditions.append(
                or_(
                    Order.created_at < last_created,
                    and_(Order.created_at == last_created, Order.id < last_id),
                )
            )
        stmt = (
            select(Order)
            .where(*conditions)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit + 1)  # one extra row answers "is there a next page?"
        )
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            encode_cursor({"v": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)})
            if rows and has_more
            else None
        )
        return rows, next_cursor, has_more

    async def list_all(
        self,
        session: AsyncSession,
        *,
        status: OrderStatus | None = None,
        user_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Order], int]:
        """Admin listing. Offset paging is fine here — an operator wants page 7."""
        conditions = []
        if status is not None:
            conditions.append(Order.status == status)
        if user_id is not None:
            conditions.append(Order.user_id == user_id)
        if since is not None:
            conditions.append(Order.created_at >= since)
        if until is not None:
            conditions.append(Order.created_at <= until)

        total = int(
            (await session.execute(select(func.count(Order.id)).where(*conditions))).scalar_one()
        )
        stmt = (
            select(Order)
            .where(*conditions)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all()), total

    # ---- creation -------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        payload: OrderCreate,
        cart: PricedCart,
        idempotency_key: str | None = None,
        billing_email: str | None = None,
    ) -> Order:
        """Persist the priced cart as an order and reserve its coupon.

        The coupon is consumed here rather than on payment. A single-use code held
        by an abandoned checkout is released by :meth:`expire_stale`; the reverse
        mistake — consuming it only on success — lets one code fund unlimited
        concurrent checkouts.
        """
        cart.assert_consistent()

        order = Order(
            user_id=user_id,
            order_number=generate_order_number(self._settings.invoice_prefix),
            status=OrderStatus.PENDING,
            subtotal_minor=cart.subtotal_minor,
            discount_minor=cart.discount_minor,
            tax_minor=cart.tax.total_minor,
            total_minor=cart.total_minor,
            currency=cart.currency,
            cgst_minor=cart.tax.cgst_minor,
            sgst_minor=cart.tax.sgst_minor,
            igst_minor=cart.tax.igst_minor,
            tax_percent=cart.tax.percent,
            place_of_supply=cart.tax.place_of_supply,
            coupon_id=cart.coupon_id,
            coupon_code=cart.coupon_code if cart.coupon_applied else None,
            affiliate_code=payload.affiliate_code,
            idempotency_key=idempotency_key,
            billing_name=payload.billing_name,
            billing_email=billing_email or payload.billing_email,
            billing_address=payload.billing.model_dump(mode="json") if payload.billing else {},
            expires_at=datetime.now(UTC) + timedelta(seconds=self._settings.order_ttl_seconds),
        )
        session.add(order)

        for line in cart.lines:
            session.add(
                OrderItem(
                    order=order,
                    book_id=line.book_id,
                    # Snapshotted: a price change tomorrow must not rewrite what this
                    # customer bought today.
                    title=line.title,
                    slug=line.slug,
                    quantity=line.quantity,
                    unit_price_minor=line.unit_price_minor,
                    line_total_minor=line.line_total_minor,
                )
            )

        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            # Two candidates: the order number collided (astronomically unlikely, and
            # a retry fixes it) or this idempotency key is already in use.
            raise ConflictError(
                "That order could not be created; please retry.",
                code="order_create_conflict",
            ) from exc

        if cart.coupon_applied and cart.coupon_id is not None:
            coupon = await self._coupons.get(session, cart.coupon_id)
            await self._coupons.redeem(
                session,
                coupon=coupon,
                order_id=order.id,
                user_id=user_id,
                discount_minor=cart.discount_minor,
            )

        logger.info(
            "order.created",
            order_id=str(order.id),
            order_number=order.order_number,
            total_minor=order.total_minor,
            currency=order.currency,
            item_count=len(cart.lines),
        )
        return order

    async def attach_gateway(
        self,
        session: AsyncSession,
        order: Order,
        *,
        requested_provider: PaymentProvider | str | None,
        return_url: str | None,
    ) -> tuple[Order, str | None, str | None]:
        """Open the order on a gateway and record what it gave us back.

        Returns ``(order, checkout_url, client_secret)``. The client secret is never
        persisted — it is a short-lived, single-use credential for one browser, and
        storing it would put a live payment credential in the database for no reason.
        """
        gateway = self._gateways.resolve(requested_provider, amount_minor=order.total_minor)
        description = ", ".join(item.title for item in order.items)[:250] or order.order_number

        provider_order = await gateway.create_order(
            order_id=order.id,
            order_number=order.order_number,
            amount_minor=order.total_minor,
            currency=order.currency,
            customer_email=order.billing_email,
            return_url=return_url,
            description=description,
        )

        order.provider = gateway.name
        order.provider_order_id = provider_order.provider_order_id
        order.checkout_url = provider_order.checkout_url
        order.status = OrderStatus.AWAITING_PAYMENT
        await session.flush()

        logger.info(
            "order.gateway_attached",
            order_id=str(order.id),
            provider=gateway.name.value,
            provider_order_id=provider_order.provider_order_id,
        )
        return order, provider_order.checkout_url, provider_order.client_secret

    # ---- transitions ----------------------------------------------------

    async def cancel(
        self,
        session: AsyncSession,
        order: Order,
        *,
        reason: str | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> Order:
        if order.status in SETTLED:
            raise ConflictError(
                "A paid order cannot be cancelled; refund it instead.",
                code="order_already_paid",
                details={"order_id": str(order.id), "status": str(order.status)},
            )
        if order.status not in CANCELLABLE:
            raise ConflictError(
                f"An order in state '{order.status}' cannot be cancelled.",
                code="order_not_cancellable",
            )

        order.status = OrderStatus.CANCELLED
        order.cancelled_at = datetime.now(UTC)
        order.failure_reason = reason
        # Hand the coupon back — the customer never got their discount.
        await self._coupons.release(session, order_id=order.id)
        await session.flush()
        logger.info(
            "order.cancelled",
            order_id=str(order.id),
            actor_id=str(actor_id) if actor_id else None,
            reason=reason,
        )
        return order

    async def mark_failed(
        self, session: AsyncSession, order: Order, *, reason: str | None = None
    ) -> Order:
        """A failed attempt is not terminal.

        The order stays reachable so the customer can retry from the same cart, but
        an order already paid is never downgraded — a late failure webhook for a
        superseded attempt must not un-pay a settled order.
        """
        if order.status in SETTLED:
            logger.info("order.failure_ignored_after_payment", order_id=str(order.id))
            return order
        order.status = OrderStatus.FAILED
        order.failure_reason = (reason or "")[:500] or None
        await session.flush()
        logger.info("order.failed", order_id=str(order.id), reason=reason)
        return order

    async def mark_paid(self, session: AsyncSession, order: Order) -> bool:
        """Settle the order. Returns True only the first time.

        The return value is what makes the whole payment path idempotent: a
        redelivered webhook and a client-side verify call both reach here, and only
        one of them may grant entitlements or issue an invoice.
        """
        if order.status in SETTLED:
            return False
        order.status = OrderStatus.PAID
        order.paid_at = datetime.now(UTC)
        order.failure_reason = None
        await session.flush()
        logger.info(
            "order.paid",
            order_id=str(order.id),
            order_number=order.order_number,
            total_minor=order.total_minor,
        )
        return True

    async def apply_refund(self, session: AsyncSession, order: Order, amount_minor: int) -> Order:
        """Move refunded_minor and recompute the order's status."""
        if amount_minor <= 0:
            raise BadRequestError("A refund amount must be positive.")
        if amount_minor > order.refundable_minor:
            raise BadRequestError(
                "That is more than remains refundable on this order.",
                code="refund_exceeds_remaining",
                details={
                    "refundable_minor": order.refundable_minor,
                    "requested_minor": amount_minor,
                },
            )
        order.refunded_minor += amount_minor
        order.status = (
            OrderStatus.REFUNDED
            if order.refunded_minor >= order.total_minor
            else OrderStatus.PARTIALLY_REFUNDED
        )
        await session.flush()
        return order

    # ---- housekeeping ---------------------------------------------------

    async def expire_stale(self, session: AsyncSession, *, batch: int = 200) -> int:
        """Expire orders that were never paid, returning their coupons.

        Run from the worker on a schedule. Without it a single-use code held by an
        abandoned checkout is consumed forever, and the customer who was going to use
        it gets told it is spent.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Order)
            .where(
                Order.status.in_([OrderStatus.PENDING, OrderStatus.AWAITING_PAYMENT]),
                Order.expires_at.is_not(None),
                Order.expires_at < now,
            )
            .options(selectinload(Order.items))
            .limit(batch)
        )
        stale = list((await session.execute(stmt)).scalars().all())
        for order in stale:
            order.status = OrderStatus.CANCELLED
            order.cancelled_at = now
            order.failure_reason = "Expired before payment."
            await self._coupons.release(session, order_id=order.id)
        if stale:
            await session.commit()
            logger.info("order.expired_batch", count=len(stale))
        return len(stale)

    # ---- ownership guard -------------------------------------------------

    @staticmethod
    def require_owner(order: Order, user_id: uuid.UUID) -> None:
        if order.user_id != user_id:
            raise ForbiddenError("This order belongs to someone else.")

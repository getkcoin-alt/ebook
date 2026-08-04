"""Checkout: quoting a cart, creating an order, confirming a payment.

The customer-facing half of the service. Three things are true of every route here:

* The **price is never taken from the request**. Clients send book ids; amounts come
  from the catalogue.
* The **user id is never taken from the request**. It comes from the access token.
* **Order creation is idempotent.** A double-clicked Buy button, a mobile client
  retrying after a timeout, and a flaky connection all produce one order.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from deps import (
    CurrentUser,
    CursorLimit,
    DbSession,
    Gateways,
    IdempotencyKey,
    OptionalUser,
    Orders,
    OrderStatusFilter,
    Payments,
    Pricing,
    safe_return_url,
    user_uuid,
)
from knowledgeos_core import (
    BadRequestError,
    ConflictError,
    Currency,
    ListResponse,
    PaymentProvider,
    get_logger,
)
from knowledgeos_core.deps import Ctx, Idempotency, rate_limit
from schemas import (
    CheckoutSession,
    CouponValidateRequest,
    CouponValidateResponse,
    OrderCancelRequest,
    OrderCreate,
    OrderDetail,
    OrderOut,
    PaymentVerifyRequest,
    ProvidersResponse,
    QuoteRequest,
    QuoteResponse,
    TaxBreakdown,
)
from services.pricing import PricedCart
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["checkout"])

#: Checkout is expensive (it calls the catalogue and a gateway) and is a natural
#: target for automated abuse, so it gets a tighter budget than ordinary reads.
CHECKOUT_LIMIT = Depends(rate_limit("authenticated", cost=5))


def _tax_out(cart: PricedCart) -> TaxBreakdown:
    return TaxBreakdown(
        percent=cart.tax.percent,
        cgst_minor=cart.tax.cgst_minor,
        sgst_minor=cart.tax.sgst_minor,
        igst_minor=cart.tax.igst_minor,
        total_minor=cart.tax.total_minor,
        place_of_supply=cart.tax.place_of_supply,
    )


def _quote_out(cart: PricedCart) -> QuoteResponse:
    return QuoteResponse(
        lines=cart.lines,
        subtotal_minor=cart.subtotal_minor,
        discount_minor=cart.discount_minor,
        taxable_minor=cart.taxable_minor,
        tax=_tax_out(cart),
        total_minor=cart.total_minor,
        currency=Currency(cart.currency),
        coupon_code=cart.coupon_code,
        coupon_applied=cart.coupon_applied,
        coupon_message=cart.coupon_message,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@router.get(
    "/payments/providers",
    response_model=ProvidersResponse,
    summary="Which gateways this deployment can use",
    description=(
        "The checkout page reads this rather than hard-coding a provider list, so a "
        "deployment without Stripe credentials does not render a Stripe button. The "
        "keys returned are the public ones the gateway SDKs need; secrets never "
        "leave this service."
    ),
)
async def list_providers(gateways: Gateways) -> ProvidersResponse:
    default = settings.default_provider
    return ProvidersResponse(
        providers=gateways.public_providers,
        default=PaymentProvider(default) if default else None,
        currency=Currency(settings.default_currency),
        razorpay_key_id=settings.razorpay_key_id,
        stripe_publishable_key=settings.stripe_publishable_key,
    )


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------


@router.post(
    "/checkout/quote",
    response_model=QuoteResponse,
    summary="Price a cart without creating an order",
    description=(
        "Read-only. The checkout page calls this as the customer edits their cart or "
        "types a coupon code, so it must not write anything or consume a coupon use."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def quote(
    payload: QuoteRequest,
    session: DbSession,
    pricing: Pricing,
    principal: OptionalUser = None,
) -> QuoteResponse:
    cart = await pricing.quote(
        session,
        items=payload.items,
        user_id=user_uuid(principal) if principal else None,
        currency=str(payload.currency),
        coupon_code=payload.coupon_code,
        billing=payload.billing,
    )
    return _quote_out(cart)


@router.post(
    "/coupons/validate",
    response_model=CouponValidateResponse,
    summary="Check a coupon against a cart",
    description=(
        "Returns 200 with `valid: false` and a readable reason for a bad code — a "
        "wrong coupon is a UI state, not an error, and a 4xx would make the "
        "checkout page render a failure banner for a typo."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def validate_coupon(
    payload: CouponValidateRequest,
    session: DbSession,
    pricing: Pricing,
    principal: OptionalUser = None,
) -> CouponValidateResponse:
    cart = await pricing.quote(
        session,
        items=payload.items,
        user_id=user_uuid(principal) if principal else None,
        currency=str(payload.currency),
        coupon_code=payload.code,
    )
    return CouponValidateResponse(
        valid=cart.coupon_applied,
        code=payload.code.strip().upper(),
        discount_minor=cart.discount_minor,
        subtotal_minor=cart.subtotal_minor,
        total_minor=cart.total_minor,
        reason=cart.coupon_message,
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@router.post(
    "/orders",
    response_model=CheckoutSession,
    status_code=status.HTTP_201_CREATED,
    summary="Create an order and open a checkout session",
    description=(
        "Send `Idempotency-Key: <uuid>` to make this safe to retry. Replaying a key "
        "returns the original order rather than creating a second one.\n\n"
        "The order is created *before* the customer is sent to the gateway, so the "
        "callback has something to reconcile against."
    ),
    dependencies=[CHECKOUT_LIMIT],
)
async def create_order(
    payload: OrderCreate,
    principal: CurrentUser,
    session: DbSession,
    pricing: Pricing,
    orders: Orders,
    ctx: Ctx,
    idempotency: Idempotency,
    idempotency_key: IdempotencyKey = None,
) -> CheckoutSession:
    user_id = user_uuid(principal)
    return_url = safe_return_url(payload.return_url)

    # The database is the durable idempotency record; Redis only stops two truly
    # concurrent retries from both entering the handler. Checking the database first
    # means a retry still works after the Redis key has expired.
    if idempotency_key:
        existing = await orders.find_by_idempotency_key(session, idempotency_key, user_id)
        if existing is not None:
            logger.info("order.idempotent_replay", order_id=str(existing.id))
            return CheckoutSession(
                order=OrderDetail.model_validate(existing),
                provider=existing.provider or PaymentProvider.MANUAL,
                provider_order_id=existing.provider_order_id,
                publishable_key=_publishable_key(existing.provider),
                checkout_url=existing.checkout_url,
                # Deliberately absent on a replay: a client secret is a single-use
                # credential for one browser session and is never persisted.
                client_secret=None,
                amount_minor=existing.total_minor,
                currency=existing.currency,
                expires_at=existing.expires_at,
            )

        reservation = await idempotency.begin(
            scope="orders",
            key=idempotency_key,
            actor=str(user_id),
            request_fingerprint=idempotency.fingerprint(payload.model_dump(mode="json")),
        )
        if reservation is not None:
            raise ConflictError("This order was already created.", code="idempotent_replay_pending")

    try:
        cart = await pricing.quote(
            session,
            items=payload.items,
            user_id=user_id,
            currency=str(payload.currency),
            coupon_code=payload.coupon_code,
            billing=payload.billing,
        )

        # A cart made entirely of books the customer already owns is a mistake worth
        # stopping, not a ₹0 order to process.
        if cart.lines and all(line.already_owned for line in cart.lines):
            raise BadRequestError(
                "You already own everything in this cart.",
                code="already_owned",
                details={"book_ids": [str(line.book_id) for line in cart.lines]},
            )

        order = await orders.create(
            session,
            user_id=user_id,
            payload=payload,
            cart=cart,
            idempotency_key=idempotency_key,
            billing_email=principal.email,
        )
        order, checkout_url, client_secret = await orders.attach_gateway(
            session,
            order,
            requested_provider=payload.provider,
            return_url=return_url,
        )
        await session.commit()
    except Exception:
        # Release the reservation so a client that can fix the request may retry.
        # Holding it would make the operation permanently unretryable with that key.
        await session.rollback()
        if idempotency_key:
            await idempotency.release(scope="orders", key=idempotency_key, actor=str(user_id))
        raise

    await session.refresh(order, ["items"])

    if idempotency_key:
        await idempotency.complete(
            scope="orders",
            key=idempotency_key,
            actor=str(user_id),
            request_fingerprint=idempotency.fingerprint(payload.model_dump(mode="json")),
            status_code=status.HTTP_201_CREATED,
            body={"order_id": str(order.id)},
        )

    # A zero-value order has no gateway step. Settling it here means a free book
    # grants access through exactly the same path as a paid one.
    if order.total_minor == 0:
        result = await ctx.extras["payments"].settle(
            session,
            order=order,
            provider=PaymentProvider.MANUAL,
            provider_payment_id=f"free_{order.id}",
            amount_minor=0,
        )
        await session.commit()
        await ctx.extras["payments"].announce_paid(ctx.publisher, result)

    return CheckoutSession(
        order=OrderDetail.model_validate(order),
        provider=order.provider or PaymentProvider.MANUAL,
        provider_order_id=order.provider_order_id,
        publishable_key=_publishable_key(order.provider),
        checkout_url=checkout_url,
        client_secret=client_secret,
        amount_minor=order.total_minor,
        currency=order.currency,
        expires_at=order.expires_at,
    )


def _publishable_key(provider: PaymentProvider | None) -> str | None:
    """The *public* key for the gateway's browser SDK. Never a secret."""
    if provider == PaymentProvider.RAZORPAY:
        return settings.razorpay_key_id
    if provider == PaymentProvider.STRIPE:
        return settings.stripe_publishable_key
    return None


@router.get(
    "/orders",
    response_model=ListResponse[OrderDetail],
    summary="Your order history",
    description="Keyset paginated: a new order does not shift the boundaries of pages already fetched.",
)
async def list_orders(
    principal: CurrentUser,
    session: DbSession,
    orders: Orders,
    limit: CursorLimit,
    order_status: OrderStatusFilter = None,
    cursor: Annotated[str | None, Query()] = None,
) -> ListResponse[OrderDetail]:
    rows, _cursor, _more = await orders.list_for_user(
        session,
        user_id=user_uuid(principal),
        cursor=cursor,
        limit=limit,
        status=order_status,
    )
    items = [OrderDetail.model_validate(row) for row in rows]
    return ListResponse[OrderDetail](items=items, total=len(items))


@router.get(
    "/orders/{order_id}",
    response_model=OrderDetail,
    summary="One of your orders",
    description="Returns 404 for an order you do not own, so order ids cannot be probed.",
)
async def get_order(
    order_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    orders: Orders,
) -> OrderDetail:
    order = await orders.get_for_user(session, order_id, user_uuid(principal))
    return OrderDetail.model_validate(order)


@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderOut,
    summary="Cancel an unpaid order",
    description="A paid order cannot be cancelled — it has to be refunded, which is an admin action.",
)
async def cancel_order(
    order_id: uuid.UUID,
    payload: OrderCancelRequest,
    principal: CurrentUser,
    session: DbSession,
    orders: Orders,
) -> OrderOut:
    user_id = user_uuid(principal)
    order = await orders.get_for_user(session, order_id, user_id)
    await orders.cancel(session, order, reason=payload.reason, actor_id=user_id)
    await session.commit()
    return OrderOut.model_validate(order)


# ---------------------------------------------------------------------------
# Client-side confirmation
# ---------------------------------------------------------------------------


@router.post(
    "/payments/verify",
    response_model=OrderDetail,
    summary="Confirm a payment from the browser",
    description=(
        "Called when the Razorpay Checkout modal closes successfully. This is a "
        "*convenience* path so the customer sees their library immediately — the "
        "webhook is still the authoritative channel and still arrives. The signature "
        "is verified exactly as strictly here as it is there; whichever lands first "
        "settles the order and the second is a no-op."
    ),
    dependencies=[Depends(rate_limit("authenticated", cost=2))],
)
async def verify_payment(
    payload: PaymentVerifyRequest,
    principal: CurrentUser,
    session: DbSession,
    orders: Orders,
    payments: Payments,
    gateways: Gateways,
    ctx: Ctx,
) -> OrderDetail:
    user_id = user_uuid(principal)
    order = await orders.get_for_user(session, payload.order_id, user_id)

    gateway = gateways.get(payload.provider)
    verify = getattr(gateway, "verify_checkout", None)
    if verify is None:
        raise BadRequestError(
            f"The {payload.provider} gateway does not support client-side verification.",
            code="verification_unsupported",
        )
    if not verify(
        provider_order_id=payload.provider_order_id,
        provider_payment_id=payload.provider_payment_id,
        signature=payload.signature,
    ):
        # Not a client error to be smoothed over: someone posted a payment
        # confirmation they could not sign.
        logger.warning(
            "payment.verify_signature_invalid",
            order_id=str(order.id),
            user_id=str(user_id),
            provider=str(payload.provider),
        )
        raise BadRequestError(
            "That payment confirmation could not be verified.",
            code="invalid_payment_signature",
        )

    # The gateway order id must be the one we opened for this order — a valid
    # signature over *someone else's* payment is still not payment for this order.
    if order.provider_order_id and order.provider_order_id != payload.provider_order_id:
        raise BadRequestError(
            "That payment belongs to a different order.", code="order_reference_mismatch"
        )

    result = await payments.settle(
        session,
        order=order,
        provider=payload.provider,
        provider_payment_id=payload.provider_payment_id,
    )
    await session.commit()
    await payments.announce_paid(ctx.publisher, result)

    await session.refresh(order, ["items"])
    return OrderDetail.model_validate(order)

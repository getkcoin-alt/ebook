"""Payment administration and service-to-service endpoints.

Refunds are the sharpest tool here. They require `orders:refund`, they record who
authorised them, and they announce revocation so the books service can take back
access — a refunded customer who keeps the file is the failure mode that costs real
money.

The `/internal/*` routes require an HMAC signature. On Railway's private network any
container can reach any other, so reachability is not authorisation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from deps import (
    Affiliates,
    Coupons,
    CurrentUser,
    DbSession,
    Invoices,
    Orders,
    PageOffset,
    Payments,
    Refunds,
    Reports,
    Subscriptions,
    Webhooks,
    user_uuid,
)
from knowledgeos_core import (
    ListResponse,
    MessageResponse,
    NotFoundError,
    OrderStatus,
    PaymentProvider,
    get_logger,
)
from knowledgeos_core.deps import Ctx, InternalCaller, require_permission
from schemas import (
    CouponCreate,
    CouponOut,
    CouponUpdate,
    InternalOrderSummary,
    InvoiceOut,
    OrderDetail,
    PlanCreate,
    PlanOut,
    PlanUpdate,
    RefundCreate,
    RefundOut,
    RevenueSummary,
    WebhookEventOut,
)

logger = get_logger(__name__)

ORDERS_READ = Depends(require_permission("orders:read"))
ORDERS_REFUND = Depends(require_permission("orders:refund"))
SETTINGS_WRITE = Depends(require_permission("settings:write"))
ANALYTICS_READ = Depends(require_permission("analytics:read"))

router = APIRouter(prefix="/v1/admin", tags=["admin"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


# ---------------------------------------------------------------------------
# Orders and refunds
# ---------------------------------------------------------------------------


@router.get(
    "/orders",
    response_model=ListResponse[OrderDetail],
    summary="List orders",
    description="Offset paging, unlike the customer feed — an operator legitimately wants page 7.",
    dependencies=[ORDERS_READ],
)
async def admin_list_orders(
    session: DbSession,
    orders: Orders,
    page: PageOffset,
    order_status: Annotated[OrderStatus | None, Query(alias="status")] = None,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> ListResponse[OrderDetail]:
    limit, offset = page
    rows, total = await orders.list_all(
        session,
        status=order_status,
        user_id=user_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return ListResponse[OrderDetail](
        items=[OrderDetail.model_validate(row) for row in rows], total=total
    )


@router.get(
    "/orders/{order_id}",
    response_model=OrderDetail,
    summary="Get any order",
    dependencies=[ORDERS_READ],
)
async def admin_get_order(order_id: uuid.UUID, session: DbSession, orders: Orders) -> OrderDetail:
    return OrderDetail.model_validate(await orders.get(session, order_id))


@router.post(
    "/orders/{order_id}/refund",
    response_model=RefundOut,
    status_code=status.HTTP_201_CREATED,
    summary="Refund an order",
    description=(
        "Omit `amount_minor` for a full refund of whatever remains refundable. "
        "The refund is attributed to the authenticated administrator, and unless "
        "`revoke_entitlements` is false the books service is told to take back access."
    ),
    dependencies=[ORDERS_REFUND],
)
async def refund_order(
    order_id: uuid.UUID,
    payload: RefundCreate,
    principal: CurrentUser,
    session: DbSession,
    orders: Orders,
    refunds: Refunds,
    payments: Payments,
    ctx: Ctx,
) -> RefundOut:
    order = await orders.get(session, order_id)
    refund = await refunds.create(
        session,
        order=order,
        amount_minor=payload.amount_minor,
        reason=payload.reason,
        actor_id=user_uuid(principal),
    )
    await session.commit()

    # Announced after the commit: revoking access for a refund that rolled back
    # would lock a paying customer out of a book they still own.
    await payments.announce_refunded(
        ctx.publisher,
        order,
        amount_minor=refund.amount_minor,
        revoke_entitlements=payload.revoke_entitlements,
    )
    return RefundOut.model_validate(refund)


@router.get(
    "/orders/{order_id}/refunds",
    response_model=ListResponse[RefundOut],
    summary="Refunds on an order",
    dependencies=[ORDERS_READ],
)
async def list_order_refunds(
    order_id: uuid.UUID, session: DbSession, refunds: Refunds
) -> ListResponse[RefundOut]:
    rows = await refunds.list_for_order(session, order_id)
    return ListResponse[RefundOut](
        items=[RefundOut.model_validate(row) for row in rows], total=len(rows)
    )


@router.post(
    "/orders/{order_id}/mark-paid",
    response_model=OrderDetail,
    summary="Settle an order paid outside the platform",
    description=(
        "For bank transfers and institutional invoices. Goes through the same "
        "settlement path as a card payment, so entitlements, the invoice and the "
        "affiliate credit all happen exactly as they normally would."
    ),
    dependencies=[ORDERS_REFUND],
)
async def mark_order_paid(
    order_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    orders: Orders,
    payments: Payments,
    ctx: Ctx,
) -> OrderDetail:
    order = await orders.get(session, order_id)
    result = await payments.settle(
        session,
        order=order,
        provider=PaymentProvider.MANUAL,
        # The actor is in the reference so an offline settlement is attributable.
        provider_payment_id=f"manual_{order.id}_{principal.user_id}",
        raw_payload={"settled_by": principal.user_id, "channel": "manual"},
    )
    await session.commit()
    await payments.announce_paid(ctx.publisher, result)
    await session.refresh(order, ["items"])
    logger.info("order.marked_paid_manually", order_id=str(order.id), actor=principal.user_id)
    return OrderDetail.model_validate(order)


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


@router.get(
    "/coupons",
    response_model=ListResponse[CouponOut],
    summary="List coupons",
    dependencies=[SETTINGS_WRITE],
)
async def list_coupons(
    session: DbSession,
    coupons: Coupons,
    page: PageOffset,
    active_only: Annotated[bool, Query()] = False,
) -> ListResponse[CouponOut]:
    limit, offset = page
    rows, total = await coupons.list(session, active_only=active_only, limit=limit, offset=offset)
    return ListResponse[CouponOut](
        items=[CouponOut.model_validate(row) for row in rows], total=total
    )


@router.post(
    "/coupons",
    response_model=CouponOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a coupon",
    dependencies=[SETTINGS_WRITE],
)
async def create_coupon(payload: CouponCreate, session: DbSession, coupons: Coupons) -> CouponOut:
    return CouponOut.model_validate(await coupons.create(session, payload))


@router.patch(
    "/coupons/{coupon_id}",
    response_model=CouponOut,
    summary="Update a coupon",
    description="The code itself is immutable — a redeemed code is part of the order record.",
    dependencies=[SETTINGS_WRITE],
)
async def update_coupon(
    coupon_id: uuid.UUID, payload: CouponUpdate, session: DbSession, coupons: Coupons
) -> CouponOut:
    coupon = await coupons.get(session, coupon_id)
    return CouponOut.model_validate(await coupons.update(session, coupon, payload))


@router.delete(
    "/coupons/{coupon_id}",
    response_model=CouponOut,
    summary="Deactivate a coupon",
    description="Deactivates rather than deletes: a redeemed coupon is referenced by past orders.",
    dependencies=[SETTINGS_WRITE],
)
async def deactivate_coupon(
    coupon_id: uuid.UUID, session: DbSession, coupons: Coupons
) -> CouponOut:
    coupon = await coupons.get(session, coupon_id)
    return CouponOut.model_validate(await coupons.deactivate(session, coupon))


# ---------------------------------------------------------------------------
# Subscription plans
# ---------------------------------------------------------------------------


@router.get(
    "/plans",
    response_model=ListResponse[PlanOut],
    summary="List all plans",
    dependencies=[SETTINGS_WRITE],
)
async def admin_list_plans(
    session: DbSession, subscriptions: Subscriptions
) -> ListResponse[PlanOut]:
    plans = await subscriptions.list_plans(session, active_only=False)
    return ListResponse[PlanOut](
        items=[PlanOut.model_validate(plan) for plan in plans], total=len(plans)
    )


@router.post(
    "/plans",
    response_model=PlanOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a plan",
    dependencies=[SETTINGS_WRITE],
)
async def create_plan(
    payload: PlanCreate, session: DbSession, subscriptions: Subscriptions
) -> PlanOut:
    return PlanOut.model_validate(await subscriptions.create_plan(session, payload))


@router.patch(
    "/plans/{plan_id}",
    response_model=PlanOut,
    summary="Update a plan",
    description="Price changes apply to new subscriptions only; existing members keep their rate.",
    dependencies=[SETTINGS_WRITE],
)
async def update_plan(
    plan_id: uuid.UUID,
    payload: PlanUpdate,
    session: DbSession,
    subscriptions: Subscriptions,
) -> PlanOut:
    plan = await subscriptions.get_plan(session, plan_id)
    return PlanOut.model_validate(await subscriptions.update_plan(session, plan, payload))


# ---------------------------------------------------------------------------
# Webhooks and reporting
# ---------------------------------------------------------------------------


@router.get(
    "/webhooks",
    response_model=ListResponse[WebhookEventOut],
    summary="Received webhooks",
    description=(
        "Includes deliveries whose signature failed — a burst of those is an attack "
        "signal, and dropping them would hide it."
    ),
    dependencies=[ORDERS_READ],
)
async def list_webhooks(
    session: DbSession,
    webhooks: Webhooks,
    page: PageOffset,
    provider: Annotated[PaymentProvider | None, Query()] = None,
    processed: Annotated[bool | None, Query()] = None,
) -> ListResponse[WebhookEventOut]:
    limit, offset = page
    rows = await webhooks.list_events(
        session, provider=provider, processed=processed, limit=limit, offset=offset
    )
    return ListResponse[WebhookEventOut](
        items=[WebhookEventOut.model_validate(row) for row in rows], total=len(rows)
    )


@router.post(
    "/webhooks/{webhook_event_id}/replay",
    response_model=MessageResponse,
    summary="Replay a stored webhook",
    description=(
        "For an event whose processing failed and has since been fixed. Only runs "
        "against events whose signature already verified — a replay must not become "
        "a way to launder an unsigned payload into the system."
    ),
    dependencies=[ORDERS_REFUND],
)
async def replay_webhook(
    webhook_event_id: uuid.UUID,
    session: DbSession,
    webhooks: Webhooks,
    orders: Orders,
    payments: Payments,
    ctx: Ctx,
) -> MessageResponse:
    outcome = await webhooks.replay(session, webhook_event_id)
    if not outcome.accepted:
        raise NotFoundError(
            "No replayable webhook event with that id.",
            details={"webhook_event_id": str(webhook_event_id)},
        )
    if outcome.settled_order_id is not None:
        from services.payments import SettlementResult

        order = await orders.get(session, outcome.settled_order_id)
        await payments.announce_paid(ctx.publisher, SettlementResult(order=order, newly_paid=True))
    return MessageResponse(message="Webhook replayed.")


@router.get(
    "/revenue",
    response_model=RevenueSummary,
    summary="Revenue summary",
    description=(
        "Net is gross minus refunds **minus tax**. GST is collected on the "
        "government's behalf and is not revenue; counting it would overstate the "
        "business."
    ),
    dependencies=[ANALYTICS_READ],
)
async def revenue(
    session: DbSession,
    reports: Reports,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> RevenueSummary:
    return await reports.revenue(session, since=since, until=until)


@router.get(
    "/invoices",
    response_model=ListResponse[InvoiceOut],
    summary="Invoices for any customer",
    dependencies=[ORDERS_READ],
)
async def admin_list_invoices(
    session: DbSession,
    invoices: Invoices,
    page: PageOffset,
    # No `= ...` default. With `Annotated`, a required parameter is one with no
    # default at all; writing `= ...` makes Ellipsis the actual default value, and
    # FastAPI then tries to validate the literal `...` as a UUID. Every call was a
    # 500 before it reached this function.
    user_id: Annotated[uuid.UUID, Query()],
) -> ListResponse[InvoiceOut]:
    limit, offset = page
    rows, total = await invoices.list_for_user(session, user_id=user_id, limit=limit, offset=offset)
    return ListResponse[InvoiceOut](
        items=[InvoiceOut.model_validate(row) for row in rows], total=total
    )


# ---------------------------------------------------------------------------
# Internal (HMAC-signed callers only)
# ---------------------------------------------------------------------------


@internal_router.get(
    "/orders/{order_id}",
    response_model=InternalOrderSummary,
    summary="Order summary (internal)",
    description="Used by the admin service and by support tooling to resolve an order.",
)
async def internal_get_order(
    order_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    orders: Orders,
) -> InternalOrderSummary:
    order = await orders.get(session, order_id)
    return InternalOrderSummary(
        id=order.id,
        order_number=order.order_number,
        user_id=order.user_id,
        status=OrderStatus(order.status),
        total_minor=order.total_minor,
        currency=order.currency,
        book_ids=[item.book_id for item in order.items],
        paid_at=order.paid_at,
    )


@internal_router.get(
    "/users/{user_id}/purchased-books",
    response_model=list[uuid.UUID],
    summary="Books a user has paid for (internal)",
    description=(
        "The payment service's own view of what was bought. The books service owns "
        "entitlements and is authoritative for access; this endpoint exists so the "
        "two can be reconciled when they disagree."
    ),
)
async def internal_purchased_books(
    user_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    orders: Orders,
) -> list[uuid.UUID]:
    rows, _total = await orders.list_all(session, user_id=user_id, limit=500)
    purchased: dict[uuid.UUID, None] = {}
    for order in rows:
        if order.status in (OrderStatus.PAID, OrderStatus.PARTIALLY_REFUNDED):
            for item in order.items:
                purchased.setdefault(item.book_id, None)
    return list(purchased)


@internal_router.get(
    "/subscriptions/{user_id}/active",
    response_model=bool,
    summary="Is this user subscribed? (internal)",
    description="A single boolean, because that is the only thing callers act on.",
)
async def internal_subscription_active(
    user_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    subscriptions: Subscriptions,
) -> bool:
    return await subscriptions.active_for_user(session, user_id) is not None


@internal_router.post(
    "/maintenance/expire-orders",
    response_model=MessageResponse,
    summary="Expire unpaid orders (internal)",
    description=(
        "Called by the worker on a schedule. Returns the coupons held by abandoned "
        "checkouts, which would otherwise be consumed forever."
    ),
)
async def internal_expire_orders(
    caller: InternalCaller, session: DbSession, orders: Orders
) -> MessageResponse:
    count = await orders.expire_stale(session)
    return MessageResponse(message=f"Expired {count} unpaid orders.")


@internal_router.post(
    "/maintenance/approve-conversions",
    response_model=MessageResponse,
    summary="Approve matured affiliate conversions (internal)",
)
async def internal_approve_conversions(
    caller: InternalCaller, session: DbSession, affiliates: Affiliates
) -> MessageResponse:
    count = await affiliates.approve_matured(session)
    return MessageResponse(message=f"Approved {count} affiliate conversions.")


@internal_router.post(
    "/maintenance/expire-subscriptions",
    response_model=MessageResponse,
    summary="Expire lapsed subscriptions (internal)",
    description="A safety net for a missed cancellation webhook, not the primary mechanism.",
)
async def internal_expire_subscriptions(
    caller: InternalCaller, session: DbSession, subscriptions: Subscriptions
) -> MessageResponse:
    count = await subscriptions.expire_lapsed(session)
    return MessageResponse(message=f"Expired {count} subscriptions.")

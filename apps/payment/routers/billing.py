"""Customer-facing billing: invoices, subscriptions and affiliate earnings.

Everything here is scoped to the caller's own records. The user id comes from the
access token in every case, and lookups that miss return 404 rather than 403 so an
id cannot be tested for existence.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from deps import (
    Affiliates,
    CurrentUser,
    DbSession,
    Gateways,
    Invoices,
    PageOffset,
    Subscriptions,
    safe_return_url,
    user_uuid,
)
from knowledgeos_core import ListResponse, PaymentProvider, get_logger
from knowledgeos_core.deps import rate_limit
from schemas import (
    AffiliateConversionOut,
    AffiliateOut,
    AffiliateRegister,
    InvoiceOut,
    PlanOut,
    SubscriptionCancelRequest,
    SubscriptionCreate,
    SubscriptionOut,
)

logger = get_logger(__name__)

invoices_router = APIRouter(prefix="/v1/invoices", tags=["invoices"])
subscriptions_router = APIRouter(prefix="/v1", tags=["subscriptions"])
affiliates_router = APIRouter(prefix="/v1/affiliate", tags=["affiliate"])


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


@invoices_router.get(
    "",
    response_model=ListResponse[InvoiceOut],
    summary="Your invoices",
)
async def list_invoices(
    principal: CurrentUser,
    session: DbSession,
    invoices: Invoices,
    page: PageOffset,
) -> ListResponse[InvoiceOut]:
    limit, offset = page
    rows, total = await invoices.list_for_user(
        session, user_id=user_uuid(principal), limit=limit, offset=offset
    )
    return ListResponse[InvoiceOut](
        items=[InvoiceOut.model_validate(row) for row in rows], total=total
    )


@invoices_router.get(
    "/{invoice_id}",
    response_model=InvoiceOut,
    summary="One invoice",
    description=(
        "The billing details and line items are the ones snapshotted when the "
        "invoice was issued, not today's — a filed document does not change because "
        "a customer later edited their address."
    ),
)
async def get_invoice(
    invoice_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    invoices: Invoices,
) -> InvoiceOut:
    invoice = await invoices.get_for_user(session, invoice_id, user_uuid(principal))
    return InvoiceOut.model_validate(invoice)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


@subscriptions_router.get(
    "/plans",
    response_model=ListResponse[PlanOut],
    summary="Available subscription plans",
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def list_plans(session: DbSession, subscriptions: Subscriptions) -> ListResponse[PlanOut]:
    plans = await subscriptions.list_plans(session, active_only=True)
    return ListResponse[PlanOut](
        items=[PlanOut.model_validate(plan) for plan in plans], total=len(plans)
    )


@subscriptions_router.get(
    "/subscriptions",
    response_model=ListResponse[SubscriptionOut],
    summary="Your subscriptions",
)
async def list_subscriptions(
    principal: CurrentUser, session: DbSession, subscriptions: Subscriptions
) -> ListResponse[SubscriptionOut]:
    rows = await subscriptions.list_for_user(session, user_uuid(principal))
    return ListResponse[SubscriptionOut](
        items=[SubscriptionOut.model_validate(row) for row in rows], total=len(rows)
    )


@subscriptions_router.get(
    "/subscriptions/active",
    response_model=SubscriptionOut | None,
    summary="Your current membership, if any",
    description=(
        "Checks the period end as well as the status, so a gateway that fails to "
        "send its cancellation webhook cannot leave someone subscribed forever."
    ),
)
async def active_subscription(
    principal: CurrentUser, session: DbSession, subscriptions: Subscriptions
) -> SubscriptionOut | None:
    subscription = await subscriptions.active_for_user(session, user_uuid(principal))
    return SubscriptionOut.model_validate(subscription) if subscription else None


@subscriptions_router.post(
    "/subscriptions",
    response_model=SubscriptionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a subscription",
    dependencies=[Depends(rate_limit("authenticated", cost=5))],
)
async def create_subscription(
    payload: SubscriptionCreate,
    principal: CurrentUser,
    session: DbSession,
    subscriptions: Subscriptions,
    gateways: Gateways,
) -> SubscriptionOut:
    # Validated even though it is not used to build a redirect here, so a bad value
    # is rejected at the point the client sent it rather than silently ignored.
    safe_return_url(payload.return_url)

    plan = await subscriptions.get_plan(session, payload.plan_id)
    gateway = gateways.resolve(payload.provider, amount_minor=plan.price_minor)
    subscription = await subscriptions.create(
        session,
        user_id=user_uuid(principal),
        plan=plan,
        provider=PaymentProvider(gateway.name),
    )
    return SubscriptionOut.model_validate(subscription)


@subscriptions_router.post(
    "/subscriptions/{subscription_id}/cancel",
    response_model=SubscriptionOut,
    summary="Cancel a subscription",
    description=(
        "Defaults to cancelling at the end of the paid period. The customer paid "
        "for that period; ending it immediately would be an unrefunded loss to them."
    ),
)
async def cancel_subscription(
    subscription_id: uuid.UUID,
    payload: SubscriptionCancelRequest,
    principal: CurrentUser,
    session: DbSession,
    subscriptions: Subscriptions,
) -> SubscriptionOut:
    subscription = await subscriptions.get_for_user(session, subscription_id, user_uuid(principal))
    updated = await subscriptions.cancel(session, subscription, at_period_end=payload.at_period_end)
    return SubscriptionOut.model_validate(updated)


# ---------------------------------------------------------------------------
# Affiliates
# ---------------------------------------------------------------------------


@affiliates_router.post(
    "",
    response_model=AffiliateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Become an affiliate",
    description="Idempotent: enrolling twice returns the existing account rather than failing.",
)
async def register_affiliate(
    payload: AffiliateRegister,
    principal: CurrentUser,
    session: DbSession,
    affiliates: Affiliates,
) -> AffiliateOut:
    account = await affiliates.register(session, user_id=user_uuid(principal), code=payload.code)
    return AffiliateOut.model_validate(account)


@affiliates_router.get(
    "/me",
    response_model=AffiliateOut | None,
    summary="Your affiliate account",
)
async def my_affiliate(
    principal: CurrentUser, session: DbSession, affiliates: Affiliates
) -> AffiliateOut | None:
    account = await affiliates.for_user(session, user_uuid(principal))
    return AffiliateOut.model_validate(account) if account else None


@affiliates_router.get(
    "/conversions",
    response_model=ListResponse[AffiliateConversionOut],
    summary="Your referred sales",
    description=(
        "Conversions stay `pending` until the refund window closes, then become "
        "`approved`. Paying out sooner would mean clawing money back when a buyer "
        "refunds."
    ),
)
async def my_conversions(
    principal: CurrentUser,
    session: DbSession,
    affiliates: Affiliates,
    page: PageOffset,
) -> ListResponse[AffiliateConversionOut]:
    account = await affiliates.for_user(session, user_uuid(principal))
    if account is None:
        return ListResponse[AffiliateConversionOut](items=[], total=0)
    limit, offset = page
    rows, total = await affiliates.conversions_for(
        session, account_id=account.id, limit=limit, offset=offset
    )
    return ListResponse[AffiliateConversionOut](
        items=[AffiliateConversionOut.model_validate(row) for row in rows], total=total
    )

"""Service-local dependencies for the payment service."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, Header, Query

from knowledgeos_core import BadRequestError, OrderStatus
from knowledgeos_core.deps import Ctx, CurrentUser, DbSession, OptionalUser
from knowledgeos_core.security import Principal
from services import (
    AffiliateService,
    CatalogueClient,
    CouponService,
    GatewayRegistry,
    InvoiceService,
    OrderService,
    PaymentService,
    PricingService,
    RefundService,
    ReportService,
    SubscriptionService,
    WebhookService,
)
from settings import settings

__all__ = [
    "Affiliates",
    "Catalogue",
    "Coupons",
    "CurrentUser",
    "DbSession",
    "Gateways",
    "IdempotencyKey",
    "Invoices",
    "OptionalUser",
    "Orders",
    "Payments",
    "Pricing",
    "Refunds",
    "Reports",
    "Subscriptions",
    "Webhooks",
    "cursor_limit",
    "safe_return_url",
    "user_uuid",
]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


Catalogue = Annotated[CatalogueClient, Depends(_extra("catalogue"))]
Coupons = Annotated[CouponService, Depends(_extra("coupons"))]
Pricing = Annotated[PricingService, Depends(_extra("pricing"))]
Orders = Annotated[OrderService, Depends(_extra("orders"))]
Payments = Annotated[PaymentService, Depends(_extra("payments"))]
Refunds = Annotated[RefundService, Depends(_extra("refunds"))]
Invoices = Annotated[InvoiceService, Depends(_extra("invoices"))]
Subscriptions = Annotated[SubscriptionService, Depends(_extra("subscriptions"))]
Affiliates = Annotated[AffiliateService, Depends(_extra("affiliates"))]
Webhooks = Annotated[WebhookService, Depends(_extra("webhooks"))]
Gateways = Annotated[GatewayRegistry, Depends(_extra("gateways"))]
Reports = Annotated[ReportService, Depends(_extra("reports"))]


def user_uuid(principal: Principal) -> uuid.UUID:
    """The authenticated caller's id.

    Ownership always comes from the token, never from a request field. An order's
    ``user_id`` is set from this, so a client cannot buy on someone else's behalf or
    read someone else's history by passing a different id.
    """
    return uuid.UUID(principal.user_id)


def cursor_limit(
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
) -> int:
    return limit


def page_offset(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> tuple[int, int]:
    return limit, offset


def order_status_filter(
    status: Annotated[OrderStatus | None, Query(description="Filter by order state.")] = None,
) -> OrderStatus | None:
    return status


def idempotency_key(
    key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            max_length=255,
            description=(
                "Send a UUID to make order creation safe to retry. Replaying the "
                "same key returns the original order instead of creating a second."
            ),
        ),
    ] = None,
) -> str | None:
    return key


def safe_return_url(candidate: str | None) -> str | None:
    """Validate a client-supplied return URL against the configured allowlist.

    The gateway sends the customer here after payment, so an unvalidated value is an
    open redirect from a page the customer already trusts — the ideal position from
    which to phish a login. Anything unrecognised is dropped, not rejected: the
    default return URL is always correct.
    """
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = {str(item).rstrip("/") for item in settings.allowed_return_origins}
    allowed.add(str(settings.frontend_url).rstrip("/"))
    if origin not in allowed:
        raise BadRequestError(
            "That return URL is not allowed.",
            code="return_url_not_allowed",
            details={"origin": origin},
        )
    return candidate


CursorLimit = Annotated[int, Depends(cursor_limit)]
PageOffset = Annotated[tuple[int, int], Depends(page_offset)]
OrderStatusFilter = Annotated[OrderStatus | None, Depends(order_status_filter)]
IdempotencyKey = Annotated[str | None, Depends(idempotency_key)]

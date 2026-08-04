"""Request and response schemas for the payment service.

Two rules govern everything in this file.

**Money is an integer of the currency's minor unit.** Paise, cents. There is no
float and no ``Decimal`` on the wire. A float price column loses money at scale and
produces invoices that do not reconcile.

**The client never sends a price.** ``OrderCreate`` carries book ids and quantities;
the amount is resolved server-side from the catalogue. Any schema that accepted a
client-supplied amount would let a caller buy a book for one paisa, and no amount of
downstream validation makes that safe again.

Schemas subclass :class:`~knowledgeos_core.BaseSchema`, which sets
``from_attributes=True`` (responses build straight from ORM rows) and
``extra="forbid"`` (a typo'd field is a 422, not a silently ignored one).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from knowledgeos_core import BaseSchema, Currency, OrderStatus, PaymentProvider

# ---------------------------------------------------------------------------
# Enums owned by this service
# ---------------------------------------------------------------------------


class PaymentStatus(StrEnum):
    """Lifecycle of a single capture attempt.

    Distinct from :class:`~knowledgeos_core.OrderStatus`: an order may collect
    several payment attempts, most of which failed, before one succeeds.
    """

    PENDING = "pending"
    #: Authorised but not captured. Money is reserved, not taken.
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    #: The customer disputed the charge with their bank.
    DISPUTED = "disputed"


class RefundStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CouponType(StrEnum):
    #: ``value`` is a percentage, 0-100.
    PERCENT = "percent"
    #: ``value`` is a fixed amount in minor units.
    FIXED = "fixed"


class SubscriptionStatus(StrEnum):
    PENDING = "pending"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AffiliateConversionStatus(StrEnum):
    """A conversion is not payable until the refund window has closed."""

    PENDING = "pending"
    APPROVED = "approved"
    PAID = "paid"
    REVERSED = "reversed"


#: Indian state/UT codes, used to decide CGST+SGST versus IGST. A place of supply
#: outside this set is an export of services and attracts no GST.
STATE_CODE_PATTERN = r"^[A-Z]{2}$"


# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------


class BillingAddress(BaseSchema):
    """Captured for the invoice, not for delivery — these are digital goods.

    ``state_code`` is the one field with legal weight: it is the place of supply and
    decides whether the tax is CGST+SGST or IGST.
    """

    line1: str | None = Field(default=None, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=2, pattern=STATE_CODE_PATTERN)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str = Field(default="IN", min_length=2, max_length=2)
    #: GSTIN of a business buyer, so they can claim input credit.
    gstin: str | None = Field(default=None, max_length=15)

    @field_validator("country", "state_code", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class TaxBreakdown(BaseSchema):
    """The GST split. Exactly one of (cgst+sgst) or igst is non-zero."""

    percent: int = Field(ge=0, le=100)
    cgst_minor: int = Field(default=0, ge=0)
    sgst_minor: int = Field(default=0, ge=0)
    igst_minor: int = Field(default=0, ge=0)
    total_minor: int = Field(default=0, ge=0)
    place_of_supply: str | None = None


class OrderItemIn(BaseSchema):
    """A line the client asks for. Note the absence of a price field."""

    book_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=20)


class OrderItemOut(BaseSchema):
    id: uuid.UUID
    book_id: uuid.UUID
    title: str
    slug: str | None = None
    quantity: int
    unit_price_minor: int
    line_total_minor: int


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


class QuoteRequest(BaseSchema):
    """Price a cart without creating an order.

    The checkout page calls this on every coupon keystroke, so it must not write.
    """

    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    coupon_code: str | None = Field(default=None, max_length=64)
    currency: Currency = Currency.INR
    billing: BillingAddress | None = None

    @model_validator(mode="after")
    def _no_duplicate_books(self) -> QuoteRequest:
        seen = {item.book_id for item in self.items}
        if len(seen) != len(self.items):
            raise ValueError("Each book may appear only once; use quantity instead.")
        return self


class QuoteLine(BaseSchema):
    book_id: uuid.UUID
    title: str
    slug: str | None = None
    quantity: int
    unit_price_minor: int
    line_total_minor: int
    #: True when the buyer already owns it. Kept in the quote so the UI can say so
    #: rather than silently dropping the line.
    already_owned: bool = False


class QuoteResponse(BaseSchema):
    lines: list[QuoteLine]
    subtotal_minor: int
    discount_minor: int
    taxable_minor: int
    tax: TaxBreakdown
    total_minor: int
    currency: Currency
    coupon_code: str | None = None
    coupon_applied: bool = False
    coupon_message: str | None = None


# ---------------------------------------------------------------------------
# Orders and checkout
# ---------------------------------------------------------------------------


class OrderCreate(BaseSchema):
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    coupon_code: str | None = Field(default=None, max_length=64)
    #: Which gateway to route to. Omit to take the platform default.
    provider: PaymentProvider | None = None
    currency: Currency = Currency.INR
    billing_name: str | None = Field(default=None, max_length=200)
    billing_email: str | None = Field(default=None, max_length=320)
    billing: BillingAddress | None = None
    #: Attribution for an affiliate link. Unknown codes are ignored, not rejected —
    #: a stale referral link must never block a sale.
    affiliate_code: str | None = Field(default=None, max_length=64)
    #: Where the provider returns the customer. Validated against an allowlist.
    return_url: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _no_duplicate_books(self) -> OrderCreate:
        seen = {item.book_id for item in self.items}
        if len(seen) != len(self.items):
            raise ValueError("Each book may appear only once; use quantity instead.")
        return self


class OrderOut(BaseSchema):
    id: uuid.UUID
    order_number: str
    user_id: uuid.UUID
    status: OrderStatus
    subtotal_minor: int
    discount_minor: int
    tax_minor: int
    total_minor: int
    refunded_minor: int
    currency: str
    cgst_minor: int
    sgst_minor: int
    igst_minor: int
    tax_percent: int
    place_of_supply: str | None = None
    provider: PaymentProvider | None = None
    provider_order_id: str | None = None
    coupon_code: str | None = None
    billing_name: str | None = None
    billing_email: str | None = None
    paid_at: datetime | None = None
    cancelled_at: datetime | None = None
    expires_at: datetime | None = None
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class OrderDetail(OrderOut):
    items: list[OrderItemOut] = Field(default_factory=list)


class CheckoutSession(BaseSchema):
    """Everything the browser needs to open the gateway.

    ``publishable_key`` is the *public* key by design — it identifies the merchant to
    the gateway's client SDK. The secret key never leaves this service.
    """

    order: OrderDetail
    provider: PaymentProvider
    provider_order_id: str | None = None
    #: Razorpay: the merchant key. Stripe: the publishable key.
    publishable_key: str | None = None
    #: Stripe Checkout / Razorpay Payment Links redirect here.
    checkout_url: str | None = None
    #: Stripe PaymentIntent client secret, when using the embedded form.
    client_secret: str | None = None
    amount_minor: int
    currency: str
    expires_at: datetime | None = None


class PaymentVerifyRequest(BaseSchema):
    """Client-side confirmation after the Razorpay modal closes.

    This is a *convenience* path, not the source of truth. The signature is verified
    exactly as strictly as a webhook's, and the webhook still arrives and is still
    processed; whichever lands first wins, and the second is a no-op.
    """

    order_id: uuid.UUID
    provider: PaymentProvider = PaymentProvider.RAZORPAY
    provider_order_id: str = Field(max_length=191)
    provider_payment_id: str = Field(max_length=191)
    signature: str = Field(max_length=512)


class OrderCancelRequest(BaseSchema):
    reason: str | None = Field(default=None, max_length=500)


class PaymentOut(BaseSchema):
    id: uuid.UUID
    order_id: uuid.UUID
    provider: PaymentProvider
    provider_payment_id: str
    status: PaymentStatus
    amount_minor: int
    currency: str
    method: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    captured_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


class RefundCreate(BaseSchema):
    """Omit ``amount_minor`` for a full refund of what remains refundable."""

    amount_minor: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=500)
    #: Revoke the buyer's access to the books. Default true: a refunded customer
    #: keeping the file is the failure mode that costs real money.
    revoke_entitlements: bool = True


class RefundOut(BaseSchema):
    id: uuid.UUID
    order_id: uuid.UUID
    payment_id: uuid.UUID | None = None
    provider: PaymentProvider
    provider_refund_id: str | None = None
    amount_minor: int
    currency: str
    status: RefundStatus
    reason: str | None = None
    processed_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


class CouponCreate(BaseSchema):
    code: str = Field(min_length=3, max_length=64)
    description: str | None = Field(default=None, max_length=1_000)
    coupon_type: CouponType = CouponType.PERCENT
    value: int = Field(ge=0)
    max_discount_minor: int | None = Field(default=None, ge=0)
    min_order_minor: int = Field(default=0, ge=0)
    currency: Currency | None = None
    usage_limit: int | None = Field(default=None, ge=1)
    per_user_limit: int = Field(default=1, ge=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool = True
    applicable_book_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    applicable_category_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)

    @field_validator("code")
    @classmethod
    def _normalise_code(cls, value: str) -> str:
        """Upper-cased on the way in so lookups need no functional index and
        ``welcome10`` and ``WELCOME10`` cannot become two different coupons."""
        code = value.strip().upper()
        if not code.replace("-", "").replace("_", "").isalnum():
            raise ValueError("A coupon code may contain only letters, digits, '-' and '_'.")
        return code

    @model_validator(mode="after")
    def _check_value(self) -> CouponCreate:
        # `==`, not `is`: BaseSchema sets use_enum_values=True, so by the time an
        # after-validator runs the field is the plain string "percent" and an
        # identity check against the enum member would silently never fire.
        if self.coupon_type == CouponType.PERCENT and not 0 <= self.value <= 100:
            raise ValueError("A percentage coupon's value must be between 0 and 100.")
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from.")
        return self


class CouponUpdate(BaseSchema):
    """Everything optional; the code itself is immutable once issued."""

    description: str | None = Field(default=None, max_length=1_000)
    value: int | None = Field(default=None, ge=0)
    max_discount_minor: int | None = Field(default=None, ge=0)
    min_order_minor: int | None = Field(default=None, ge=0)
    usage_limit: int | None = Field(default=None, ge=1)
    per_user_limit: int | None = Field(default=None, ge=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool | None = None
    applicable_book_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    applicable_category_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


class CouponOut(BaseSchema):
    id: uuid.UUID
    code: str
    description: str | None = None
    coupon_type: CouponType
    value: int
    max_discount_minor: int | None = None
    min_order_minor: int
    currency: str | None = None
    usage_limit: int | None = None
    usage_count: int
    per_user_limit: int
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CouponValidateRequest(BaseSchema):
    code: str = Field(min_length=1, max_length=64)
    #: The cart the coupon would apply to. Required, because a coupon's validity
    #: depends on the order value and on which books are in it.
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    currency: Currency = Currency.INR


class CouponValidateResponse(BaseSchema):
    """Never raises for an invalid coupon — a wrong code is a UI state, not an error.

    ``reason`` is written for the customer to read.
    """

    valid: bool
    code: str
    discount_minor: int = 0
    subtotal_minor: int = 0
    total_minor: int = 0
    coupon_type: CouponType | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class PlanCreate(BaseSchema):
    code: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    price_minor: int = Field(ge=0)
    currency: Currency = Currency.INR
    interval: Literal["month", "year"] = "month"
    trial_days: int = Field(default=0, ge=0, le=365)
    is_active: bool = True
    provider_plan_ids: dict[str, str] = Field(default_factory=dict)


class PlanUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    price_minor: int | None = Field(default=None, ge=0)
    interval: Literal["month", "year"] | None = None
    trial_days: int | None = Field(default=None, ge=0, le=365)
    is_active: bool | None = None
    provider_plan_ids: dict[str, str] | None = None


class PlanOut(BaseSchema):
    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    price_minor: int
    currency: str
    interval: str
    trial_days: int
    is_active: bool
    created_at: datetime


class SubscriptionCreate(BaseSchema):
    plan_id: uuid.UUID
    provider: PaymentProvider | None = None
    return_url: str | None = Field(default=None, max_length=1000)


class SubscriptionOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    plan_id: uuid.UUID
    provider: PaymentProvider
    provider_subscription_id: str | None = None
    status: SubscriptionStatus
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool
    cancelled_at: datetime | None = None
    trial_ends_at: datetime | None = None
    created_at: datetime


class SubscriptionCancelRequest(BaseSchema):
    #: Default true: the customer paid through the end of the period and keeps it.
    at_period_end: bool = True
    reason: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


class InvoiceLine(BaseSchema):
    title: str
    quantity: int
    unit_price_minor: int
    line_total_minor: int


class InvoiceOut(BaseSchema):
    id: uuid.UUID
    order_id: uuid.UUID
    invoice_number: str
    subtotal_minor: int
    discount_minor: int
    tax_minor: int
    total_minor: int
    currency: str
    cgst_minor: int
    sgst_minor: int
    igst_minor: int
    tax_percent: int
    place_of_supply: str | None = None
    billing_snapshot: dict[str, Any] = Field(default_factory=dict)
    line_items: list[InvoiceLine] = Field(default_factory=list)
    issued_at: datetime


# ---------------------------------------------------------------------------
# Affiliates
# ---------------------------------------------------------------------------


class AffiliateRegister(BaseSchema):
    #: A vanity code. Left empty, one is generated.
    code: str | None = Field(default=None, min_length=3, max_length=64)

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.strip().lower()
        if not code.replace("-", "").replace("_", "").isalnum():
            raise ValueError("An affiliate code may contain only letters, digits, '-' and '_'.")
        return code


class AffiliateOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    code: str
    commission_bps: int
    is_active: bool
    total_earned_minor: int
    total_paid_minor: int
    created_at: datetime


class AffiliateConversionOut(BaseSchema):
    id: uuid.UUID
    order_id: uuid.UUID
    order_total_minor: int
    commission_minor: int
    currency: str
    status: AffiliateConversionStatus
    reversed_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Webhooks and operations
# ---------------------------------------------------------------------------


class WebhookAck(BaseSchema):
    """What a provider gets back.

    Deliberately terse and always 200 once the signature verifies: a provider that
    sees a 5xx retries with backoff for days. Processing failures are recorded on the
    ``webhook_events`` row and replayed by an operator, not by the provider.
    """

    received: bool = True
    event_id: str | None = None
    duplicate: bool = False


class WebhookEventOut(BaseSchema):
    id: uuid.UUID
    provider: PaymentProvider
    event_id: str
    event_type: str
    signature_valid: bool
    processed: bool
    processed_at: datetime | None = None
    error: str | None = None
    created_at: datetime


class RevenueSummary(BaseSchema):
    """Headline numbers for the admin dashboard. Computed in SQL, not in Python."""

    gross_minor: int
    refunded_minor: int
    net_minor: int
    tax_minor: int
    discount_minor: int
    order_count: int
    paid_order_count: int
    currency: str
    period_start: datetime | None = None
    period_end: datetime | None = None


class ProvidersResponse(BaseSchema):
    """Which gateways this deployment can actually use.

    The checkout page reads this instead of hard-coding a provider list, so a
    deployment with no Stripe credentials does not render a Stripe button.
    """

    providers: list[PaymentProvider]
    default: PaymentProvider | None = None
    currency: Currency
    razorpay_key_id: str | None = None
    stripe_publishable_key: str | None = None


class InternalOrderSummary(BaseSchema):
    """Shape returned to other services over ``/internal``."""

    id: uuid.UUID
    order_number: str
    user_id: uuid.UUID
    status: OrderStatus
    total_minor: int
    currency: str
    book_ids: list[uuid.UUID] = Field(default_factory=list)
    paid_at: datetime | None = None

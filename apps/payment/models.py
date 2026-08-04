"""SQLAlchemy models for the payment service (schema ``payment``).

**This database is the source of truth for money.** Events are announcements; the
provider's webhook is a second opinion. When they disagree, these rows win and the
discrepancy is reconciled explicitly.

Three conventions:

* **Every amount is an integer of the currency's minor unit** (paise, cents). There
  is no float or Decimal money column. Floats cannot represent 0.1, and a float
  price column produces invoices that do not reconcile.
* **Line items snapshot the price at purchase time.** A later price change must not
  retroactively alter what someone paid.
* **Nothing is hard-deleted.** Financial records are kept; refunds and cancellations
  are recorded as new state, never by removing a row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from knowledgeos_core import (
    Base,
    OrderStatus,
    PaymentProvider,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UUIDType,
)
from schemas import CouponType, PaymentStatus, RefundStatus, SubscriptionStatus

SCHEMA = "payment"

JSONType = JSON().with_variant(JSONB, "postgresql")


def _enum(enum_cls: type, name: str) -> SAEnum:
    """VARCHAR + CHECK rather than a native PostgreSQL ENUM.

    Adding a value to a native enum needs ALTER TYPE, which historically could not
    run inside a transaction; a check constraint is a plain reversible migration.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


def _money(**kwargs: object) -> Mapped[int]:
    """An amount in minor units. Never a float."""
    return mapped_column(Integer, nullable=False, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class Order(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        # The unique idempotency key is what makes a double-clicked Buy button safe.
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency_key"),
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        Index("ix_orders_status_created_at", "status", "created_at"),
        Index("ix_orders_provider_order_id", "provider_order_id"),
        CheckConstraint("subtotal_minor >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount_minor >= 0", name="discount_non_negative"),
        CheckConstraint("tax_minor >= 0", name="tax_non_negative"),
        CheckConstraint("total_minor >= 0", name="total_non_negative"),
        CheckConstraint("refunded_minor >= 0", name="refunded_non_negative"),
        CheckConstraint("refunded_minor <= total_minor", name="refund_not_above_total"),
        {"schema": SCHEMA},
    )

    #: The buyer. Owned by the auth service; no foreign key across schemas.
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    order_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus, "order_status"),
        nullable=False,
        default=OrderStatus.PENDING,
        server_default=OrderStatus.PENDING.value,
        index=True,
    )

    # ---- money (all minor units) ----
    subtotal_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    discount_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    tax_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    total_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    refunded_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # ---- tax breakdown (India GST) ----
    cgst_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    sgst_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    igst_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    tax_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    place_of_supply: Mapped[str | None] = mapped_column(String(8))

    # ---- provider ----
    provider: Mapped[PaymentProvider | None] = mapped_column(
        _enum(PaymentProvider, "payment_provider")
    )
    provider_order_id: Mapped[str | None] = mapped_column(String(191))
    checkout_url: Mapped[str | None] = mapped_column(String(1000))

    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.coupons.id", ondelete="SET NULL")
    )
    coupon_code: Mapped[str | None] = mapped_column(String(64))
    affiliate_code: Mapped[str | None] = mapped_column(String(64), index=True)

    #: Supplied by the client; the unique constraint above makes retries safe.
    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    billing_name: Mapped[str | None] = mapped_column(String(200))
    billing_email: Mapped[str | None] = mapped_column(String(320))
    billing_address: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500))

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="raise_on_sql"
    )
    refunds: Mapped[list[Refund]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="raise_on_sql"
    )

    @property
    def is_paid(self) -> bool:
        return self.status in (OrderStatus.PAID, OrderStatus.PARTIALLY_REFUNDED)

    @property
    def refundable_minor(self) -> int:
        return max(0, self.total_minor - self.refunded_minor)


class OrderItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One book on an order.

    Title and price are **snapshotted**: a later catalogue change must not alter
    what a past invoice says was sold.
    """

    __tablename__ = "order_items"
    __table_args__ = (
        Index("ix_order_items_order_id", "order_id"),
        Index("ix_order_items_book_id", "book_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_minor >= 0", name="unit_price_non_negative"),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.orders.id", ondelete="CASCADE"), nullable=False
    )
    book_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price_minor: Mapped[int] = _money()
    line_total_minor: Mapped[int] = _money()

    order: Mapped[Order] = relationship(back_populates="items", lazy="raise_on_sql")


# ---------------------------------------------------------------------------
# Payments and refunds
# ---------------------------------------------------------------------------


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (
        # One row per provider payment id: the guard against a redelivered webhook
        # recording the same capture twice.
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_ref"),
        Index("ix_payments_order_id", "order_id"),
        Index("ix_payments_status_created_at", "status", "created_at"),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.orders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)

    provider: Mapped[PaymentProvider] = mapped_column(
        _enum(PaymentProvider, "payment_provider_p"), nullable=False
    )
    provider_payment_id: Mapped[str] = mapped_column(String(191), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, "payment_status"), nullable=False, default=PaymentStatus.PENDING
    )

    amount_minor: Mapped[int] = _money()
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    method: Mapped[str | None] = mapped_column(String(40))

    #: The provider's payload as received. Kept for dispute resolution — when a
    #: customer and a provider disagree, this is the evidence.
    raw_payload: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="payments", lazy="raise_on_sql")


class Refund(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "refunds"
    __table_args__ = (
        UniqueConstraint("provider", "provider_refund_id", name="uq_refunds_provider_ref"),
        Index("ix_refunds_order_id", "order_id"),
        CheckConstraint("amount_minor > 0", name="refund_amount_positive"),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.orders.id", ondelete="CASCADE"), nullable=False
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.payments.id", ondelete="SET NULL")
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        _enum(PaymentProvider, "payment_provider_r"), nullable=False
    )
    provider_refund_id: Mapped[str | None] = mapped_column(String(191))

    amount_minor: Mapped[int] = _money()
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    status: Mapped[RefundStatus] = mapped_column(
        _enum(RefundStatus, "refund_status"), nullable=False, default=RefundStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(String(500))
    #: Who authorised it. A refund is a privileged action and is always attributable.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )

    order: Mapped[Order] = relationship(back_populates="refunds", lazy="raise_on_sql")


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


class Coupon(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "coupons"
    __table_args__ = (
        Index("ix_coupons_is_active_valid_until", "is_active", "valid_until"),
        CheckConstraint("value >= 0", name="coupon_value_non_negative"),
        CheckConstraint(
            "usage_limit IS NULL OR usage_limit > 0", name="coupon_usage_limit_positive"
        ),
        {"schema": SCHEMA},
    )

    #: Stored upper-cased so lookups are case-insensitive without a functional index.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    coupon_type: Mapped[CouponType] = mapped_column(
        _enum(CouponType, "coupon_type"), nullable=False, default=CouponType.PERCENT
    )
    #: Percent (0-100) or a fixed amount in minor units, per ``coupon_type``.
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Caps a percentage discount, so "50% off" cannot give away an unbounded amount.
    max_discount_minor: Mapped[int | None] = mapped_column(Integer)
    min_order_minor: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    currency: Mapped[str | None] = mapped_column(String(3))

    #: Total redemptions allowed across all users. NULL means unlimited.
    usage_limit: Mapped[int | None] = mapped_column(Integer)
    usage_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    per_user_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    #: Empty means "applies to everything".
    applicable_book_ids: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    applicable_category_ids: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )


class CouponRedemption(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One redemption. The unique constraint on (coupon, order) is what stops a
    retried checkout from consuming a single-use coupon twice."""

    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        UniqueConstraint("coupon_id", "order_id", name="uq_redemption_coupon_order"),
        Index("ix_coupon_redemptions_coupon_id_user_id", "coupon_id", "user_id"),
        {"schema": SCHEMA},
    )

    coupon_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.coupons.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.orders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    discount_minor: Mapped[int] = _money()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class SubscriptionPlan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "subscription_plans"
    __table_args__ = ({"schema": SCHEMA},)

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    price_minor: Mapped[int] = _money()
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    #: ``month`` or ``year``.
    interval: Mapped[str] = mapped_column(String(16), nullable=False, default="month")
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    provider_plan_ids: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )


class Subscription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_subscription_id", name="uq_subscriptions_provider_ref"
        ),
        Index("ix_subscriptions_user_id_status", "user_id", "status"),
        Index("ix_subscriptions_status_current_period_end", "status", "current_period_end"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.subscription_plans.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        _enum(PaymentProvider, "payment_provider_s"), nullable=False
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(String(191))

    status: Mapped[SubscriptionStatus] = mapped_column(
        _enum(SubscriptionStatus, "subscription_status"),
        nullable=False,
        default=SubscriptionStatus.PENDING,
    )
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Cancel at period end rather than immediately — the customer paid for it.
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A financial document. Immutable once issued — a correction is a credit note,
    never an edit."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("invoice_number", name="uq_invoices_number"),
        Index("ix_invoices_order_id", "order_id"),
        Index("ix_invoices_user_id_issued_at", "user_id", "issued_at"),
        {"schema": SCHEMA},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.orders.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    subtotal_minor: Mapped[int] = _money()
    discount_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    tax_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    total_minor: Mapped[int] = _money()
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    cgst_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    sgst_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    igst_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    tax_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    place_of_supply: Mapped[str | None] = mapped_column(String(8))

    billing_snapshot: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    line_items: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pdf_key: Mapped[str | None] = mapped_column(String(500))


# ---------------------------------------------------------------------------
# Webhooks and idempotency
# ---------------------------------------------------------------------------


class WebhookEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Every webhook received, verified or not.

    This is both the audit trail and the replay source. A provider redelivers
    aggressively; the unique constraint on (provider, event_id) is what makes
    processing exactly-once even though delivery is not.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),
        Index("ix_webhook_events_processed_created_at", "processed", "created_at"),
        {"schema": SCHEMA},
    )

    provider: Mapped[PaymentProvider] = mapped_column(
        _enum(PaymentProvider, "payment_provider_w"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(191), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    #: False means the signature did not verify. The row is still kept — a burst of
    #: these is an attack signal worth seeing.
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )


# ---------------------------------------------------------------------------
# Affiliates
# ---------------------------------------------------------------------------


class AffiliateAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "affiliate_accounts"
    __table_args__ = ({"schema": SCHEMA},)

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, unique=True, index=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    commission_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    total_earned_minor: Mapped[int] = _money(default=0, server_default=text("0"))
    total_paid_minor: Mapped[int] = _money(default=0, server_default=text("0"))


class AffiliateConversion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "affiliate_conversions"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_affiliate_conversion_order"),
        Index("ix_affiliate_conversions_account_id_created_at", "account_id", "created_at"),
        {"schema": SCHEMA},
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey(f"{SCHEMA}.affiliate_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    order_total_minor: Mapped[int] = _money()
    commission_minor: Mapped[int] = _money()
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    #: ``pending`` until the refund window closes, then ``approved``, then ``paid``.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

"""SQLAlchemy models for the notification service (schema ``notifications``).

Three tables here carry more weight than their size suggests.

**``suppressions``** is the list of addresses this platform must never contact
again — hard bounces and spam complaints. It is checked before every send and it
wins over everything, including a transactional message. Continuing to mail an
address that complained damages the sending domain's reputation for every message
the platform sends, password resets included.

**``notification_preferences``** is opt-out for transactional categories and opt-in
for everything else. The distinction is the whole point: a receipt is not marketing
and a weekly digest is not a legal obligation.

**``processed_events``** makes event consumption idempotent. Redis Streams deliver at
least once, and a duplicate ``order.paid`` must not send a customer two receipts.
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
    NotificationChannel,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UUIDType,
)
from schemas import DeliveryStatus, SuppressionReason

SCHEMA = "notifications"

JSONType = JSON().with_variant(JSONB, "postgresql")


def _enum(enum_cls: type, name: str) -> SAEnum:
    """VARCHAR + CHECK rather than a native PostgreSQL ENUM.

    Adding a channel or a status is then a plain reversible migration instead of an
    ALTER TYPE.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class Template(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A named, versioned message body per channel and locale.

    Stored rather than hard-coded so copy can be corrected without a deploy — the
    most common reason to change a notification is a typo or a tone problem, and
    neither should require a release.
    """

    __tablename__ = "templates"
    __table_args__ = (
        # One active template per (key, channel, locale). The uniqueness is what
        # makes "which template did this send use?" answerable.
        UniqueConstraint("key", "channel", "locale", name="uq_templates_key_channel_locale"),
        Index("ix_templates_category", "category"),
        {"schema": SCHEMA},
    )

    #: Stable identifier used in code, e.g. ``order.receipt``.
    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel_t"), nullable=False
    )
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    #: Governs opt-out. A key in ``transactional_categories`` cannot be disabled.
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="general")

    subject: Mapped[str | None] = mapped_column(String(300))
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: HTML alternative for email. Absent means text-only, which is a legitimate
    #: choice — plain text lands in the inbox more reliably than heavy HTML.
    body_html: Mapped[str | None] = mapped_column(Text)

    #: Variable names the template expects. Rendering fails loudly when one is
    #: missing rather than emitting "Hi {{name}}" to a real customer.
    required_variables: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    description: Mapped[str | None] = mapped_column(String(500))


# ---------------------------------------------------------------------------
# Notifications and deliveries
# ---------------------------------------------------------------------------


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One message to one person — the in-app record, and the parent of any
    outbound delivery attempts."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_id_created_at", "user_id", "created_at"),
        # The unread badge query. Partial-index candidate in production.
        Index("ix_notifications_user_id_read_at", "user_id", "read_at"),
        Index("ix_notifications_category", "category"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    template_key: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="general")

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Where clicking the notification takes the reader.
    action_url: Mapped[str | None] = mapped_column(String(1000))
    icon: Mapped[str | None] = mapped_column(String(60))

    #: The variables it was rendered from. Kept so a support conversation can
    #: reconstruct exactly what the customer saw.
    data: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Soft-hidden by the user. Kept, because the delivery record references it.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="notification", cascade="all, delete-orphan", lazy="raise_on_sql"
    )

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class Delivery(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One attempt to get one notification onto one channel.

    Separate from ``Notification`` because a single message may go out over email
    *and* push, and each can fail independently. Collapsing them would make "the
    email bounced but the push arrived" unrepresentable.
    """

    __tablename__ = "deliveries"
    __table_args__ = (
        Index("ix_deliveries_notification_id", "notification_id"),
        Index("ix_deliveries_status_next_attempt_at", "status", "next_attempt_at"),
        # Deduplicates a provider's redelivered status webhook.
        UniqueConstraint("provider", "provider_message_id", name="uq_deliveries_provider_ref"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        {"schema": SCHEMA},
    )

    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.notifications.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, index=True)
    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel_d"), nullable=False
    )
    #: Email address, phone number or device token. Kept verbatim so a failure can
    #: be traced to the exact destination that failed.
    destination: Mapped[str] = mapped_column(String(320), nullable=False)

    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.QUEUED,
        server_default=DeliveryStatus.QUEUED.value,
        index=True,
    )
    subject: Mapped[str | None] = mapped_column(String(300))
    #: The rendered body, stored in full rather than as a preview.
    #:
    #: A retry has to send the *same* message, and re-rendering would need the
    #: original variables kept somewhere anyway. Storing a truncated preview and
    #: retrying from it would quietly mail customers half a message — which is the
    #: kind of bug that only shows up when a provider has a bad afternoon.
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    body_html: Mapped[str | None] = mapped_column(Text)

    provider: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(191))

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: When a retry becomes eligible. NULL means no retry is scheduled.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(1000))

    notification: Mapped[Notification | None] = relationship(
        back_populates="deliveries", lazy="raise_on_sql"
    )


# ---------------------------------------------------------------------------
# Preferences and suppression
# ---------------------------------------------------------------------------


class Preference(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One user's opt-in state for one (category, channel) pair.

    Absence means the default applies, which is why there is no row per user per
    category at signup — writing millions of rows that all say "yes" is a way to
    make the table useless.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "category", "channel", name="uq_preferences_scope"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel_p"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Suppression(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An address this platform must not contact again.

    Checked before every send, and it wins over everything — including a
    transactional message. That is not a bug: continuing to mail an address that
    issued a spam complaint costs the sending domain its reputation, which takes
    every other message down with it.

    A user who lands here by mistake is removed by an operator, deliberately.
    """

    __tablename__ = "suppressions"
    __table_args__ = (
        UniqueConstraint("channel", "destination", name="uq_suppressions_channel_destination"),
        {"schema": SCHEMA},
    )

    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel_s"), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    reason: Mapped[SuppressionReason] = mapped_column(
        _enum(SuppressionReason, "suppression_reason"), nullable=False
    )
    #: The provider payload that caused it, for when someone disputes the block.
    detail: Mapped[str | None] = mapped_column(String(1000))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)


class DeviceToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A push target. Tokens rotate, so they are keyed by value, not by device."""

    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_device_tokens_token"),
        Index("ix_device_tokens_user_id_is_active", "user_id", "is_active"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(500), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class ProcessedEvent(Base):
    """Ledger of consumed event ids.

    Written in the same transaction as its effect, so either both land or neither
    does. Without it a redelivered ``order.paid`` sends a second receipt.
    """

    __tablename__ = "processed_events"
    __table_args__ = ({"schema": SCHEMA},)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

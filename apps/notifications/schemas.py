"""Request and response schemas for the notification service."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from knowledgeos_core import BaseSchema, NotificationChannel


class DeliveryStatus(StrEnum):
    """Lifecycle of one outbound attempt.

    ``SENT`` and ``DELIVERED`` are deliberately distinct: a provider accepting a
    message says nothing about whether a mailbox took it, and conflating them makes
    a deliverability problem invisible.
    """

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    #: Retryable. A transient provider error or a timeout.
    FAILED = "failed"
    #: Terminal. Retries are exhausted or the failure is permanent.
    BOUNCED = "bounced"
    #: Blocked before sending — suppressed address, opted out, or sending disabled.
    SKIPPED = "skipped"


class SuppressionReason(StrEnum):
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    UNSUBSCRIBED = "unsubscribed"
    #: Set by an operator, e.g. a role address that should never receive mail.
    MANUAL = "manual"
    INVALID = "invalid"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class SendRequest(BaseSchema):
    """Ask for a message to be sent. Used over ``/internal`` by sibling services.

    The caller names a **template key**, not a body. Letting a caller pass raw
    content would put copy in five services and make an unsubscribe footer
    something each of them has to remember.
    """

    user_id: uuid.UUID
    template_key: str = Field(min_length=1, max_length=120)
    #: Variables the template renders from. Missing required ones are a 422.
    variables: dict[str, Any] = Field(default_factory=dict)
    #: Omit to use every channel the user has enabled for this category.
    channels: list[NotificationChannel] | None = None
    #: Where the message should go. Omit and the caller must supply it in
    #: `variables` — this service does not hold a user directory.
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    locale: str = Field(default="en", max_length=10)
    #: Deduplicates a retried send. Reusing a key returns the original result.
    idempotency_key: str | None = Field(default=None, max_length=255)


class SendResponse(BaseSchema):
    notification_id: uuid.UUID | None = None
    #: One entry per channel attempted, including the ones that were skipped and
    #: why — a caller that only sees successes cannot tell "delivered" from
    #: "silently dropped because the user opted out".
    deliveries: list[DeliveryOut] = Field(default_factory=list)
    skipped_reason: str | None = None


class DeliveryOut(BaseSchema):
    id: uuid.UUID
    channel: NotificationChannel
    #: The address, number or token this attempt targeted. Only ever returned to
    #: the owning user or to staff — a delivery list is a list of contact details.
    destination: str
    status: DeliveryStatus
    provider: str | None = None
    provider_message_id: str | None = None
    attempts: int = 0
    error: str | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# In-app notifications
# ---------------------------------------------------------------------------


class NotificationOut(BaseSchema):
    id: uuid.UUID
    template_key: str
    category: str
    title: str
    body: str
    action_url: str | None = None
    icon: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    read_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class NotificationPage(BaseSchema):
    items: list[NotificationOut]
    next_cursor: str | None = None
    has_more: bool = False
    unread_count: int = 0


class UnreadCount(BaseSchema):
    """The badge. Its own endpoint because the frontend polls it far more often
    than it fetches the list."""

    unread: int


class MarkReadRequest(BaseSchema):
    #: Omit to mark everything read.
    notification_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


class PreferenceOut(BaseSchema):
    category: str
    channel: NotificationChannel
    enabled: bool
    #: True when the category is transactional and cannot be turned off. The UI
    #: shows the toggle disabled rather than hiding it, so the user can see the
    #: message exists and that it is not marketing.
    locked: bool = False


class PreferencesResponse(BaseSchema):
    user_id: uuid.UUID
    preferences: list[PreferenceOut]
    #: Categories a user may never opt out of, named so the UI can explain why.
    transactional_categories: list[str] = Field(default_factory=list)


class PreferenceUpdate(BaseSchema):
    category: str = Field(min_length=1, max_length=60)
    channel: NotificationChannel
    enabled: bool


class PreferencesUpdateRequest(BaseSchema):
    preferences: list[PreferenceUpdate] = Field(min_length=1, max_length=100)


class UnsubscribeRequest(BaseSchema):
    """One-click unsubscribe from an email footer.

    The token is a signed blob, never a raw user id — a URL containing an id lets
    anyone unsubscribe anyone by editing it.
    """

    token: str = Field(min_length=1, max_length=1000)
    category: str | None = Field(default=None, max_length=60)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TemplateCreate(BaseSchema):
    key: str = Field(min_length=1, max_length=120)
    channel: NotificationChannel
    locale: str = Field(default="en", max_length=10)
    category: str = Field(default="general", max_length=60)
    subject: str | None = Field(default=None, max_length=300)
    body_text: str = Field(min_length=1, max_length=100_000)
    body_html: str | None = Field(default=None, max_length=500_000)
    required_variables: list[str] = Field(default_factory=list, max_length=50)
    is_active: bool = True
    description: str | None = Field(default=None, max_length=500)

    @field_validator("key")
    @classmethod
    def _normalise_key(cls, value: str) -> str:
        key = value.strip().lower()
        if not all(part.isalnum() or part in "._-" for part in key):
            raise ValueError("A template key may contain only letters, digits, '.', '_' and '-'.")
        return key


class TemplateUpdate(BaseSchema):
    category: str | None = Field(default=None, max_length=60)
    subject: str | None = Field(default=None, max_length=300)
    body_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    body_html: str | None = Field(default=None, max_length=500_000)
    required_variables: list[str] | None = Field(default=None, max_length=50)
    is_active: bool | None = None
    description: str | None = Field(default=None, max_length=500)


class TemplateOut(BaseSchema):
    id: uuid.UUID
    key: str
    channel: NotificationChannel
    locale: str
    category: str
    subject: str | None = None
    body_text: str
    body_html: str | None = None
    required_variables: list[str] = Field(default_factory=list)
    is_active: bool
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class TemplatePreviewRequest(BaseSchema):
    """Render a template without sending it. What every copy change should go
    through before it reaches a customer."""

    variables: dict[str, Any] = Field(default_factory=dict)


class TemplatePreview(BaseSchema):
    subject: str | None = None
    body_text: str
    body_html: str | None = None
    #: Variables the template wanted that were not supplied.
    missing_variables: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Devices and suppression
# ---------------------------------------------------------------------------


class DeviceTokenRegister(BaseSchema):
    token: str = Field(min_length=8, max_length=500)
    platform: str = Field(default="web", max_length=20)


class DeviceTokenOut(BaseSchema):
    id: uuid.UUID
    platform: str
    is_active: bool
    last_seen_at: datetime | None = None
    created_at: datetime


class SuppressionOut(BaseSchema):
    id: uuid.UUID
    channel: NotificationChannel
    destination: str
    reason: SuppressionReason
    detail: str | None = None
    created_at: datetime


class SuppressionCreate(BaseSchema):
    channel: NotificationChannel = NotificationChannel.EMAIL
    destination: str = Field(min_length=3, max_length=320)
    reason: SuppressionReason = SuppressionReason.MANUAL
    detail: str | None = Field(default=None, max_length=1000)


class ChannelStatus(BaseSchema):
    """Which channels this deployment can actually use, so the preferences UI does
    not render a toggle for a channel with no provider behind it."""

    channels: list[NotificationChannel]
    sending_enabled: bool
    email_provider: str


class DeliveryStats(BaseSchema):
    window_days: int
    total: int
    sent: int
    delivered: int
    failed: int
    bounced: int
    skipped: int
    #: Of everything actually attempted. The number to watch: above a few percent
    #: and the sending domain is in trouble.
    bounce_rate: float
    by_channel: dict[str, int] = Field(default_factory=dict)

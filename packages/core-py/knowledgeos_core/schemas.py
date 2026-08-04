"""Shared Pydantic schemas and platform-wide enums.

These types are the contract the TypeScript SDK is generated from, so a change here
propagates to the frontend through ``pnpm generate:types``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class BaseSchema(BaseModel):
    """Base for every response model."""

    model_config = ConfigDict(
        from_attributes=True,  # build straight from ORM rows
        populate_by_name=True,
        str_strip_whitespace=True,
        # Enum fields come out as their plain values, so responses serialise to
        # strings without a custom encoder.
        #
        # The sharp edge: this only applies to values that were *validated*. A field
        # left at its enum default keeps the enum member. So `payload.provider.value`
        # works while the client omits the field and raises AttributeError the moment
        # they send it. Never call `.value` on a field of one of these models — every
        # enum on this platform is a StrEnum, so `str(field)` is correct either way.
        use_enum_values=True,
        # Reject unknown fields on input so a typo'd query param is a 422 rather
        # than being silently ignored.
        extra="forbid",
    )


class ErrorDetail(BaseSchema):
    code: str
    message: str
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseSchema):
    """The single error shape every service returns."""

    error: ErrorDetail


class MessageResponse(BaseSchema):
    message: str
    success: bool = True


class IdResponse(BaseSchema):
    id: uuid.UUID


class TimestampedSchema(BaseSchema):
    created_at: datetime
    updated_at: datetime


class HealthResponse(BaseSchema):
    status: str
    service: str
    version: str
    environment: str
    uptime_seconds: float


class ListResponse(BaseSchema, Generic[T]):
    items: list[T]
    total: int


# ---- domain enums shared across services --------------------------------


class UserRole(StrEnum):
    """RBAC roles, ordered from least to most privileged."""

    USER = "user"
    AUTHOR = "author"
    MODERATOR = "moderator"
    ADMIN = "admin"
    SUPERADMIN = "superadmin"


class Permission(StrEnum):
    """Fine-grained permissions. ``<resource>:<action>``; ``<resource>:*`` wildcards."""

    BOOKS_READ = "books:read"
    BOOKS_WRITE = "books:write"
    BOOKS_DELETE = "books:delete"
    BOOKS_PUBLISH = "books:publish"
    USERS_READ = "users:read"
    USERS_WRITE = "users:write"
    USERS_DELETE = "users:delete"
    ORDERS_READ = "orders:read"
    ORDERS_REFUND = "orders:refund"
    REVIEWS_MODERATE = "reviews:moderate"
    AUTOMATION_RUN = "automation:run"
    AUTOMATION_READ = "automation:read"
    ANALYTICS_READ = "analytics:read"
    SETTINGS_WRITE = "settings:write"
    AI_USE = "ai:use"


#: Default permission grants per role. The auth service embeds the resolved set in
#: the access token so downstream services authorise without a lookup.
ROLE_PERMISSIONS: dict[UserRole, list[Permission]] = {
    UserRole.USER: [Permission.BOOKS_READ, Permission.AI_USE],
    UserRole.AUTHOR: [
        Permission.BOOKS_READ,
        Permission.BOOKS_WRITE,
        Permission.AI_USE,
        Permission.AUTOMATION_RUN,
        Permission.AUTOMATION_READ,
    ],
    UserRole.MODERATOR: [
        Permission.BOOKS_READ,
        Permission.BOOKS_WRITE,
        Permission.REVIEWS_MODERATE,
        Permission.USERS_READ,
        Permission.AI_USE,
    ],
    UserRole.ADMIN: [
        Permission.BOOKS_READ,
        Permission.BOOKS_WRITE,
        Permission.BOOKS_DELETE,
        Permission.BOOKS_PUBLISH,
        Permission.USERS_READ,
        Permission.USERS_WRITE,
        Permission.ORDERS_READ,
        Permission.ORDERS_REFUND,
        Permission.REVIEWS_MODERATE,
        Permission.AUTOMATION_RUN,
        Permission.AUTOMATION_READ,
        Permission.ANALYTICS_READ,
        Permission.AI_USE,
    ],
    # superadmin bypasses the check entirely in Principal.has_permission.
    UserRole.SUPERADMIN: list(Permission),
}


class BookStatus(StrEnum):
    DRAFT = "draft"
    PROCESSING = "processing"  # in the automation pipeline
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    UNPUBLISHED = "unpublished"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class BookFormat(StrEnum):
    PDF = "pdf"
    EPUB = "epub"
    MOBI = "mobi"
    AUDIOBOOK = "audiobook"


class OrderStatus(StrEnum):
    PENDING = "pending"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class PaymentProvider(StrEnum):
    RAZORPAY = "razorpay"
    STRIPE = "stripe"
    MANUAL = "manual"


class Currency(StrEnum):
    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


class NotificationChannel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    PUSH = "push"
    IN_APP = "in_app"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class MoneyAmount(BaseSchema):
    """Money is stored and transported in **minor units** (paise, cents).

    Floats cannot represent 0.1 exactly; a float price column loses money at scale
    and produces invoices that do not reconcile. Every amount on this platform is an
    integer of the currency's smallest unit.
    """

    amount_minor: int = Field(ge=0, description="Amount in the smallest currency unit.")
    currency: Currency = Currency.INR

    @property
    def as_decimal(self) -> float:
        """Display only — never use the result for arithmetic."""
        return self.amount_minor / 100

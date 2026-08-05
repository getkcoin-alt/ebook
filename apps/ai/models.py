"""SQLAlchemy models for the AI service (schema ``ai``).

Everything here exists to answer one of two questions: **what did we spend, and
what did the model actually say?**

The first matters because this is the only service on the platform that fails by
costing money rather than by being unavailable. `generations` is the ledger every
cost figure is computed from, and it records blocked and cached requests too —
excluding them would make the cache look free and moderation look like it never ran.

The second matters because model output is non-deterministic. When a customer asks
why a book description says something odd, "we cannot reproduce it" is not an answer.
The prompt, the response and the model version are all kept.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
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
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UUIDType,
)
from schemas import GenerationStatus, MessageRole, TaskKind

SCHEMA = "ai"

JSONType = JSON().with_variant(JSONB, "postgresql")


def _enum(enum_cls: type, name: str) -> SAEnum:
    """VARCHAR + CHECK rather than a native PostgreSQL ENUM, so adding a task kind
    is a plain reversible migration instead of an ALTER TYPE."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


class Generation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One request to a model — or one that never reached a model.

    Blocked and cached requests are rows too. A ledger that only recorded successful
    calls would make moderation invisible and the cache hit-rate unmeasurable, and
    those are the two numbers that decide whether this service is affordable.
    """

    __tablename__ = "generations"
    __table_args__ = (
        Index("ix_generations_kind_created_at", "kind", "created_at"),
        Index("ix_generations_user_id_created_at", "user_id", "created_at"),
        Index("ix_generations_book_id", "book_id"),
        # The cost report's driving query.
        Index("ix_generations_created_at_status", "created_at", "status"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative"),
        CheckConstraint("cost_usd >= 0", name="cost_non_negative"),
        {"schema": SCHEMA},
    )

    kind: Mapped[TaskKind] = mapped_column(_enum(TaskKind, "task_kind"), nullable=False)
    status: Mapped[GenerationStatus] = mapped_column(
        _enum(GenerationStatus, "generation_status"),
        nullable=False,
        default=GenerationStatus.SUCCEEDED,
    )

    #: Who asked. Null for pipeline work that has no human behind it.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    book_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    #: The service that asked, for internal calls — "automation", "books".
    source: Mapped[str | None] = mapped_column(String(40))

    provider: Mapped[str | None] = mapped_column(String(40))
    #: The exact model string. Providers change what an alias points at, so
    #: "claude-sonnet-4-5" today is not the same model as "claude-sonnet-4-5" in six
    #: months — and that is the first thing to check when output quality shifts.
    model: Mapped[str | None] = mapped_column(String(120))

    #: The rendered prompt. Kept because model output is non-deterministic and
    #: "we cannot reproduce it" is not an answer to a customer complaint.
    prompt: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Estimated from published per-token pricing, not billed. Float is acceptable
    #: here precisely because it is an estimate — unlike order totals, nothing
    #: reconciles against it.
    cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    cache_key: Mapped[str | None] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(String(2000))


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A chat thread belonging to one user."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_id_updated_at", "user_id", "updated_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: Generated from the first message, so the sidebar is readable without the
    #: user naming anything.
    title: Mapped[str | None] = mapped_column(String(200))
    #: Grounded in one book, when the conversation started from a book page.
    book_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
        order_by="Message.created_at",
    )


class Message(Base, UUIDPrimaryKeyMixin):
    """One turn. Ordered by ``created_at``, which is why it is written client-side
    with microsecond precision rather than by the database clock."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
        {"schema": SCHEMA},
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole, "message_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: Book references the answer cited, so the UI can link them.
    sources: Mapped[list] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    conversation: Mapped[Conversation] = relationship(
        back_populates="messages", lazy="raise_on_sql"
    )


class CachedResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A durable cache of generated copy, alongside the Redis one.

    Redis is the fast path and is allowed to be empty — it is a cache. This table is
    what survives a Redis flush, and it matters because regenerating every book
    description on the platform after an eviction is a bill, not an inconvenience.
    """

    __tablename__ = "cached_results"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_cached_results_key"),
        Index("ix_cached_results_kind_expires_at", "kind", "expires_at"),
        {"schema": SCHEMA},
    )

    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[TaskKind] = mapped_column(_enum(TaskKind, "task_kind_c"), nullable=False)
    book_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, index=True)
    content: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    model: Mapped[str | None] = mapped_column(String(120))
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CostBudget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Spend for one day, and optionally one user.

    A materialised counter rather than a SUM over ``generations``. The budget check
    runs before *every* request, and a full scan of a growing ledger on the hot path
    is how a cost control becomes the reason the service is slow.
    """

    __tablename__ = "cost_budgets"
    __table_args__ = (
        # One row per (day, user). The platform-wide row uses a null user.
        UniqueConstraint("day", "user_id", name="uq_cost_budgets_day_user"),
        Index("ix_cost_budgets_day", "day"),
        {"schema": SCHEMA},
    )

    day: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD, UTC
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class ProcessedEvent(Base):
    """Ledger of consumed event ids, written with its effect so a redelivered
    ``book.published`` does not regenerate (and re-pay for) the same copy."""

    __tablename__ = "processed_events"
    __table_args__ = ({"schema": SCHEMA},)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class PromptTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A server-owned prompt, versioned.

    Prompts live here rather than in code for the same reason notification copy
    does: the most common reason to change one is that its output was subtly wrong,
    and that should not need a deploy. **Callers never supply a system prompt** —
    accepting one would make the platform's voice unversioned and turn every
    endpoint into a jailbreak surface.
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("kind", "locale", "version", name="uq_prompt_templates_scope"),
        {"schema": SCHEMA},
    )

    kind: Mapped[TaskKind] = mapped_column(_enum(TaskKind, "task_kind_p"), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    user_template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(String(1000))

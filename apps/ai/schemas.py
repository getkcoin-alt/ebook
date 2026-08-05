"""Request and response schemas for the AI service."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator

from knowledgeos_core import BaseSchema


class TaskKind(StrEnum):
    """What the model was asked to do.

    A closed set, because each entry maps to a server-owned prompt. A caller
    supplying its own system prompt would make the platform's voice unversioned and
    turn every endpoint into a jailbreak surface.
    """

    DESCRIPTION = "description"
    SUMMARY = "summary"
    SEO = "seo"
    TAGS = "tags"
    CATEGORIES = "categories"
    CHAT = "chat"
    RECOMMEND = "recommend"
    MODERATION = "moderation"
    EMBEDDING = "embedding"


class GenerationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Refused by moderation, before any model was called.
    BLOCKED = "blocked"
    #: Served from cache. Recorded distinctly so cache hit-rate is measurable and
    #: the cost report is not inflated by requests that cost nothing.
    CACHED = "cached"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class BookContext(BaseSchema):
    """What the model is told about a book.

    Deliberately narrow. Passing a whole book into a prompt is expensive, slow, and
    mostly wasted — the metadata below is what description, SEO and tagging actually
    need.
    """

    title: str = Field(min_length=1, max_length=500)
    subtitle: str | None = Field(default=None, max_length=500)
    authors: list[str] = Field(default_factory=list, max_length=20)
    categories: list[str] = Field(default_factory=list, max_length=20)
    language: str = "en"
    #: An excerpt, not the book. Truncated server-side to `max_input_chars`.
    excerpt: str | None = Field(default=None, max_length=200_000)
    existing_description: str | None = Field(default=None, max_length=20_000)
    page_count: int | None = Field(default=None, ge=0)


class GenerateRequest(BaseSchema):
    kind: TaskKind
    book_id: uuid.UUID | None = None
    context: BookContext
    #: Ignore a cached result and call the model again. Costs money; audited.
    force_refresh: bool = False
    locale: str = Field(default="en", max_length=10)


class GenerationOut(BaseSchema):
    id: uuid.UUID
    kind: TaskKind
    status: GenerationStatus
    book_id: uuid.UUID | None = None
    #: The generated text. Absent when the task returns structured output instead.
    content: str | None = None
    #: Structured output for tags, categories and SEO.
    data: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    #: Estimated, from the provider's published per-token pricing. Presented as an
    #: estimate everywhere, because it is one.
    cost_usd: float = 0.0
    cached: bool = False
    error: str | None = None
    created_at: datetime


class SeoResult(BaseSchema):
    meta_title: str = Field(max_length=60)
    meta_description: str = Field(max_length=160)
    keywords: list[str] = Field(default_factory=list, max_length=15)
    slug: str | None = None


class TagsResult(BaseSchema):
    tags: list[str] = Field(default_factory=list, max_length=20)


class CategoriesResult(BaseSchema):
    #: Category slugs, chosen from the taxonomy supplied in the prompt. The model is
    #: told to pick from a list rather than invent, so its output can be joined
    #: against real rows instead of fuzzy-matched.
    categories: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


class ChatMessage(BaseSchema):
    role: MessageRole
    content: str = Field(min_length=1, max_length=20_000)
    created_at: datetime | None = None


class ChatRequest(BaseSchema):
    message: str = Field(min_length=1, max_length=8_000)
    #: Continue an existing conversation. Omit to start a new one.
    conversation_id: uuid.UUID | None = None
    #: Ground the answer in one book the user has access to. Entitlement is checked
    #: server-side; a caller cannot use this to read a book they have not bought.
    book_id: uuid.UUID | None = None
    stream: bool = False


class ChatResponse(BaseSchema):
    conversation_id: uuid.UUID
    message: ChatMessage
    #: Books referenced in the answer, so the UI can link them.
    sources: list[dict[str, Any]] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ConversationOut(BaseSchema):
    id: uuid.UUID
    title: str | None = None
    book_id: uuid.UUID | None = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[ChatMessage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


class RecommendRequest(BaseSchema):
    #: Books the reader has finished or bought, as context.
    seed_book_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    #: Free-text mood or intent: "something short and funny for a flight".
    prompt: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=6, ge=1, le=20)


class Recommendation(BaseSchema):
    book_id: uuid.UUID | None = None
    title: str
    #: One sentence, written for the reader. This is the whole value of an AI
    #: recommendation over a "customers also bought" list.
    reason: str


class RecommendResponse(BaseSchema):
    recommendations: list[Recommendation] = Field(default_factory=list)
    #: True when this fell back to catalogue popularity because no model was
    #: available. The UI should not claim an AI recommendation it did not get.
    fallback: bool = False


# ---------------------------------------------------------------------------
# Moderation
# ---------------------------------------------------------------------------


class ModerationRequest(BaseSchema):
    content: str = Field(min_length=1, max_length=20_000)
    context: Literal["review", "chat", "profile", "book"] = "review"


class ModerationResult(BaseSchema):
    allowed: bool
    #: Categories that tripped, e.g. `["harassment"]`. Empty when allowed.
    flags: list[str] = Field(default_factory=list)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Written for the person who submitted the content, not for a log.
    reason: str | None = None


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class EmbeddingRequest(BaseSchema):
    texts: list[str] = Field(min_length=1, max_length=100)
    model: str | None = None


class EmbeddingResponse(BaseSchema):
    embeddings: list[list[float]]
    model: str
    dimensions: int
    input_tokens: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class UsageSummary(BaseSchema):
    window_days: int
    total_requests: int
    cached_requests: int
    failed_requests: int
    blocked_requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    #: The ceiling, so a dashboard can render a gauge rather than a bare number.
    daily_limit_usd: float
    today_cost_usd: float
    by_kind: dict[str, int] = Field(default_factory=dict)
    by_provider: dict[str, int] = Field(default_factory=dict)
    #: Requests that actually reached a provider. The denominator for "cost per
    #: generation" — dividing by the total instead makes every cache hit look like it
    #: made the model cheaper, which is the opposite of what happened.
    billable_requests: int = 0


class DailySpend(BaseSchema):
    """One day of platform-wide spend, for the cost chart."""

    day: str
    cost_usd: float
    request_count: int


class SpendHistory(BaseSchema):
    days: list[DailySpend] = Field(default_factory=list)
    daily_limit_usd: float
    total_usd: float


class FailureOut(BaseSchema):
    """A failed generation, without its prompt or its output.

    An admin console is not a place to read customers' questions, so only the parts
    that diagnose a failure are surfaced.
    """

    id: uuid.UUID
    kind: TaskKind
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    latency_ms: int | None = None
    created_at: datetime


class AiStatus(BaseSchema):
    """Whether the feature is usable right now, and honestly why not."""

    available: bool
    providers: list[str] = Field(default_factory=list)
    model: str | None = None
    budget_remaining_usd: float = 0.0
    cache_enabled: bool = True
    moderation_enabled: bool = True


class BatchGenerateRequest(BaseSchema):
    """Used by the automation pipeline, which generates several fields per book."""

    book_id: uuid.UUID
    context: BookContext
    kinds: list[TaskKind] = Field(min_length=1, max_length=6)
    force_refresh: bool = False

    @field_validator("kinds")
    @classmethod
    def _no_chat(cls, value: list[TaskKind]) -> list[TaskKind]:
        # Chat is stateful and per-user; batching it makes no sense and would
        # silently create orphan conversations.
        if TaskKind.CHAT in value:
            raise ValueError("Chat cannot be batched.")
        return value


class BatchGenerateResponse(BaseSchema):
    book_id: uuid.UUID
    results: dict[str, GenerationOut] = Field(default_factory=dict)
    #: Kinds that failed, so the pipeline can decide whether to proceed. A book with
    #: no AI description is still publishable; one with no title is not.
    failed: list[str] = Field(default_factory=list)
    total_cost_usd: float = 0.0

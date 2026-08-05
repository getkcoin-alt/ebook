"""Configuration for the AI service.

Two settings decide whether this service is safe to run in production.

``daily_cost_limit_usd`` is a hard ceiling, not a target. Every other service on
this platform fails by being unavailable; this one fails by silently spending money.
A runaway loop against a paid model is the only bug here that keeps costing after
you have gone home.

``cache_enabled`` is the difference between an affordable feature and an expensive
one. Book descriptions and summaries are generated once and read thousands of times;
regenerating them per request is pure waste, and non-determinism means the answer
changes each time for no reason a reader would understand.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    service_name: str = "ai"
    database_schema: str = "ai"
    port: int = 8004

    jwks_url: str | None = "http://localhost:8001/.well-known/jwks.json"

    # ---- providers -------------------------------------------------------
    #: Preference order. The first configured provider wins; the rest are fallbacks
    #: for when one is rate-limited or down.
    provider_order: CsvList = Field(default_factory=lambda: ["anthropic", "openai"])

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-5-20250929"
    anthropic_base_url: str = "https://api.anthropic.com"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    #: Used by the search service when semantic search is enabled.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # ---- limits ----------------------------------------------------------
    request_timeout: float = 120.0
    #: Streaming keeps a connection open far longer than a normal request.
    stream_timeout: float = 300.0
    max_output_tokens: int = 4096
    #: Cap on what a caller may submit. A 200k-token document sent to a paid model
    #: by accident is a four-figure mistake.
    max_input_chars: int = 120_000
    max_retries: int = 2

    # ---- cost control ----------------------------------------------------
    #: Hard daily ceiling across the whole platform, in USD. Reaching it returns
    #: 503 rather than continuing to spend.
    daily_cost_limit_usd: float = 50.0
    #: Per-user daily ceiling, so one account cannot consume the platform budget.
    user_daily_cost_limit_usd: float = 2.0
    #: Below this, requests are served but a warning is logged — the signal that
    #: someone should look before the ceiling stops the feature entirely.
    cost_warning_threshold: float = 0.8
    cost_tracking_enabled: bool = True

    # ---- caching ---------------------------------------------------------
    cache_enabled: bool = True
    #: Generated copy for a book changes only when the book does.
    cache_ttl: int = 86_400
    #: Chat answers vary by conversation, so they are cached far more briefly —
    #: only long enough to absorb a double-submit.
    chat_cache_ttl: int = 60

    # ---- moderation ------------------------------------------------------
    moderation_enabled: bool = True
    #: Refuse rather than forward when moderation itself is unavailable. The
    #: alternative is an unmoderated path that opens exactly when it is needed most.
    moderation_fail_closed: bool = True

    # ---- retention -------------------------------------------------------
    conversation_retention_days: int = 90
    max_conversation_messages: int = 100
    max_conversations_per_user: int = 50

    # ---- events ----------------------------------------------------------
    events_enabled: bool = True
    event_consumer_group: str = "ai"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def anthropic_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def enabled_providers(self) -> list[str]:
        """Configured providers, in preference order."""
        available = {"anthropic": self.anthropic_enabled, "openai": self.openai_enabled}
        return [name for name in self.provider_order if available.get(name)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ai_available(self) -> bool:
        return bool(self.enabled_providers)


settings = Settings()

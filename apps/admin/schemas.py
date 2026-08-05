"""Request and response schemas for the admin service."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from knowledgeos_core import BaseSchema

FLAG_KEY_PATTERN = r"^[a-z][a-z0-9_.-]{1,80}$"


class PanelStatus(StrEnum):
    OK = "ok"
    #: The service answered, but with an error. Its data is not shown.
    ERROR = "error"
    #: No answer inside the panel budget.
    TIMEOUT = "timeout"
    #: Not configured on this deployment. Rendered as absent, not broken — a
    #: deployment without an AI provider should not show a red card forever.
    UNAVAILABLE = "unavailable"


class Panel(BaseSchema):
    """One service's contribution to the dashboard.

    Every panel carries its own status. A dashboard that returns one status for the
    whole page is a dashboard that goes red because one optional service is
    restarting, and then nobody can see the seven panels that are fine.
    """

    service: str
    status: PanelStatus
    data: dict[str, Any] = Field(default_factory=dict)
    #: Set when the status is not OK. Short, and never the upstream's stack trace.
    error: str | None = None
    latency_ms: int | None = None


class Dashboard(BaseSchema):
    generated_at: datetime
    window_days: int
    panels: list[Panel]
    #: True when every panel answered. The single number a status page wants.
    complete: bool = True
    cached: bool = False


class ServiceHealth(BaseSchema):
    service: str
    status: str
    version: str | None = None
    latency_ms: int | None = None
    detail: str | None = None


class HealthBoard(BaseSchema):
    generated_at: datetime
    services: list[ServiceHealth]
    healthy: int
    degraded: int
    cached: bool = False


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------


class FlagCreate(BaseSchema):
    key: str = Field(min_length=2, max_length=80)
    enabled: bool = False
    description: str = Field(default="", max_length=500)
    #: Percentage of users the flag is on for, 0-100. A flag with a rollout is
    #: evaluated per user; one without is simply on or off for everyone.
    rollout_percent: int = Field(default=100, ge=0, le=100)
    #: Always on for these user ids, whatever the rollout says. How you test a flag
    #: in production without turning it on for everybody.
    allowlist: list[uuid.UUID] = Field(default_factory=list, max_length=200)

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        # Constrained because the key is embedded in cache keys and read by every
        # service. A key with a colon or a space in it produces a cache collision
        # that is very hard to see.
        key = value.strip().lower()
        if not re.match(FLAG_KEY_PATTERN, key):
            raise ValueError(
                "A flag key is lower-case letters, digits, dot, dash and underscore, "
                "starting with a letter."
            )
        return key


class FlagUpdate(BaseSchema):
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=500)
    rollout_percent: int | None = Field(default=None, ge=0, le=100)
    allowlist: list[uuid.UUID] | None = Field(default=None, max_length=200)
    #: Why. Recorded on the audit row — "who turned payments off" is a question that
    #: always arrives with "and why".
    reason: str = Field(default="", max_length=500)


class FlagOut(BaseSchema):
    key: str
    enabled: bool
    description: str = ""
    rollout_percent: int = 100
    allowlist: list[uuid.UUID] = Field(default_factory=list)
    updated_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class FlagEvaluation(BaseSchema):
    """What a sibling service gets back.

    The decision, not the rule. A service that received the rollout percentage would
    have to implement the bucketing itself, and two implementations of a hash bucket
    diverge — which shows up as a user who has the feature on one page and not on the
    next.
    """

    key: str
    enabled: bool
    #: Why the answer is what it is. For debugging a flag that "is not working".
    reason: str


class FlagBatchRequest(BaseSchema):
    keys: list[str] = Field(min_length=1, max_length=100)
    user_id: uuid.UUID | None = None


class FlagBatchResponse(BaseSchema):
    flags: dict[str, bool] = Field(default_factory=dict)
    #: Keys that do not exist. Reported rather than silently defaulted to false, so a
    #: typo in a service's flag name is visible instead of looking like "off".
    unknown: list[str] = Field(default_factory=list)


class FlagAuditOut(BaseSchema):
    id: uuid.UUID
    flag_key: str
    action: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    actor_id: uuid.UUID | None = None
    created_at: datetime


class FlagAuditPage(BaseSchema):
    items: list[FlagAuditOut]
    total: int
    limit: int
    offset: int

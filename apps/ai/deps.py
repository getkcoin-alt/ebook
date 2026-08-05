"""Service-local dependencies for the AI service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Query

from knowledgeos_core.deps import Ctx, CurrentUser, DbSession, OptionalUser
from knowledgeos_core.security import Principal
from services import (
    BudgetService,
    ChatService,
    Generator,
    ModerationService,
    ProviderRegistry,
    ReportingService,
)

__all__ = [
    "Budget",
    "Chat",
    "CurrentUser",
    "DbSession",
    "Generate",
    "Moderation",
    "OptionalUser",
    "Providers",
    "Reporting",
    "user_uuid",
]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


Generate = Annotated[Generator, Depends(_extra("generator"))]
Chat = Annotated[ChatService, Depends(_extra("chat"))]
Moderation = Annotated[ModerationService, Depends(_extra("moderation"))]
Budget = Annotated[BudgetService, Depends(_extra("budget"))]
Providers = Annotated[ProviderRegistry, Depends(_extra("providers"))]
Reporting = Annotated[ReportingService, Depends(_extra("reporting"))]


def user_uuid(principal: Principal) -> uuid.UUID:
    """The caller's id, from the token.

    Also the key the per-user cost ceiling is counted against, which is why it can
    never come from a request field.
    """
    return uuid.UUID(principal.user_id)


def page_limit(
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
) -> int:
    return limit


PageLimit = Annotated[int, Depends(page_limit)]

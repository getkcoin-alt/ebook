"""Service-local dependencies for the notification service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Query

from knowledgeos_core.deps import Ctx, CurrentUser, DbSession, OptionalUser
from knowledgeos_core.security import Principal
from services import ChannelRegistry, Dispatcher, InboxService, PreferenceService, TemplateService

__all__ = [
    "Channels",
    "CurrentUser",
    "DbSession",
    "Dispatch",
    "Inbox",
    "OptionalUser",
    "Preferences",
    "Templates",
    "cursor_limit",
    "user_uuid",
]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


Dispatch = Annotated[Dispatcher, Depends(_extra("dispatcher"))]
Inbox = Annotated[InboxService, Depends(_extra("inbox"))]
Templates = Annotated[TemplateService, Depends(_extra("templates"))]
Preferences = Annotated[PreferenceService, Depends(_extra("preferences"))]
Channels = Annotated[ChannelRegistry, Depends(_extra("channels"))]


def user_uuid(principal: Principal) -> uuid.UUID:
    """The caller's id, from the token.

    Never from a request field. A notification list is a list of things that
    happened to one person, and taking the id from the body would let anyone read
    anyone's.
    """
    return uuid.UUID(principal.user_id)


def cursor_limit(
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
) -> int:
    return limit


def page_offset(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> tuple[int, int]:
    return limit, offset


CursorLimit = Annotated[int, Depends(cursor_limit)]
PageOffset = Annotated[tuple[int, int], Depends(page_offset)]

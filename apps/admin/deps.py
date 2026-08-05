"""Service-local dependencies for the admin service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from knowledgeos_core.deps import Ctx, CurrentUser, DbSession
from services import Aggregator, FlagService

__all__ = [
    "Aggregate",
    "CallerToken",
    "CurrentUser",
    "DbSession",
    "Flags",
    "PageOffset",
    "WindowDays",
]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


Aggregate = Annotated[Aggregator, Depends(_extra("aggregator"))]
Flags = Annotated[FlagService, Depends(_extra("flags"))]


def caller_token(request: Request) -> str | None:
    """The raw bearer token from the incoming request.

    Forwarded to sibling services so each applies its own permission check. Without
    it this service would be a confused deputy: reachable by anyone it lets in, and
    holding an HMAC key that opens every `/internal/*` route on the platform.

    Returned rather than raised on absence — a route that needs a principal already
    depends on `CurrentUser`, and this is only ever an addition to that.
    """
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


CallerToken = Annotated[str | None, Depends(caller_token)]


def window_days(
    days: Annotated[int, Query(ge=1, le=90, description="Reporting window.")] = 7,
) -> int:
    return days


WindowDays = Annotated[int, Depends(window_days)]


def page_offset(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> tuple[int, int]:
    return limit, offset


PageOffset = Annotated[tuple[int, int], Depends(page_offset)]

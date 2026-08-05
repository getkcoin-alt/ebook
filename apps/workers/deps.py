"""Service-local dependencies for the workers service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query

from knowledgeos_core.deps import Ctx, CurrentUser, DbSession
from services import HistoryService, JobRunner

__all__ = ["CurrentUser", "DbSession", "History", "PageOffset", "Runner"]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


History = Annotated[HistoryService, Depends(_extra("history"))]
Runner = Annotated[JobRunner, Depends(_extra("runner"))]


def page_offset(
    limit: Annotated[int, Query(ge=1, le=200, description="Rows per page.")] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> tuple[int, int]:
    return limit, offset


PageOffset = Annotated[tuple[int, int], Depends(page_offset)]

"""Service-local dependencies for the automation service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query

from knowledgeos_core.deps import Ctx, CurrentUser, DbSession
from services import ImportService, JobService, PipelineClients, PipelineRunner

__all__ = [
    "Clients",
    "CurrentUser",
    "DbSession",
    "Imports",
    "Jobs",
    "PageOffset",
    "Runner",
]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


Jobs = Annotated[JobService, Depends(_extra("jobs"))]
Runner = Annotated[PipelineRunner, Depends(_extra("runner"))]
Imports = Annotated[ImportService, Depends(_extra("imports"))]
Clients = Annotated[PipelineClients, Depends(_extra("clients"))]


def page_offset(
    limit: Annotated[int, Query(ge=1, le=100, description="Rows per page.")] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> tuple[int, int]:
    """Offset paging.

    This is an operator console over a table that is pruned on a schedule, not a
    public feed. The depth a keyset cursor protects against is not reachable, and an
    operator wants to jump to page four.
    """
    return limit, offset


PageOffset = Annotated[tuple[int, int], Depends(page_offset)]

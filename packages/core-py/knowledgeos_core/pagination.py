"""Pagination, filtering and sorting helpers.

Two strategies, deliberately both supported:

* **Page/limit** — for admin tables and anywhere a user expects "page 4 of 27".
  Costs a ``COUNT(*)``; fine on filtered admin queries.
* **Cursor** — for infinite scroll and public catalogue feeds. Keyset pagination on
  ``(sort_key, id)`` stays O(limit) no matter how deep the user scrolls, and never
  skips or duplicates rows when new books are inserted mid-scroll.

The frontend's infinite-scroll views must use cursors; ``OFFSET 40000`` on a
million-row catalogue is what takes a database down.
"""

from __future__ import annotations

import base64
from typing import Any, Generic, TypeVar

import orjson
from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import BadRequestError

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class PageParams(BaseModel):
    """Offset pagination query parameters."""

    page: int = Field(default=1, ge=1, le=10_000, description="1-indexed page number.")
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


class SortParams(BaseModel):
    sort_by: str = Field(default="created_at")
    sort_order: str = Field(default="desc", pattern="^(asc|desc)$")


class PageMeta(BaseModel):
    page: int
    limit: int
    total: int
    pages: int
    has_next: bool
    has_prev: bool


class Page(BaseModel, Generic[T]):
    """Standard offset-paginated envelope returned by every list endpoint."""

    items: list[T]
    meta: PageMeta

    @classmethod
    def create(cls, items: list[T], *, total: int, params: PageParams) -> Page[T]:
        pages = (total + params.limit - 1) // params.limit if total else 0
        return cls(
            items=items,
            meta=PageMeta(
                page=params.page,
                limit=params.limit,
                total=total,
                pages=pages,
                has_next=params.page < pages,
                has_prev=params.page > 1,
            ),
        )


class CursorPage(BaseModel, Generic[T]):
    """Keyset-paginated envelope for infinite scroll."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


def encode_cursor(payload: dict[str, Any]) -> str:
    """Opaque, URL-safe cursor.

    Base64 is obfuscation, not security: a client that decodes and edits a cursor can
    only reposition itself within data it is already authorised to read, because the
    query's own filters are applied server-side regardless of the cursor.
    """
    return base64.urlsafe_b64encode(orjson.dumps(payload)).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = orjson.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise BadRequestError("The pagination cursor is malformed.") from exc
    if not isinstance(decoded, dict):
        raise BadRequestError("The pagination cursor is malformed.")
    return decoded


def page_params(
    page: int = Query(1, ge=1, le=10_000, description="1-indexed page number."),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Items per page."),
) -> PageParams:
    """FastAPI dependency for offset pagination."""
    return PageParams(page=page, limit=limit)


def apply_sorting(
    stmt: Select[Any],
    model: type[Any],
    *,
    sort_by: str,
    sort_order: str = "desc",
    allowed: set[str],
    tiebreaker: str = "id",
) -> Select[Any]:
    """Apply ORDER BY from user input.

    ``allowed`` is a strict whitelist of column names — user input is never
    interpolated into SQL, it selects from a set the endpoint declares.

    A tiebreaker column is always appended: sorting only by a non-unique column
    (``rating``, ``created_at``) gives PostgreSQL no defined order for ties, so the
    same row can appear on two pages.
    """
    if sort_by not in allowed:
        raise BadRequestError(
            f"Cannot sort by '{sort_by}'.",
            details={"allowed": sorted(allowed)},
        )
    column = getattr(model, sort_by)
    direction = asc if sort_order == "asc" else desc
    stmt = stmt.order_by(direction(column))
    if sort_by != tiebreaker and hasattr(model, tiebreaker):
        stmt = stmt.order_by(direction(getattr(model, tiebreaker)))
    return stmt


def apply_search(
    stmt: Select[Any], model: type[Any], *, term: str, fields: list[str]
) -> Select[Any]:
    """Case-insensitive ILIKE across the given columns.

    This is a fallback for admin screens only. Catalogue search goes through
    Meilisearch — ``ILIKE '%term%'`` cannot use a btree index and degrades badly.
    """
    if not term:
        return stmt
    # Escape LIKE wildcards so a user searching for "100%" does not match everything.
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    clauses = [getattr(model, f).ilike(pattern, escape="\\") for f in fields if hasattr(model, f)]
    return stmt.where(or_(*clauses)) if clauses else stmt


async def paginate(
    session: AsyncSession, stmt: Select[Any], params: PageParams
) -> tuple[list[Any], int]:
    """Execute a statement with a matching COUNT. Returns ``(rows, total)``.

    The count reuses the filtered statement with ORDER BY stripped — ordering is
    irrelevant to a count and PostgreSQL would otherwise sort the whole set.
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one())
    if total == 0:
        return [], 0
    result = await session.execute(stmt.offset(params.offset).limit(params.limit))
    return list(result.scalars().unique().all()), total

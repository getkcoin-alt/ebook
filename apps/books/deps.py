"""Service-local dependencies for the book service."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Query

from knowledgeos_core.deps import Ctx, CurrentUser, DbSession, OptionalUser
from knowledgeos_core.security import Principal
from services import (
    CatalogueCache,
    CatalogueService,
    EntitlementService,
    ListService,
    ReadingService,
    ReviewService,
    TaxonomyService,
)
from services.catalogue import BookFilters
from settings import settings

__all__ = [
    "Catalogue",
    "CurrentUser",
    "DbSession",
    "Entitlements",
    "Lists",
    "OptionalUser",
    "Reading",
    "Reviews",
    "Taxonomy",
    "catalogue_filters",
    "cursor_limit",
    "user_uuid",
]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


Catalogue = Annotated[CatalogueService, Depends(_extra("catalogue"))]
Taxonomy = Annotated[TaxonomyService, Depends(_extra("taxonomy"))]
Reviews = Annotated[ReviewService, Depends(_extra("reviews"))]
Reading = Annotated[ReadingService, Depends(_extra("reading"))]
Lists = Annotated[ListService, Depends(_extra("lists"))]
Entitlements = Annotated[EntitlementService, Depends(_extra("entitlements"))]
Cache = Annotated[CatalogueCache, Depends(_extra("cache"))]


def user_uuid(principal: Principal) -> uuid.UUID:
    """The authenticated caller's id.

    Ownership always comes from the token, never from a request field — otherwise
    any user could read another's library by passing a different id.
    """
    return uuid.UUID(principal.user_id)


def cursor_limit(
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
) -> int:
    return min(limit, settings.max_cursor_limit)


def catalogue_filters(
    q: Annotated[str | None, Query(max_length=200, description="Free-text title match.")] = None,
    category: Annotated[str | None, Query(max_length=120, description="Category slug.")] = None,
    author: Annotated[str | None, Query(max_length=120, description="Author slug.")] = None,
    publisher: Annotated[str | None, Query(max_length=120, description="Publisher slug.")] = None,
    language: Annotated[str | None, Query(max_length=16)] = None,
    book_format: Annotated[str | None, Query(alias="format", max_length=16)] = None,
    min_price_minor: Annotated[int | None, Query(ge=0)] = None,
    max_price_minor: Annotated[int | None, Query(ge=0)] = None,
    free_only: Annotated[bool, Query()] = False,
) -> BookFilters:
    """Catalogue filters, applied server-side.

    `status` is deliberately absent: a public caller must not be able to ask for
    drafts. Admin listings set it explicitly on their own route.
    """
    return BookFilters(
        q=q,
        category=category,
        author=author,
        publisher=publisher,
        language=language,
        book_format=book_format,
        min_price_minor=min_price_minor,
        max_price_minor=max_price_minor,
        free_only=free_only,
    )


CatalogueFilters = Annotated[BookFilters, Depends(catalogue_filters)]
CursorLimit = Annotated[int, Depends(cursor_limit)]

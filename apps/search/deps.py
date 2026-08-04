"""Service-local dependencies for the search service.

The query-parameter dependencies here do more work than usual, and deliberately: a
search box is the most hostile input surface on the platform. Everything a caller can
influence — filter names, sort orders, page depth, result size — is validated against
a closed set *before* it reaches the engine, so a crafted query is a 400 from us
rather than a 400 from Meilisearch presented as a 500.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Query, Request

from knowledgeos_core import BadRequestError
from knowledgeos_core.deps import Ctx, CurrentUser, DbSession, OptionalUser
from schemas import SortOption
from services import (
    AnalyticsService,
    CatalogueGateway,
    Indexer,
    MeiliClient,
    QueryService,
    SearchParams,
    TrendingService,
)
from services.query import FILTERABLE
from settings import settings

__all__ = [
    "Analytics",
    "Catalogue",
    "CurrentUser",
    "DbSession",
    "Indexing",
    "Meili",
    "OptionalUser",
    "Queries",
    "SearchQueryParams",
    "Trending",
    "client_session_id",
    "search_params",
]


def _extra(name: str):  # type: ignore[no-untyped-def]
    def getter(ctx: Ctx):  # type: ignore[no-untyped-def]
        return ctx.extras[name]

    return getter


Queries = Annotated[QueryService, Depends(_extra("queries"))]
Indexing = Annotated[Indexer, Depends(_extra("indexer"))]
Trending = Annotated[TrendingService, Depends(_extra("trending"))]
Analytics = Annotated[AnalyticsService, Depends(_extra("analytics"))]
Meili = Annotated[MeiliClient, Depends(_extra("meili"))]
Catalogue = Annotated[CatalogueGateway, Depends(_extra("catalogue"))]


def _parse_filters(raw: list[str] | None) -> dict[str, list[str]]:
    """Turn repeated ``filter=name:value`` parameters into the query shape.

    Repeated parameters rather than a JSON blob, because that is what an HTML form
    and a plain `<a href>` produce — a facet sidebar should be usable without
    JavaScript, and shareable as a URL.

    Values for the same name are ORed, different names are ANDed. That is what a
    facet UI means when a user ticks two categories and one language.
    """
    parsed: dict[str, list[str]] = {}
    for item in raw or []:
        name, separator, value = item.partition(":")
        name, value = name.strip().lower(), value.strip()
        if not separator or not name or not value:
            raise BadRequestError(
                "A filter must be written as 'name:value'.",
                details={"received": item, "allowed": sorted(FILTERABLE)},
            )
        if name not in FILTERABLE:
            # Checked here as well as in build_filter, so the error names the
            # parameter the caller actually sent.
            raise BadRequestError(
                f"Cannot filter by '{name}'.", details={"allowed": sorted(FILTERABLE)}
            )
        if len(value) > 120:
            raise BadRequestError("That filter value is too long.", details={"filter": name})
        parsed.setdefault(name, []).append(value)
    return parsed


def search_params(
    q: Annotated[str, Query(max_length=200, description="Free-text query.")] = "",
    filter: Annotated[
        list[str] | None,
        Query(
            description=(
                "Repeatable `name:value`. Same name ORs, different names AND. "
                "Allowed names: category, author, language, format, tag, publisher."
            )
        ),
    ] = None,
    sort: Annotated[SortOption, Query()] = SortOption.RELEVANCE,
    min_price_minor: Annotated[int | None, Query(ge=0)] = None,
    max_price_minor: Annotated[int | None, Query(ge=0)] = None,
    min_rating: Annotated[float | None, Query(ge=0, le=5)] = None,
    free_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    facets: Annotated[bool, Query(description="Return facet counts alongside hits.")] = True,
    semantic: Annotated[
        bool | None,
        Query(description="Force hybrid search on for this request, when configured."),
    ] = None,
) -> SearchParams:
    if (
        min_price_minor is not None
        and max_price_minor is not None
        and min_price_minor > max_price_minor
    ):
        raise BadRequestError(
            "min_price_minor cannot be greater than max_price_minor.",
            details={"min_price_minor": min_price_minor, "max_price_minor": max_price_minor},
        )
    return SearchParams(
        query=q.strip(),
        filters=_parse_filters(filter),
        min_price_minor=min_price_minor,
        max_price_minor=max_price_minor,
        min_rating=min_rating,
        free_only=free_only,
        sort=SortOption(sort),
        limit=min(limit, settings.search_max_limit),
        cursor=cursor,
        facets=facets,
        semantic=semantic,
    )


def client_session_id(request: Request) -> str | None:
    """An opaque per-browser id for analytics, from a header the frontend sets.

    Never the IP address: grouping searches by IP would build a behavioural profile
    keyed to a person, for a report that only needs to know two searches came from
    the same tab.
    """
    value = request.headers.get("x-session-id")
    return value[:64] if value else None


def optional_user_uuid(principal: OptionalUser = None) -> uuid.UUID | None:
    if principal is None:
        return None
    try:
        return uuid.UUID(principal.user_id)
    except (ValueError, AttributeError):
        return None


SearchQueryParams = Annotated[SearchParams, Depends(search_params)]
SessionId = Annotated[str | None, Depends(client_session_id)]
OptionalUserId = Annotated[uuid.UUID | None, Depends(optional_user_uuid)]

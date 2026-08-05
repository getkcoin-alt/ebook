"""Public catalogue: browsing, book detail, related books, downloads."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from deps import (
    Catalogue,
    CatalogueFilters,
    CurrentUser,
    CursorLimit,
    DbSession,
    Entitlements,
    OptionalUser,
    user_uuid,
)
from knowledgeos_core import NotFoundError, get_logger
from knowledgeos_core.deps import Storage, rate_limit
from knowledgeos_core.schemas import BookFormat
from schemas import (
    AccessOut,
    BookDetail,
    BookListItem,
    CataloguePage,
    DownloadTicket,
    LibraryItem,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/books", tags=["catalogue"])

#: Which format to hand over when the caller does not name one. PDF first because it
#: renders identically everywhere, EPUB next because it reflows on a phone. Audiobook
#: is absent on purpose: it is never the thing someone means by "download the book"
#: when a text format also exists.
_DOWNLOAD_PREFERENCE: tuple[BookFormat, ...] = (
    BookFormat.PDF,
    BookFormat.EPUB,
    BookFormat.MOBI,
)


@router.get(
    "",
    response_model=CataloguePage,
    summary="Browse the catalogue",
    description=(
        "Cursor-paginated feed of published books. Filters are applied server-side "
        "and facet counts, when requested, are computed under the same filters as "
        "the page they accompany.\n\n"
        "There is no `total`: counting a filtered catalogue costs a full scan on "
        "every page, and infinite scroll never shows the number."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def list_books(
    session: DbSession,
    catalogue: Catalogue,
    filters: CatalogueFilters,
    limit: CursorLimit,
    cursor: Annotated[
        str | None, Query(description="Opaque cursor from the previous page.")
    ] = None,
    sort_by: Annotated[
        str, Query(description="published_at | title | price | rating | views")
    ] = "published_at",
    sort_order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    include_facets: Annotated[bool, Query(description="Also compute filter counts.")] = False,
) -> CataloguePage:
    rows, next_cursor, has_more = await catalogue.list_books(
        session,
        filters=filters,
        sort_by=sort_by,
        sort_order=sort_order,
        cursor=cursor,
        limit=limit,
        public=True,
    )
    facets = await catalogue.facets(session, filters, public=True) if include_facets else None
    return CataloguePage(
        items=[BookListItem.model_validate(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        facets=facets,
    )


@router.get(
    "/{slug}",
    response_model=BookDetail,
    summary="Get a book by slug",
    description=(
        "Slug rather than id, because this is the URL a reader shares and a search "
        "engine indexes. Cached; the cache is invalidated on publish and update."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def get_book(
    slug: str,
    session: DbSession,
    catalogue: Catalogue,
    user: OptionalUser,
) -> BookDetail:
    # Staff may see unpublished books at their canonical URL; everyone else 404s,
    # which is also what stops an unpublished slug from being probeable.
    include_unpublished = bool(user and user.has_permission("books:write"))
    book = await catalogue.get_by_slug(session, slug, include_unpublished=include_unpublished)
    return BookDetail.model_validate(book)


@router.get(
    "/{slug}/related",
    response_model=list[BookListItem],
    summary="Books readers also liked",
    description=(
        "Books sharing a category, best-rated first. Deliberately not a recommender "
        "— this strip must render from one index scan, not a model call."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def related_books(
    slug: str,
    session: DbSession,
    catalogue: Catalogue,
    limit: Annotated[int, Query(ge=1, le=24)] = 12,
) -> list[BookListItem]:
    book = await catalogue.get_by_slug(session, slug)
    rows = await catalogue.related(session, book, limit=limit)
    return [BookListItem.model_validate(row) for row in rows]


@router.get(
    "/{book_id}/access",
    response_model=AccessOut,
    summary="Can the signed-in user read this book?",
    description="Lets the UI render 'Read now' or 'Buy' without attempting a download.",
)
async def check_access(
    book_id: uuid.UUID,
    session: DbSession,
    catalogue: Catalogue,
    entitlements: Entitlements,
    user: CurrentUser,
) -> AccessOut:
    book = await catalogue.get_by_id(session, book_id)
    return await entitlements.access(session, user_id=user_uuid(user), book=book)


@router.get(
    "/{book_id}/download",
    response_model=DownloadTicket,
    summary="Get a signed download URL",
    description=(
        "Checks entitlement, then returns a short-lived presigned URL. The file "
        "itself never passes through this service — proxying a 40MB PDF would "
        "occupy a worker for the whole transfer.\n\n"
        "402 when the book must be bought, 403 when access exists but is read-only.\n\n"
        "`format` is optional. Omit it and the book's own preferred format is used; "
        "ask for one it does not have and the answer is 404 naming what it does have."
    ),
    dependencies=[Depends(rate_limit("authenticated"))],
)
async def download_book(
    book_id: uuid.UUID,
    session: DbSession,
    catalogue: Catalogue,
    entitlements: Entitlements,
    storage: Storage,
    user: CurrentUser,
    book_format: Annotated[BookFormat | None, Query(alias="format")] = None,
) -> DownloadTicket:
    from settings import settings

    book = await catalogue.get_by_id(session, book_id)
    # Entitlement is checked BEFORE a URL is minted: the URL is the capability, and
    # anyone holding it can read the object until it expires.
    await entitlements.require_download(session, user_id=user_uuid(user), book=book)

    if book_format is None:
        # No format asked for, so serve what the book actually has. This defaulted to
        # PDF, which 404s an EPUB-only title for a customer who has paid for it — a
        # confusing answer to give someone holding a valid entitlement, and one the
        # book itself has the information to avoid.
        preferred = next(
            (fmt for fmt in _DOWNLOAD_PREFERENCE if fmt.value in book.available_formats),
            None,
        )
        if preferred is None:
            raise NotFoundError(
                "This book has no downloadable file.",
                details={"book_id": str(book_id), "available": book.available_formats},
            )
        book_format = preferred

    key = book.file_key_for(book_format.value)
    if not key:
        raise NotFoundError(
            f"This book is not available as {book_format.value}.",
            details={"book_id": str(book_id), "available": book.available_formats},
        )

    ttl = settings.download_url_ttl
    filename = f"{book.slug}.{book_format.value}"
    url = await storage.signed_download_url(key, expires_in=ttl, download_filename=filename)
    await catalogue.increment_download_count(session, book.id)

    logger.info(
        "book.download_issued",
        book_id=str(book.id),
        user_id=user.user_id,
        book_format=book_format.value,
    )
    return DownloadTicket(
        book_id=book.id,
        format=book_format,
        url=url,
        expires_in=ttl,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        filename=filename,
    )


library_router = APIRouter(prefix="/v1/library", tags=["library"])


@library_router.get(
    "",
    response_model=CataloguePage,
    summary="Books the signed-in user can read",
    description="Keyset-paginated on grant time — a long-standing customer's shelf is not a small table either.",
)
async def my_library(
    session: DbSession,
    entitlements: Entitlements,
    user: CurrentUser,
    limit: CursorLimit,
    cursor: Annotated[str | None, Query()] = None,
) -> CataloguePage:
    rows, next_cursor, has_more = await entitlements.library(
        session, user_id=user_uuid(user), cursor=cursor, limit=limit
    )
    return CataloguePage(
        items=[BookListItem.model_validate(book) for _entitlement, book in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@library_router.get(
    "/detailed",
    response_model=list[LibraryItem],
    summary="Library with entitlement and reading progress",
)
async def my_library_detailed(
    session: DbSession,
    entitlements: Entitlements,
    user: CurrentUser,
    limit: CursorLimit,
    cursor: Annotated[str | None, Query()] = None,
) -> list[LibraryItem]:
    uid = user_uuid(user)
    rows, _next_cursor, _has_more = await entitlements.library(
        session, user_id=uid, cursor=cursor, limit=limit
    )
    progress = await entitlements.progress_percent_for(
        session, user_id=uid, book_ids=[book.id for _e, book in rows]
    )
    return [
        LibraryItem(
            book=BookListItem.model_validate(book),
            source=entitlement.source,
            granted_at=entitlement.granted_at,
            expires_at=entitlement.expires_at,
            can_download=entitlement.can_download,
            progress_percent=progress.get(book.id, 0.0),
        )
        for entitlement, book in rows
    ]

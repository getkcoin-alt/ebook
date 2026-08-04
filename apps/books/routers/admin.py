"""Catalogue administration and service-to-service endpoints.

Admin routes require `books:write` / `books:publish` / `books:delete`. The
`/internal/*` routes require an HMAC signature — private-network reachability is not
authorisation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from deps import (
    Catalogue,
    CatalogueFilters,
    CurrentUser,
    CursorLimit,
    DbSession,
    Entitlements,
    Taxonomy,
)
from knowledgeos_core import (
    EventType,
    MessageResponse,
    PageParams,
    encode_cursor,
    get_logger,
    page_params,
)
from knowledgeos_core.deps import Ctx, InternalCaller, Storage, require_permission
from schemas import (
    AuthorOut,
    BatchBooksRequest,
    BatchBooksResponse,
    BookAdminDetail,
    BookCreate,
    BookDetail,
    BookListItem,
    BookUpdate,
    BookVersionCreate,
    BookVersionOut,
    CataloguePage,
    CategoryOut,
    EntitlementGrantIn,
    EntitlementOut,
    InternalPage,
    InternalPublishRequest,
    OwnedBooksRequest,
    OwnedBooksResponse,
    UploadRequest,
    UploadTargetOut,
)
from settings import settings

logger = get_logger(__name__)


def _offset_cursor(params: PageParams, total: int) -> str | None:
    """Present offset paging as a cursor, so every ``/internal`` listing walks the
    same way regardless of how the underlying service pages.

    Taxonomies are small and near-static, so an offset is fine for them — but the
    reconciler should not have to know which endpoints page which way.
    """
    consumed = params.page * params.limit
    if consumed >= total:
        return None
    return encode_cursor({"page": params.page + 1})


WRITE = Depends(require_permission("books:write"))
PUBLISH = Depends(require_permission("books:publish"))
DELETE = Depends(require_permission("books:delete"))

router = APIRouter(prefix="/v1/admin/books", tags=["admin"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


async def _publish_event(ctx: Ctx, event_type: str, book: object) -> None:
    """Announce a catalogue change so search reindexes and the gateway purges."""
    if ctx.publisher is None:
        return
    await ctx.publisher.publish(
        event_type,
        {"book_id": str(book.id), "slug": book.slug, "status": book.status},  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Admin catalogue
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=CataloguePage,
    summary="List books including drafts",
    description="Unlike the public feed, this returns every status. Requires `books:write`.",
    dependencies=[WRITE],
)
async def admin_list_books(
    session: DbSession,
    catalogue: Catalogue,
    filters: CatalogueFilters,
    limit: CursorLimit,
    cursor: Annotated[str | None, Query()] = None,
    book_status: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> CataloguePage:
    filters.status = book_status
    rows, next_cursor, has_more = await catalogue.list_books(
        session,
        filters=filters,
        sort_by=sort_by,
        sort_order=sort_order,
        cursor=cursor,
        limit=limit,
        public=False,
    )
    return CataloguePage(
        items=[BookListItem.model_validate(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/{book_id}",
    response_model=BookAdminDetail,
    summary="Get a book including draft fields",
    dependencies=[WRITE],
)
async def admin_get_book(
    book_id: uuid.UUID, session: DbSession, catalogue: Catalogue
) -> BookAdminDetail:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    return BookAdminDetail.model_validate(book)


@router.post(
    "",
    response_model=BookDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a book",
    dependencies=[WRITE],
)
async def create_book(
    payload: BookCreate,
    session: DbSession,
    catalogue: Catalogue,
    ctx: Ctx,
    user: CurrentUser,
) -> BookDetail:
    book = await catalogue.create(session, payload, actor_id=uuid.UUID(user.user_id))
    await _publish_event(ctx, EventType.BOOK_CREATED, book)
    return BookDetail.model_validate(book)


@router.patch(
    "/{book_id}",
    response_model=BookDetail,
    summary="Update a book",
    dependencies=[WRITE],
)
async def update_book(
    book_id: uuid.UUID,
    payload: BookUpdate,
    session: DbSession,
    catalogue: Catalogue,
    ctx: Ctx,
    user: CurrentUser,
) -> BookDetail:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    updated = await catalogue.update(session, book, payload, actor_id=uuid.UUID(user.user_id))
    await _publish_event(ctx, EventType.BOOK_UPDATED, updated)
    return BookDetail.model_validate(updated)


@router.post(
    "/{book_id}/publish",
    response_model=BookDetail,
    summary="Publish a book",
    description="Makes it visible in the public catalogue and triggers search indexing.",
    dependencies=[PUBLISH],
)
async def publish_book(
    book_id: uuid.UUID,
    session: DbSession,
    catalogue: Catalogue,
    ctx: Ctx,
    user: CurrentUser,
) -> BookDetail:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    published = await catalogue.publish(session, book, actor_id=uuid.UUID(user.user_id))
    await _publish_event(ctx, EventType.BOOK_PUBLISHED, published)
    return BookDetail.model_validate(published)


@router.post(
    "/{book_id}/unpublish",
    response_model=BookDetail,
    summary="Unpublish a book",
    description="Removes it from the catalogue. Existing entitlements are unaffected — someone who bought it keeps it.",
    dependencies=[PUBLISH],
)
async def unpublish_book(
    book_id: uuid.UUID,
    session: DbSession,
    catalogue: Catalogue,
    ctx: Ctx,
    user: CurrentUser,
) -> BookDetail:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    updated = await catalogue.unpublish(session, book, actor_id=uuid.UUID(user.user_id))
    await _publish_event(ctx, EventType.BOOK_UNPUBLISHED, updated)
    return BookDetail.model_validate(updated)


@router.delete(
    "/{book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a book (soft)",
    description="Soft delete: purchase history and entitlements must survive.",
    dependencies=[DELETE],
)
async def delete_book(
    book_id: uuid.UUID,
    session: DbSession,
    catalogue: Catalogue,
    ctx: Ctx,
    user: CurrentUser,
) -> Response:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    await catalogue.soft_delete(session, book, actor_id=uuid.UUID(user.user_id))
    await _publish_event(ctx, EventType.BOOK_DELETED, book)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{book_id}/versions",
    response_model=BookVersionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a new file version",
    description="Keeps the previous edition's keys, so a re-upload never destroys what buyers already have.",
    dependencies=[WRITE],
)
async def add_version(
    book_id: uuid.UUID,
    payload: BookVersionCreate,
    session: DbSession,
    catalogue: Catalogue,
    user: CurrentUser,
) -> BookVersionOut:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    version = await catalogue.add_version(session, book, payload, actor_id=uuid.UUID(user.user_id))
    return BookVersionOut.model_validate(version)


@router.post(
    "/uploads",
    response_model=UploadTargetOut,
    summary="Get a presigned upload target",
    description=(
        "The browser PUTs straight to object storage. A presigned POST policy pins "
        "the content type and a maximum size server-side, so a URL issued for a 5MB "
        "cover cannot be used to upload a 200MB file."
    ),
    dependencies=[WRITE],
)
async def create_upload_target(
    payload: UploadRequest,
    storage: Storage,
    user: CurrentUser,
) -> UploadTargetOut:
    # str(), not .value: BaseSchema sets use_enum_values=True, so this field is
    # already a plain string by the time the handler runs.
    category = str(payload.category)
    max_bytes = (
        settings.max_book_upload_bytes if category == "book" else settings.max_cover_upload_bytes
    )
    target = await storage.create_upload_target(
        category=category,
        owner_id=user.user_id,
        filename=payload.filename,
        content_type=payload.content_type,
        visibility="private" if category == "book" else "public",
        max_bytes=max_bytes,
    )
    return UploadTargetOut(
        url=target.url,
        key=target.key,
        fields=target.fields,
        expires_in=target.expires_in,
        max_bytes=target.max_bytes,
    )


# ---------------------------------------------------------------------------
# Entitlement administration
# ---------------------------------------------------------------------------

entitlements_router = APIRouter(prefix="/v1/admin/entitlements", tags=["admin"])


@entitlements_router.post(
    "",
    response_model=EntitlementOut,
    status_code=status.HTTP_201_CREATED,
    summary="Grant access manually",
    description="For support cases and comped copies. Audited via the `source` field.",
    dependencies=[WRITE],
)
async def grant_entitlement(
    payload: EntitlementGrantIn,
    session: DbSession,
    entitlements: Entitlements,
) -> EntitlementOut:
    # grant() is idempotent and reports whether it created a row; a repeated manual
    # grant is a no-op rather than a duplicate.
    entitlement, _created = await entitlements.grant(
        session,
        user_id=payload.user_id,
        book_id=payload.book_id,
        source=payload.source,
        external_ref=payload.external_ref,
        expires_at=payload.expires_at,
        can_download=payload.can_download,
    )
    return EntitlementOut.model_validate(entitlement)


@entitlements_router.delete(
    "/{entitlement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke access",
    dependencies=[DELETE],
)
async def revoke_entitlement(
    entitlement_id: uuid.UUID,
    session: DbSession,
    entitlements: Entitlements,
) -> Response:
    await entitlements.revoke(session, await entitlements.get(session, entitlement_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Internal (HMAC-signed callers only)
# ---------------------------------------------------------------------------


@internal_router.post(
    "/books/batch",
    response_model=BatchBooksResponse,
    summary="Fetch many books by id (internal)",
    description="Batch lookup so a sibling service rendering a list makes one call, not one per row.",
)
async def internal_batch_books(
    payload: BatchBooksRequest,
    caller: InternalCaller,
    session: DbSession,
    catalogue: Catalogue,
) -> BatchBooksResponse:
    rows = await catalogue.batch(
        session, payload.book_ids, include_unpublished=payload.include_unpublished
    )
    found = {row.id for row in rows}
    # Report what was not found rather than silently returning a short list — the
    # caller is another service reconciling state and needs to know.
    return BatchBooksResponse(
        items=[BookDetail.model_validate(row) for row in rows],
        missing=[book_id for book_id in payload.book_ids if book_id not in found],
    )


@internal_router.post(
    "/entitlements/check",
    response_model=OwnedBooksResponse,
    summary="Which of these books does a user already own? (internal)",
    description=(
        "Called by the payment service while pricing a cart, so a customer is not "
        "charged a second time for a file they already hold."
    ),
)
async def internal_entitlements_check(
    payload: OwnedBooksRequest,
    caller: InternalCaller,
    session: DbSession,
    entitlements: Entitlements,
) -> OwnedBooksResponse:
    owned = await entitlements.owned_subset(
        session, user_id=payload.user_id, book_ids=payload.book_ids
    )
    return OwnedBooksResponse(user_id=payload.user_id, owned_book_ids=sorted(owned, key=str))


@internal_router.post(
    "/books/{book_id}/publish",
    response_model=MessageResponse,
    summary="Publish a book (internal)",
    description="Called by the automation pipeline when a book finishes processing.",
)
async def internal_publish(
    book_id: uuid.UUID,
    payload: InternalPublishRequest,
    caller: InternalCaller,
    session: DbSession,
    catalogue: Catalogue,
    ctx: Ctx,
) -> MessageResponse:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    published = await catalogue.publish(session, book, actor_id=None)
    await _publish_event(ctx, EventType.BOOK_PUBLISHED, published)
    logger.info("book.published_by_automation", book_id=str(book_id), caller=caller)
    return MessageResponse(message="Book published.")


@internal_router.get(
    "/books/{book_id}",
    response_model=BookDetail,
    summary="Fetch one book (internal)",
)
async def internal_get_book(
    book_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    catalogue: Catalogue,
) -> BookDetail:
    book = await catalogue.get_by_id(session, book_id, include_unpublished=True)
    return BookDetail.model_validate(book)


@internal_router.get(
    "/books",
    response_model=InternalPage,
    summary="Page through published books (internal)",
    description=(
        "Used by the search service to reconcile its index against the source of "
        "truth. Follow `next_cursor` until it is null — a caller that stops after "
        "the first page silently indexes only the newest few hundred books.\n\n"
        "Returns the full `BookDetail` shape, because the reconciler builds index "
        "documents from it and a card-sized projection is missing the description, "
        "tags and formats it needs."
    ),
)
async def internal_list_books(
    caller: InternalCaller,
    session: DbSession,
    catalogue: Catalogue,
    filters: CatalogueFilters,
    limit: CursorLimit,
    cursor: Annotated[str | None, Query()] = None,
) -> InternalPage:
    rows, next_cursor, _more = await catalogue.list_books(
        session, filters=filters, cursor=cursor, limit=limit, public=True
    )
    return InternalPage(
        items=[BookDetail.model_validate(row).model_dump(mode="json") for row in rows],
        next_cursor=next_cursor,
    )


@internal_router.get(
    "/authors",
    response_model=InternalPage,
    summary="Page through authors (internal)",
    description="Feeds the search service's author index.",
)
async def internal_list_authors(
    caller: InternalCaller,
    session: DbSession,
    taxonomy: Taxonomy,
    params: Annotated[PageParams, Depends(page_params)],
) -> InternalPage:
    rows, total = await taxonomy.list_authors(session, params=params)
    return InternalPage(
        items=[AuthorOut.model_validate(row).model_dump(mode="json") for row in rows],
        next_cursor=_offset_cursor(params, total),
    )


@internal_router.get(
    "/categories",
    response_model=InternalPage,
    summary="Page through categories (internal)",
    description="Feeds the search service's category index.",
)
async def internal_list_categories(
    caller: InternalCaller,
    session: DbSession,
    taxonomy: Taxonomy,
    params: Annotated[PageParams, Depends(page_params)],
) -> InternalPage:
    rows, total = await taxonomy.list_categories(session, params=params)
    return InternalPage(
        items=[CategoryOut.model_validate(row).model_dump(mode="json") for row in rows],
        next_cursor=_offset_cursor(params, total),
    )

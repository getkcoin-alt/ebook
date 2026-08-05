"""Reader-owned data: reviews, bookmarks, reading progress, wishlist, collections.

Every route here writes on behalf of the signed-in user. The user id comes from the
verified token via ``user_uuid`` and never from a request field — a body-supplied
owner is how one account edits another's shelf.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from deps import (
    Catalogue,
    CurrentUser,
    CursorLimit,
    DbSession,
    Entitlements,
    Lists,
    OptionalUser,
    Reading,
    Reviews,
    user_uuid,
)
from knowledgeos_core import ListResponse, MessageResponse, NotFoundError
from knowledgeos_core.deps import StaffUser, rate_limit
from schemas import (
    BookListItem,
    BookmarkCreate,
    BookmarkOut,
    BookmarkUpdate,
    CollectionCreate,
    CollectionDetail,
    CollectionItemAdd,
    CollectionOut,
    CollectionUpdate,
    ProgressOut,
    ProgressUpdate,
    ReviewCreate,
    ReviewModerate,
    ReviewOut,
    ReviewUpdate,
    ReviewVoteIn,
    WishlistAdd,
    WishlistItemOut,
)

# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------

reviews_router = APIRouter(prefix="/v1", tags=["reviews"])


@reviews_router.get(
    "/books/{book_id}/reviews",
    response_model=ListResponse[ReviewOut],
    summary="List reviews for a book",
    description=(
        "Approved reviews, plus the viewer's own whatever its moderation status — "
        "a user who has just written a review must be able to see it."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def list_reviews(
    book_id: uuid.UUID,
    session: DbSession,
    reviews: Reviews,
    limit: CursorLimit,
    user: OptionalUser,
    cursor: Annotated[str | None, Query()] = None,
    sort_by: Annotated[str, Query(description="created_at | helpful | rating")] = "created_at",
) -> ListResponse[ReviewOut]:
    rows, _cursor, _more = await reviews.list_for_book(
        session,
        book_id=book_id,
        cursor=cursor,
        limit=limit,
        sort_by=sort_by,
        viewer_id=uuid.UUID(user.user_id) if user else None,
    )
    items = [ReviewOut.model_validate(row) for row in rows]
    return ListResponse[ReviewOut](items=items, total=len(items))


@reviews_router.post(
    "/books/{book_id}/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="Write a review",
    description=(
        "One review per user per book, enforced by a unique constraint. Reviews from "
        "users who own the book are flagged `verified_purchase`."
    ),
    dependencies=[Depends(rate_limit("authenticated"))],
)
async def create_review(
    book_id: uuid.UUID,
    payload: ReviewCreate,
    session: DbSession,
    reviews: Reviews,
    catalogue: Catalogue,
    entitlements: Entitlements,
    user: CurrentUser,
) -> ReviewOut:
    uid = user_uuid(user)
    book = await catalogue.get_by_id(session, book_id)
    access = await entitlements.access(session, user_id=uid, book=book)
    review = await reviews.create(
        session,
        book=book,
        user_id=uid,
        payload=payload,
        verified_purchase=access.has_access,
    )
    return ReviewOut.model_validate(review)


@reviews_router.patch(
    "/reviews/{review_id}",
    response_model=ReviewOut,
    summary="Edit your own review",
)
async def update_review(
    review_id: uuid.UUID,
    payload: ReviewUpdate,
    session: DbSession,
    reviews: Reviews,
    catalogue: Catalogue,
    user: CurrentUser,
) -> ReviewOut:
    review = await reviews.get_own(session, review_id=review_id, user_id=user_uuid(user))
    book = await catalogue.get_by_id(session, review.book_id, include_unpublished=True)
    return ReviewOut.model_validate(
        await reviews.update(session, review=review, book=book, payload=payload)
    )


@reviews_router.delete(
    "/reviews/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete your own review",
)
async def delete_review(
    review_id: uuid.UUID,
    session: DbSession,
    reviews: Reviews,
    catalogue: Catalogue,
    user: CurrentUser,
) -> Response:
    review = await reviews.get_own(session, review_id=review_id, user_id=user_uuid(user))
    book = await catalogue.get_by_id(session, review.book_id, include_unpublished=True)
    await reviews.delete(session, review=review, book=book)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@reviews_router.post(
    "/reviews/{review_id}/vote",
    response_model=ReviewOut,
    summary="Mark a review helpful or unhelpful",
    description="One vote per user per review. Changing a vote moves the counter by two.",
    dependencies=[Depends(rate_limit("authenticated"))],
)
async def vote_review(
    review_id: uuid.UUID,
    payload: ReviewVoteIn,
    session: DbSession,
    reviews: Reviews,
    user: CurrentUser,
) -> ReviewOut:
    review = await reviews.get(session, review_id)
    updated = await reviews.vote(
        session, review=review, user_id=user_uuid(user), is_helpful=payload.is_helpful
    )
    return ReviewOut.model_validate(updated)


@reviews_router.post(
    "/reviews/{review_id}/moderate",
    response_model=ReviewOut,
    summary="Approve or reject a review (staff)",
)
async def moderate_review(
    review_id: uuid.UUID,
    payload: ReviewModerate,
    session: DbSession,
    reviews: Reviews,
    catalogue: Catalogue,
    staff: StaffUser,
) -> ReviewOut:
    review = await reviews.get(session, review_id)
    book = await catalogue.get_by_id(session, review.book_id, include_unpublished=True)
    updated = await reviews.moderate(
        session, review=review, book=book, payload=payload, actor_id=uuid.UUID(staff.user_id)
    )
    return ReviewOut.model_validate(updated)


# ---------------------------------------------------------------------------
# Bookmarks and reading progress
# ---------------------------------------------------------------------------

reading_router = APIRouter(prefix="/v1", tags=["reading"])


@reading_router.get(
    "/bookmarks",
    response_model=ListResponse[BookmarkOut],
    summary="List your bookmarks",
)
async def list_bookmarks(
    session: DbSession,
    reading: Reading,
    user: CurrentUser,
    limit: CursorLimit,
    book_id: Annotated[uuid.UUID | None, Query(description="Restrict to one book.")] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> ListResponse[BookmarkOut]:
    rows, _cursor, _more = await reading.list_bookmarks(
        session, user_id=user_uuid(user), book_id=book_id, cursor=cursor, limit=limit
    )
    items = [BookmarkOut.model_validate(row) for row in rows]
    return ListResponse[BookmarkOut](items=items, total=len(items))


@reading_router.post(
    "/bookmarks",
    response_model=BookmarkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a bookmark",
)
async def create_bookmark(
    payload: BookmarkCreate,
    session: DbSession,
    reading: Reading,
    catalogue: Catalogue,
    entitlements: Entitlements,
    user: CurrentUser,
) -> BookmarkOut:
    uid = user_uuid(user)
    book = await catalogue.get_by_id(session, payload.book_id)
    # Bookmarking implies reading, so the same entitlement gate applies.
    await entitlements.require_read(session, user_id=uid, book=book)
    bookmark = await reading.create_bookmark(session, user_id=uid, payload=payload)
    return BookmarkOut.model_validate(bookmark)


@reading_router.patch(
    "/bookmarks/{bookmark_id}",
    response_model=BookmarkOut,
    summary="Edit a bookmark",
)
async def update_bookmark(
    bookmark_id: uuid.UUID,
    payload: BookmarkUpdate,
    session: DbSession,
    reading: Reading,
    user: CurrentUser,
) -> BookmarkOut:
    bookmark = await reading.get_own_bookmark(
        session, bookmark_id=bookmark_id, user_id=user_uuid(user)
    )
    return BookmarkOut.model_validate(
        await reading.update_bookmark(session, bookmark=bookmark, payload=payload)
    )


@reading_router.delete(
    "/bookmarks/{bookmark_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a bookmark",
)
async def delete_bookmark(
    bookmark_id: uuid.UUID,
    session: DbSession,
    reading: Reading,
    user: CurrentUser,
) -> Response:
    bookmark = await reading.get_own_bookmark(
        session, bookmark_id=bookmark_id, user_id=user_uuid(user)
    )
    await reading.delete_bookmark(session, bookmark=bookmark)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@reading_router.get(
    "/reading-progress",
    response_model=ListResponse[ProgressOut],
    summary="Your reading progress across books",
)
async def list_progress(
    session: DbSession,
    reading: Reading,
    user: CurrentUser,
    limit: CursorLimit,
    cursor: Annotated[str | None, Query()] = None,
) -> ListResponse[ProgressOut]:
    rows, _cursor, _more = await reading.list_progress(
        session, user_id=user_uuid(user), cursor=cursor, limit=limit
    )
    items = [ProgressOut.model_validate(row) for row in rows]
    return ListResponse[ProgressOut](items=items, total=len(items))


@reading_router.get(
    "/reading-progress/{book_id}",
    response_model=ProgressOut,
    summary="Your progress in one book",
)
async def get_progress(
    book_id: uuid.UUID,
    session: DbSession,
    reading: Reading,
    user: CurrentUser,
) -> ProgressOut:
    progress = await reading.get_progress(session, user_id=user_uuid(user), book_id=book_id)
    if progress is None:
        raise NotFoundError("No reading progress recorded for this book yet.")
    return ProgressOut.model_validate(progress)


@reading_router.put(
    "/reading-progress/{book_id}",
    response_model=ProgressOut,
    summary="Sync reading position",
    description=(
        "Upserts one row per (user, book). `session_seconds` is *accumulated*, never "
        "assigned — a replayed request must not be able to rewrite total reading time."
    ),
)
async def sync_progress(
    book_id: uuid.UUID,
    payload: ProgressUpdate,
    session: DbSession,
    reading: Reading,
    catalogue: Catalogue,
    entitlements: Entitlements,
    user: CurrentUser,
) -> ProgressOut:
    uid = user_uuid(user)
    book = await catalogue.get_by_id(session, book_id)
    await entitlements.require_read(session, user_id=uid, book=book)
    progress = await reading.sync_progress(session, user_id=uid, book_id=book_id, payload=payload)
    return ProgressOut.model_validate(progress)


# ---------------------------------------------------------------------------
# Wishlist and collections
# ---------------------------------------------------------------------------

lists_router = APIRouter(prefix="/v1", tags=["lists"])


@lists_router.get(
    "/wishlist",
    response_model=ListResponse[WishlistItemOut],
    summary="Your wishlist",
)
async def list_wishlist(
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
    limit: CursorLimit,
    cursor: Annotated[str | None, Query()] = None,
) -> ListResponse[WishlistItemOut]:
    rows, _cursor, _more = await lists.list_wishlist(
        session, user_id=user_uuid(user), cursor=cursor, limit=limit
    )
    # `user_id`, `book_id` and `note` are all required or meaningful on the schema
    # and were previously omitted, so serialising a single row raised a validation
    # error. An empty wishlist never entered this comprehension, which is why the
    # route looked healthy right up until somebody added something to it.
    items = [
        WishlistItemOut(
            user_id=entry.user_id,
            book_id=entry.book_id,
            note=entry.note,
            created_at=entry.created_at,
            book=BookListItem.model_validate(book),
        )
        for entry, book in rows
    ]
    return ListResponse[WishlistItemOut](items=items, total=len(items))


@lists_router.post(
    "/wishlist",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a book to your wishlist",
)
async def add_to_wishlist(
    payload: WishlistAdd,
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
) -> MessageResponse:
    await lists.add_to_wishlist(session, user_id=user_uuid(user), payload=payload)
    return MessageResponse(message="Added to your wishlist.")


@lists_router.delete(
    "/wishlist/{book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a book from your wishlist",
)
async def remove_from_wishlist(
    book_id: uuid.UUID,
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
) -> Response:
    await lists.remove_from_wishlist(session, user_id=user_uuid(user), book_id=book_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@lists_router.get(
    "/collections",
    response_model=ListResponse[CollectionOut],
    summary="List collections",
    description="Your own collections, or public ones when `public_only` is set.",
)
async def list_collections(
    session: DbSession,
    lists: Lists,
    user: OptionalUser,
    limit: CursorLimit,
    public_only: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query()] = None,
) -> ListResponse[CollectionOut]:
    owner = uuid.UUID(user.user_id) if user and not public_only else None
    rows, _cursor, _more = await lists.list_collections(
        session, user_id=owner, public_only=public_only or owner is None, cursor=cursor, limit=limit
    )
    items = [CollectionOut.model_validate(row) for row in rows]
    return ListResponse[CollectionOut](items=items, total=len(items))


@lists_router.post(
    "/collections",
    response_model=CollectionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a collection",
)
async def create_collection(
    payload: CollectionCreate,
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
) -> CollectionOut:
    collection = await lists.create_collection(session, user_id=user_uuid(user), payload=payload)
    return CollectionOut.model_validate(collection)


@lists_router.get(
    "/collections/{collection_id}",
    response_model=CollectionDetail,
    summary="Get a collection and its books",
)
async def get_collection(
    collection_id: uuid.UUID,
    session: DbSession,
    lists: Lists,
    user: OptionalUser,
) -> CollectionDetail:
    collection = await lists.get_collection(
        session,
        collection_id=collection_id,
        viewer_id=uuid.UUID(user.user_id) if user else None,
    )
    books = await lists.collection_books(session, collection=collection)
    detail = CollectionDetail.model_validate(collection)
    detail.books = [BookListItem.model_validate(book) for book in books]
    return detail


@lists_router.patch(
    "/collections/{collection_id}",
    response_model=CollectionOut,
    summary="Edit your collection",
)
async def update_collection(
    collection_id: uuid.UUID,
    payload: CollectionUpdate,
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
) -> CollectionOut:
    collection = await lists.get_own_collection(
        session, collection_id=collection_id, user_id=user_uuid(user)
    )
    return CollectionOut.model_validate(
        await lists.update_collection(session, collection=collection, payload=payload)
    )


@lists_router.delete(
    "/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete your collection",
)
async def delete_collection(
    collection_id: uuid.UUID,
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
) -> Response:
    collection = await lists.get_own_collection(
        session, collection_id=collection_id, user_id=user_uuid(user)
    )
    await lists.delete_collection(session, collection=collection)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@lists_router.post(
    "/collections/{collection_id}/items",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a book to your collection",
)
async def add_collection_item(
    collection_id: uuid.UUID,
    payload: CollectionItemAdd,
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
) -> MessageResponse:
    collection = await lists.get_own_collection(
        session, collection_id=collection_id, user_id=user_uuid(user)
    )
    await lists.add_collection_item(session, collection=collection, payload=payload)
    return MessageResponse(message="Added to the collection.")


@lists_router.delete(
    "/collections/{collection_id}/items/{book_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a book from your collection",
)
async def remove_collection_item(
    collection_id: uuid.UUID,
    book_id: uuid.UUID,
    session: DbSession,
    lists: Lists,
    user: CurrentUser,
) -> Response:
    collection = await lists.get_own_collection(
        session, collection_id=collection_id, user_id=user_uuid(user)
    )
    await lists.remove_collection_item(session, collection=collection, book_id=book_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

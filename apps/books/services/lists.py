"""Wishlist and collections: the reader's own curation.

A wishlist item is a composite-key row — one per (user, book) — so "add" is
naturally idempotent: tapping the heart twice on a flaky connection must not create
a second entry or fail.

Collections carry a denormalised ``item_count`` because the shelf grid renders the
count next to every collection; counting rows per collection on that screen is N+1
queries for a number that changes only when someone adds a book.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from knowledgeos_core import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    decode_cursor,
    encode_cursor,
    get_logger,
)
from models import Book, BookAuthor, BookCategory, Collection, CollectionItem, WishlistItem
from schemas import CollectionCreate, CollectionItemAdd, CollectionUpdate, WishlistAdd
from settings import Settings

logger = get_logger(__name__)


def _book_options() -> tuple:
    return (
        selectinload(Book.author_links).selectinload(BookAuthor.author),
        selectinload(Book.category_links).selectinload(BookCategory.category),
    )


class ListService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- wishlist -------------------------------------------------------

    async def add_to_wishlist(
        self, session: AsyncSession, *, user_id: uuid.UUID, payload: WishlistAdd
    ) -> WishlistItem:
        existing = await session.get(WishlistItem, {"user_id": user_id, "book_id": payload.book_id})
        if existing is not None:
            existing.note = payload.note or existing.note
            await session.flush()
            return existing
        item = WishlistItem(user_id=user_id, book_id=payload.book_id, note=payload.note)
        session.add(item)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            found = await session.get(
                WishlistItem, {"user_id": user_id, "book_id": payload.book_id}
            )
            if found is None:  # pragma: no cover - defensive
                raise
            return found
        return item

    async def list_wishlist(
        self, session: AsyncSession, *, user_id: uuid.UUID, cursor: str | None, limit: int
    ) -> tuple[list[tuple[WishlistItem, Book]], str | None, bool]:
        stmt = (
            select(WishlistItem, Book)
            .join(Book, Book.id == WishlistItem.book_id)
            .where(WishlistItem.user_id == user_id, Book.deleted_at.is_(None))
            .options(*_book_options())
            .order_by(WishlistItem.created_at.desc(), WishlistItem.book_id.desc())
            .limit(limit + 1)
        )
        if cursor:
            created_at, last_id = _decode_cursor(cursor)
            stmt = stmt.where(
                or_(
                    WishlistItem.created_at < created_at,
                    and_(
                        WishlistItem.created_at == created_at,
                        WishlistItem.book_id < last_id,
                    ),
                )
            )
        rows = [(row[0], row[1]) for row in (await session.execute(stmt)).unique().all()]
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1][0]
            next_cursor = encode_cursor({"v": last.created_at.isoformat(), "id": str(last.book_id)})
        return rows, next_cursor, has_more

    async def remove_from_wishlist(
        self, session: AsyncSession, *, user_id: uuid.UUID, book_id: uuid.UUID
    ) -> None:
        item = await session.get(WishlistItem, {"user_id": user_id, "book_id": book_id})
        if item is None:
            raise NotFoundError("That book is not in your wishlist.")
        await session.delete(item)
        await session.flush()

    # ---- collections ----------------------------------------------------

    async def create_collection(
        self, session: AsyncSession, *, user_id: uuid.UUID, payload: CollectionCreate
    ) -> Collection:
        collection = Collection(user_id=user_id, **payload.model_dump())
        session.add(collection)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "You already have a collection with that slug.",
                details={"slug": payload.slug},
            ) from exc
        return collection

    async def get_collection(
        self,
        session: AsyncSession,
        collection_id: uuid.UUID,
        *,
        viewer_id: uuid.UUID | None,
    ) -> Collection:
        collection = await session.get(Collection, collection_id)
        if collection is None or collection.deleted_at is not None:
            raise NotFoundError(
                "Collection not found.", details={"collection_id": str(collection_id)}
            )
        if not collection.is_public and collection.user_id != viewer_id:
            # A private collection must be indistinguishable from a missing one, or
            # the endpoint becomes a probe for which ids exist.
            raise NotFoundError(
                "Collection not found.", details={"collection_id": str(collection_id)}
            )
        return collection

    async def get_own_collection(
        self, session: AsyncSession, collection_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> Collection:
        collection = await session.get(Collection, collection_id)
        if collection is None or collection.deleted_at is not None:
            raise NotFoundError(
                "Collection not found.", details={"collection_id": str(collection_id)}
            )
        if collection.user_id != user_id:
            raise ForbiddenError("You can only modify your own collections.")
        return collection

    async def list_collections(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | None,
        public_only: bool,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Collection], str | None, bool]:
        stmt = select(Collection).where(Collection.deleted_at.is_(None))
        if public_only:
            stmt = stmt.where(Collection.is_public.is_(True))
        if user_id is not None:
            stmt = stmt.where(Collection.user_id == user_id)
        if cursor:
            created_at, last_id = _decode_cursor(cursor)
            stmt = stmt.where(
                or_(
                    Collection.created_at < created_at,
                    and_(Collection.created_at == created_at, Collection.id < last_id),
                )
            )
        stmt = stmt.order_by(Collection.created_at.desc(), Collection.id.desc()).limit(limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = encode_cursor({"v": last.created_at.isoformat(), "id": str(last.id)})
        return rows, next_cursor, has_more

    async def update_collection(
        self, session: AsyncSession, *, collection: Collection, payload: CollectionUpdate
    ) -> Collection:
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(collection, field, value)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            raise ConflictError("You already have a collection with that slug.") from exc
        return collection

    async def delete_collection(self, session: AsyncSession, *, collection: Collection) -> None:
        collection.deleted_at = datetime.now(UTC)
        await session.flush()

    async def add_collection_item(
        self, session: AsyncSession, *, collection: Collection, payload: CollectionItemAdd
    ) -> CollectionItem:
        existing = await session.get(
            CollectionItem, {"collection_id": collection.id, "book_id": payload.book_id}
        )
        if existing is not None:
            existing.display_order = payload.display_order
            existing.note = payload.note
            await session.flush()
            return existing
        item = CollectionItem(
            collection_id=collection.id,
            book_id=payload.book_id,
            display_order=payload.display_order,
            note=payload.note,
        )
        session.add(item)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            found = await session.get(
                CollectionItem, {"collection_id": collection.id, "book_id": payload.book_id}
            )
            if found is None:  # pragma: no cover - defensive
                raise
            return found
        await self._refresh_count(session, collection)
        return item

    async def remove_collection_item(
        self, session: AsyncSession, *, collection: Collection, book_id: uuid.UUID
    ) -> None:
        item = await session.get(
            CollectionItem, {"collection_id": collection.id, "book_id": book_id}
        )
        if item is None:
            raise NotFoundError("That book is not in this collection.")
        await session.delete(item)
        await session.flush()
        await self._refresh_count(session, collection)

    async def collection_books(
        self, session: AsyncSession, *, collection: Collection
    ) -> list[Book]:
        stmt = (
            select(Book)
            .join(CollectionItem, CollectionItem.book_id == Book.id)
            .where(
                CollectionItem.collection_id == collection.id,
                Book.deleted_at.is_(None),
            )
            .options(*_book_options())
            .order_by(CollectionItem.display_order, Book.title)
        )
        return list((await session.execute(stmt)).scalars().unique().all())

    async def _refresh_count(self, session: AsyncSession, collection: Collection) -> None:
        """Recount rather than increment.

        The count is derived data; recomputing it from the source of truth after a
        mutation costs one indexed COUNT and cannot drift, whereas ``+= 1`` drifts
        permanently the first time a request fails between the two writes.
        """
        collection.item_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CollectionItem)
                    .where(CollectionItem.collection_id == collection.id)
                )
            ).scalar_one()
        )
        await session.flush()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    data = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(str(data["v"])), uuid.UUID(str(data["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BadRequestError("The pagination cursor is malformed.") from exc

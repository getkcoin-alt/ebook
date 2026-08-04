"""Bookmarks and reading progress — the reader's own state.

Both are per-user data on someone else's content, so every query filters by the
``user_id`` taken from the access token. There is no endpoint that accepts a user id
in the body: that is how one reader ends up writing to another's shelf.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    decode_cursor,
    encode_cursor,
    get_logger,
)
from models import Bookmark, ReadingProgress
from schemas import BookmarkCreate, BookmarkUpdate, ProgressUpdate
from settings import Settings

logger = get_logger(__name__)

#: A book is considered finished at this percentage. Readers rarely land exactly on
#: 100 — the last page of an EPUB is often a colophon nobody scrolls through.
FINISHED_PERCENT = 98.0


class ReadingService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- bookmarks ------------------------------------------------------

    async def create_bookmark(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        book_id: uuid.UUID,
        payload: BookmarkCreate,
    ) -> Bookmark:
        bookmark = Bookmark(user_id=user_id, book_id=book_id, **payload.model_dump())
        session.add(bookmark)
        await session.flush()
        return bookmark

    async def get_own_bookmark(
        self, session: AsyncSession, bookmark_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> Bookmark:
        bookmark = await session.get(Bookmark, bookmark_id)
        if bookmark is None:
            raise NotFoundError("Bookmark not found.", details={"bookmark_id": str(bookmark_id)})
        if bookmark.user_id != user_id:
            raise ForbiddenError("You can only modify your own bookmarks.")
        return bookmark

    async def list_bookmarks(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        book_id: uuid.UUID | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Bookmark], str | None, bool]:
        stmt = select(Bookmark).where(Bookmark.user_id == user_id)
        if book_id is not None:
            stmt = stmt.where(Bookmark.book_id == book_id)
        if cursor:
            created_at, last_id = _decode_time_cursor(cursor)
            stmt = stmt.where(
                or_(
                    Bookmark.created_at < created_at,
                    and_(Bookmark.created_at == created_at, Bookmark.id < last_id),
                )
            )
        stmt = stmt.order_by(Bookmark.created_at.desc(), Bookmark.id.desc()).limit(limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        return _slice(rows, limit)

    async def update_bookmark(
        self, session: AsyncSession, *, bookmark: Bookmark, payload: BookmarkUpdate
    ) -> Bookmark:
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(bookmark, field, value)
        await session.flush()
        return bookmark

    async def delete_bookmark(self, session: AsyncSession, *, bookmark: Bookmark) -> None:
        await session.delete(bookmark)
        await session.flush()

    # ---- progress -------------------------------------------------------

    async def get_progress(
        self, session: AsyncSession, *, user_id: uuid.UUID, book_id: uuid.UUID
    ) -> ReadingProgress | None:
        return (
            await session.execute(
                select(ReadingProgress).where(
                    ReadingProgress.user_id == user_id, ReadingProgress.book_id == book_id
                )
            )
        ).scalars().one_or_none()

    async def sync_progress(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        book_id: uuid.UUID,
        payload: ProgressUpdate,
    ) -> ReadingProgress:
        """Upsert one row per (user, book).

        ``session_seconds`` is *accumulated*, never assigned: the client reports how
        long this session lasted, and a replayed request must not be able to rewrite
        total reading time to an arbitrary value.
        """
        now = datetime.now(UTC)
        progress = await self.get_progress(session, user_id=user_id, book_id=book_id)

        if progress is None:
            progress = ReadingProgress(
                user_id=user_id,
                book_id=book_id,
                position=payload.position,
                page_number=payload.page_number,
                percent=payload.percent,
                total_reading_seconds=payload.session_seconds,
                last_read_at=now,
            )
            session.add(progress)
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError:
                # Two devices syncing the same book at once. Re-read and fall
                # through to the update path.
                progress = await self.get_progress(session, user_id=user_id, book_id=book_id)
                if progress is None:  # pragma: no cover - defensive
                    raise
            else:
                self._mark_finished(progress, now)
                await session.flush()
                return progress

        progress.position = payload.position
        progress.page_number = payload.page_number
        # Progress only moves forward. A second device that is behind must not drag
        # the furthest-read position backwards.
        progress.percent = max(progress.percent, payload.percent)
        progress.total_reading_seconds += payload.session_seconds
        progress.last_read_at = now
        self._mark_finished(progress, now)
        await session.flush()
        return progress

    @staticmethod
    def _mark_finished(progress: ReadingProgress, now: datetime) -> None:
        if progress.percent >= FINISHED_PERCENT and not progress.is_finished:
            progress.is_finished = True
            progress.finished_at = now

    async def list_progress(
        self, session: AsyncSession, *, user_id: uuid.UUID, cursor: str | None, limit: int
    ) -> tuple[list[ReadingProgress], str | None, bool]:
        """"Continue reading": most recently opened first."""
        stmt = select(ReadingProgress).where(ReadingProgress.user_id == user_id)
        if cursor:
            last_read_at, last_id = _decode_time_cursor(cursor)
            stmt = stmt.where(
                or_(
                    ReadingProgress.last_read_at < last_read_at,
                    and_(
                        ReadingProgress.last_read_at == last_read_at,
                        ReadingProgress.id < last_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            ReadingProgress.last_read_at.desc(), ReadingProgress.id.desc()
        ).limit(limit + 1)
        rows = list((await session.execute(stmt)).scalars().all())
        return _slice(rows, limit, key="last_read_at")


def _decode_time_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    data = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(str(data["v"])), uuid.UUID(str(data["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BadRequestError("The pagination cursor is malformed.") from exc


def _slice(rows: list, limit: int, *, key: str = "created_at") -> tuple[list, str | None, bool]:
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"v": getattr(last, key).isoformat(), "id": str(last.id)})
    return rows, next_cursor, has_more

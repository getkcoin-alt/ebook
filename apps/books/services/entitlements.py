"""Entitlements: who may open which book, and why.

This is the only thing that grants access to a paid file. Not an order row, not a
session flag, not a claim in an access token — a row in ``books.entitlements``. That
single rule is what makes access auditable and revocable.

Grants are idempotent by construction. The unique constraint over
``(user_id, book_id, source, external_ref)`` means a redelivered
``payment.succeeded`` produces the same one row, not a second grant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from knowledgeos_core import (
    BadRequestError,
    BookStatus,
    ForbiddenError,
    NotFoundError,
    PaymentRequiredError,
    decode_cursor,
    encode_cursor,
    get_logger,
)
from models import Book, BookAuthor, BookCategory, Entitlement, ReadingProgress
from schemas import AccessOut, EntitlementSource
from settings import Settings

logger = get_logger(__name__)


class EntitlementService:
    """Grant, revoke and check access."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- checks ---------------------------------------------------------

    @staticmethod
    def _active_clause(now: datetime) -> Any:
        return and_(
            Entitlement.revoked_at.is_(None),
            or_(Entitlement.expires_at.is_(None), Entitlement.expires_at > now),
        )

    async def find_active(
        self, session: AsyncSession, *, user_id: uuid.UUID, book_id: uuid.UUID
    ) -> Entitlement | None:
        """The strongest live grant for this pair.

        Ordered so a grant that permits downloading wins over a read-only one — a
        user who bought a book and was also gifted a read-only copy should get the
        rights of the purchase.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Entitlement)
            .where(
                Entitlement.user_id == user_id,
                Entitlement.book_id == book_id,
                self._active_clause(now),
            )
            .order_by(Entitlement.can_download.desc(), Entitlement.granted_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    def _free_access(self, book: Book) -> AccessOut | None:
        """Free books need no row when ``free_books_readable`` is on.

        Writing an entitlement row per user per free book would add millions of rows
        that all say the same thing. The setting exists so a deployment that wants
        an explicit grant for everything can turn it off.
        """
        if not self._settings.free_books_readable or not book.is_free:
            return None
        return AccessOut(
            book_id=book.id,
            has_access=True,
            can_download=True,
            source=EntitlementSource.FREE,
            expires_at=None,
        )

    async def access(self, session: AsyncSession, *, user_id: uuid.UUID, book: Book) -> AccessOut:
        entitlement = await self.find_active(session, user_id=user_id, book_id=book.id)
        if entitlement is not None:
            return AccessOut(
                book_id=book.id,
                has_access=entitlement.can_read,
                can_download=entitlement.can_download,
                source=EntitlementSource(entitlement.source),
                expires_at=entitlement.expires_at,
            )
        free = self._free_access(book)
        if free is not None:
            return free
        return AccessOut(book_id=book.id, has_access=False, can_download=False)

    async def require_read(
        self, session: AsyncSession, *, user_id: uuid.UUID, book: Book
    ) -> AccessOut:
        """402 when the book must be bought, 403 when a grant exists but forbids this."""
        access = await self.access(session, user_id=user_id, book=book)
        if not access.has_access:
            if access.source is None:
                raise PaymentRequiredError(
                    "This book is not in your library.",
                    details={"book_id": str(book.id), "price_minor": book.effective_price_minor},
                )
            raise ForbiddenError(
                "Your access to this book does not permit reading.",
                details={"book_id": str(book.id)},
            )
        return access

    async def require_download(
        self, session: AsyncSession, *, user_id: uuid.UUID, book: Book
    ) -> AccessOut:
        access = await self.require_read(session, user_id=user_id, book=book)
        if not access.can_download:
            raise ForbiddenError(
                "Your access to this book is read-only.", details={"book_id": str(book.id)}
            )
        return access

    # ---- grants ---------------------------------------------------------

    async def grant(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        book_id: uuid.UUID,
        source: EntitlementSource | str,
        external_ref: str = "",
        order_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
        can_download: bool = True,
        note: str | None = None,
    ) -> tuple[Entitlement, bool]:
        """Idempotently grant access. Returns ``(entitlement, created)``.

        The lookup-then-insert covers the ordinary redelivery; the ``IntegrityError``
        branch covers two deliveries racing in different workers, where both see no
        row and both insert. Both paths converge on one row.
        """
        existing = (
            (
                await session.execute(
                    select(Entitlement).where(
                        Entitlement.user_id == user_id,
                        Entitlement.book_id == book_id,
                        Entitlement.source == source,
                        Entitlement.external_ref == external_ref,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

        if existing is not None:
            if existing.revoked_at is not None:
                # A re-grant after a revocation (a refund that was then reversed)
                # reinstates the same row rather than creating a duplicate.
                existing.revoked_at = None
                existing.granted_at = datetime.now(UTC)
                await session.flush()
                logger.info("entitlement.reinstated", user_id=str(user_id), book_id=str(book_id))
            return existing, False

        entitlement = Entitlement(
            user_id=user_id,
            book_id=book_id,
            source=source,
            external_ref=external_ref,
            order_id=order_id,
            expires_at=expires_at,
            can_read=True,
            can_download=can_download,
            granted_at=datetime.now(UTC),
            note=note,
        )
        session.add(entitlement)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            found = (
                (
                    await session.execute(
                        select(Entitlement).where(
                            Entitlement.user_id == user_id,
                            Entitlement.book_id == book_id,
                            Entitlement.source == source,
                            Entitlement.external_ref == external_ref,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if found is None:  # pragma: no cover - the FK, not the unique key
                raise
            logger.info("entitlement.grant_raced", user_id=str(user_id), book_id=str(book_id))
            return found, False

        logger.info(
            "entitlement.granted",
            user_id=str(user_id),
            book_id=str(book_id),
            source=str(source),
            external_ref=external_ref or None,
        )
        return entitlement, True

    async def revoke(
        self, session: AsyncSession, *, entitlement: Entitlement, reason: str | None = None
    ) -> Entitlement:
        if entitlement.revoked_at is None:
            entitlement.revoked_at = datetime.now(UTC)
            entitlement.note = reason or entitlement.note
            await session.flush()
            logger.info("entitlement.revoked", entitlement_id=str(entitlement.id))
        return entitlement

    async def get(self, session: AsyncSession, entitlement_id: uuid.UUID) -> Entitlement:
        entitlement = await session.get(Entitlement, entitlement_id)
        if entitlement is None:
            raise NotFoundError("Entitlement not found.")
        return entitlement

    # ---- library --------------------------------------------------------

    async def library(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[tuple[Entitlement, Book]], str | None, bool]:
        """The signed-in user's shelf, newest grant first.

        Keyset-paginated on ``(granted_at, id)`` like the catalogue: a long-standing
        customer's library is not a small table either.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Entitlement, Book)
            .join(Book, Book.id == Entitlement.book_id)
            .where(
                Entitlement.user_id == user_id,
                self._active_clause(now),
                Entitlement.can_read.is_(True),
                Book.deleted_at.is_(None),
            )
            .options(
                selectinload(Book.author_links).selectinload(BookAuthor.author),
                selectinload(Book.category_links).selectinload(BookCategory.category),
            )
            .order_by(Entitlement.granted_at.desc(), Entitlement.id.desc())
            .limit(limit + 1)
        )

        if cursor:
            data = decode_cursor(cursor)
            try:
                last_granted = datetime.fromisoformat(str(data["v"]))
                last_id = uuid.UUID(str(data["id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise BadRequestError("The pagination cursor is malformed.") from exc
            stmt = stmt.where(
                or_(
                    Entitlement.granted_at < last_granted,
                    and_(Entitlement.granted_at == last_granted, Entitlement.id < last_id),
                )
            )

        rows = list((await session.execute(stmt)).unique().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        pairs = [(row[0], row[1]) for row in rows]

        next_cursor = None
        if has_more and pairs:
            last = pairs[-1][0]
            next_cursor = encode_cursor({"v": last.granted_at.isoformat(), "id": str(last.id)})
        return pairs, next_cursor, has_more

    async def progress_percent_for(
        self, session: AsyncSession, *, user_id: uuid.UUID, book_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, float]:
        """Reading progress for a page of the library, in one query rather than N."""
        if not book_ids:
            return {}
        rows = (
            await session.execute(
                select(ReadingProgress.book_id, ReadingProgress.percent).where(
                    ReadingProgress.user_id == user_id,
                    ReadingProgress.book_id.in_(book_ids),
                )
            )
        ).all()
        return {book_id: float(percent) for book_id, percent in rows}

    async def load_grantable_book(self, session: AsyncSession, book_id: uuid.UUID) -> Book | None:
        """A book an event may grant access to.

        Unpublished books are allowed here on purpose: a purchase completing moments
        after an editor unpublishes a title must still be honoured. Deleted books
        are not — nothing can be sold from an archived catalogue entry.
        """
        book = await session.get(Book, book_id)
        if book is None or book.deleted_at is not None:
            return None
        if book.status == BookStatus.ARCHIVED:
            return None
        return book

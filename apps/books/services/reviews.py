"""Reviews, votes, moderation — and the rating aggregates they maintain.

The catalogue sorts and filters by rating on every request. Computing
``AVG(rating)`` across ``reviews`` for each of twenty cards on a feed page is a
guaranteed table scan, so the aggregate is denormalised onto ``books`` and updated
here, on the write path, where it costs one arithmetic operation.

``rating_sum`` is stored alongside ``rating_count`` so an update can be applied as a
delta in O(1) — without it, changing one review's rating from 4 to 5 would require
re-reading every review of that book.

Only **approved** reviews count towards the aggregate. A review moving in or out of
``approved`` therefore adjusts the totals, which is why moderation goes through the
same helper as creation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    decode_cursor,
    encode_cursor,
    get_logger,
)
from models import Book, Review, ReviewVote
from schemas import ReviewCreate, ReviewStatus, ReviewUpdate
from settings import Settings

logger = get_logger(__name__)

#: Review list orderings. ``helpful_count`` is the default the frontend uses on a
#: book page; ``created_at`` powers "most recent".
SORTABLE = {"created_at": Review.created_at, "helpful_count": Review.helpful_count}


def _recalculate(book: Book) -> None:
    book.rating_average = (
        round(book.rating_sum / book.rating_count, 3) if book.rating_count else 0.0
    )


class ReviewService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- aggregate maintenance -----------------------------------------

    @staticmethod
    def apply_rating_delta(book: Book, *, sum_delta: int, count_delta: int) -> None:
        """Move the denormalised aggregate by a delta and re-derive the average.

        Clamped at zero: an aggregate can only go negative if a bug double-counted a
        removal, and a negative rating count rendered on a product page is worse
        than a slightly stale one.
        """
        book.rating_sum = max(0, book.rating_sum + sum_delta)
        book.rating_count = max(0, book.rating_count + count_delta)
        _recalculate(book)

    # ---- reads ----------------------------------------------------------

    async def get(self, session: AsyncSession, review_id: uuid.UUID) -> Review:
        review = await session.get(Review, review_id)
        if review is None or review.deleted_at is not None:
            raise NotFoundError("Review not found.", details={"review_id": str(review_id)})
        return review

    async def get_own(
        self, session: AsyncSession, review_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> Review:
        review = await self.get(session, review_id)
        if review.user_id != user_id:
            # 403, not 404: the caller proved they are signed in and the resource
            # id is not a secret, so hiding existence buys nothing.
            raise ForbiddenError("You can only modify your own review.")
        return review

    async def for_user(
        self, session: AsyncSession, *, book_id: uuid.UUID, user_id: uuid.UUID
    ) -> Review | None:
        return (
            (
                await session.execute(
                    select(Review).where(
                        Review.book_id == book_id,
                        Review.user_id == user_id,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def list_for_book(
        self,
        session: AsyncSession,
        *,
        book_id: uuid.UUID,
        cursor: str | None,
        limit: int,
        sort_by: str = "created_at",
        viewer_id: uuid.UUID | None = None,
        include_all_statuses: bool = False,
    ) -> tuple[list[Review], str | None, bool]:
        """Approved reviews, plus the viewer's own whatever its status.

        A reviewer whose review is held for moderation must still see it, otherwise
        they submit it again and hit the one-review-per-user conflict.
        """
        if sort_by not in SORTABLE:
            raise BadRequestError(
                f"Cannot sort reviews by '{sort_by}'.", details={"allowed": sorted(SORTABLE)}
            )
        column = SORTABLE[sort_by]

        stmt = select(Review).where(Review.book_id == book_id, Review.deleted_at.is_(None))
        if not include_all_statuses:
            visible = Review.status == ReviewStatus.APPROVED
            if viewer_id is not None:
                visible = or_(visible, Review.user_id == viewer_id)
            stmt = stmt.where(visible)

        if cursor:
            data = decode_cursor(cursor)
            if data.get("s") != sort_by:
                raise BadRequestError("This cursor was issued for a different ordering.")
            try:
                raw = data["v"]
                value = datetime.fromisoformat(str(raw)) if sort_by == "created_at" else int(raw)
                last_id = uuid.UUID(str(data["id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise BadRequestError("The pagination cursor is malformed.") from exc
            stmt = stmt.where(or_(column < value, and_(column == value, Review.id < last_id)))

        stmt = stmt.order_by(column.desc(), Review.id.desc()).limit(limit + 1)
        rows = list((await session.execute(stmt)).scalars().unique().all())
        has_more = len(rows) > limit
        rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            value = getattr(last, sort_by)
            next_cursor = encode_cursor(
                {
                    "s": sort_by,
                    "v": value.isoformat() if isinstance(value, datetime) else value,
                    "id": str(last.id),
                }
            )
        return rows, next_cursor, has_more

    # ---- writes ---------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        *,
        book: Book,
        user_id: uuid.UUID,
        payload: ReviewCreate,
        verified_purchase: bool,
    ) -> Review:
        """One review per user per book.

        The database enforces it with a unique constraint; this method translates
        the two ways that can surface — an existing row we can see, and a concurrent
        insert we cannot — into the same 409.
        """
        status = (
            ReviewStatus.PENDING
            if self._settings.reviews_require_moderation
            else ReviewStatus.APPROVED
        )

        existing = await self.for_user(session, book_id=book.id, user_id=user_id)
        if existing is not None and existing.deleted_at is None:
            raise ConflictError(
                "You have already reviewed this book.",
                details={"review_id": str(existing.id)},
            )
        if existing is not None:
            # The unique constraint survives a soft delete, so a user who removed
            # their review must be able to reuse the row rather than being locked
            # out of ever reviewing the book again.
            existing.deleted_at = None
            existing.rating = payload.rating
            existing.title = payload.title
            existing.body = payload.body
            existing.status = status
            existing.verified_purchase = verified_purchase
            existing.helpful_count = 0
            await session.flush()
            if status == ReviewStatus.APPROVED:
                self.apply_rating_delta(book, sum_delta=payload.rating, count_delta=1)
            logger.info("review.recreated", review_id=str(existing.id), book_id=str(book.id))
            return existing

        review = Review(
            book_id=book.id,
            user_id=user_id,
            rating=payload.rating,
            title=payload.title,
            body=payload.body,
            status=status,
            verified_purchase=verified_purchase,
        )
        session.add(review)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            raise ConflictError("You have already reviewed this book.") from exc

        if status == ReviewStatus.APPROVED:
            self.apply_rating_delta(book, sum_delta=payload.rating, count_delta=1)
        await session.flush()
        logger.info(
            "review.created",
            review_id=str(review.id),
            book_id=str(book.id),
            rating=review.rating,
        )
        return review

    async def update(
        self, session: AsyncSession, *, review: Review, book: Book, payload: ReviewUpdate
    ) -> Review:
        data = payload.model_dump(exclude_unset=True)
        new_rating = data.get("rating")
        if new_rating is not None and new_rating != review.rating:
            if review.status == ReviewStatus.APPROVED:
                self.apply_rating_delta(book, sum_delta=new_rating - review.rating, count_delta=0)
            review.rating = new_rating
        if "title" in data:
            review.title = data["title"]
        if "body" in data:
            review.body = data["body"]
        # An edited review goes back through moderation when moderation is on;
        # otherwise a reviewer could get an innocuous review approved and then
        # rewrite it.
        if self._settings.reviews_require_moderation and review.status == ReviewStatus.APPROVED:
            self.apply_rating_delta(book, sum_delta=-review.rating, count_delta=-1)
            review.status = ReviewStatus.PENDING
        await session.flush()
        return review

    async def delete(self, session: AsyncSession, *, review: Review, book: Book) -> None:
        if review.status == ReviewStatus.APPROVED:
            self.apply_rating_delta(book, sum_delta=-review.rating, count_delta=-1)
        review.deleted_at = datetime.now(UTC)
        await session.flush()
        logger.info("review.deleted", review_id=str(review.id), book_id=str(book.id))

    async def moderate(
        self,
        session: AsyncSession,
        *,
        review: Review,
        book: Book,
        status: ReviewStatus,
        note: str | None,
        moderator_id: uuid.UUID,
    ) -> Review:
        was_counted = review.status == ReviewStatus.APPROVED
        will_count = status == ReviewStatus.APPROVED
        if was_counted and not will_count:
            self.apply_rating_delta(book, sum_delta=-review.rating, count_delta=-1)
        elif will_count and not was_counted:
            self.apply_rating_delta(book, sum_delta=review.rating, count_delta=1)

        review.status = status
        review.moderation_note = note
        review.moderated_by = moderator_id
        review.moderated_at = datetime.now(UTC)
        await session.flush()
        logger.info("review.moderated", review_id=str(review.id), status=str(status))
        return review

    async def vote(
        self, session: AsyncSession, *, review: Review, user_id: uuid.UUID, is_helpful: bool
    ) -> Review:
        """One vote per user per review; changing a vote moves the counter by two."""
        if review.user_id == user_id:
            raise ForbiddenError("You cannot vote on your own review.")

        existing = await session.get(ReviewVote, {"review_id": review.id, "user_id": user_id})
        if existing is None:
            session.add(ReviewVote(review_id=review.id, user_id=user_id, is_helpful=is_helpful))
            review.helpful_count = max(0, review.helpful_count + (1 if is_helpful else -1))
        elif existing.is_helpful != is_helpful:
            existing.is_helpful = is_helpful
            review.helpful_count = max(0, review.helpful_count + (2 if is_helpful else -2))
        await session.flush()
        return review

    async def moderation_queue(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        status: ReviewStatus | None = None,
        book_id: uuid.UUID | None = None,
    ) -> tuple[list[Review], int]:
        """Reviews awaiting a decision, across the whole catalogue.

        The per-book listing cannot serve this. A moderator does not know which book
        has something pending — that is the entire question — so a queue that requires
        a book id first is a queue nobody can work from.

        Offset paged with a real total, unlike the public review feed. The queue is a
        worklist: "37 waiting" is the number that decides whether someone starts, and
        an infinite scroll cannot show it. It is also, by design, short.

        Oldest first. A queue worked newest-first leaves its oldest items forever, and
        those are exactly the ones a customer is waiting on.
        """

        def apply(stmt):  # type: ignore[no-untyped-def]
            stmt = stmt.where(Review.deleted_at.is_(None))
            stmt = stmt.where(
                Review.status == (status if status is not None else ReviewStatus.PENDING)
            )
            if book_id is not None:
                stmt = stmt.where(Review.book_id == book_id)
            return stmt

        total = int((await session.execute(apply(select(func.count(Review.id))))).scalar_one())
        stmt = (
            apply(select(Review))
            .order_by(Review.created_at.asc(), Review.id)
            .limit(limit)
            .offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all()), total

    async def moderation_counts(self, session: AsyncSession) -> dict[str, int]:
        """How many sit in each state. Feeds the queue's tab badges."""
        rows = (
            await session.execute(
                select(Review.status, func.count(Review.id))
                .where(Review.deleted_at.is_(None))
                .group_by(Review.status)
            )
        ).all()
        counts = {str(status): int(total) for status, total in rows}
        # Every state present, so the UI does not have to special-case a missing key
        # and render a blank where a zero belongs.
        return {str(state): counts.get(str(state), 0) for state in ReviewStatus}

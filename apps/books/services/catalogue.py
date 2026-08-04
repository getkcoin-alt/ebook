"""Catalogue reads and writes: the hot path of the platform.

Three rules hold this module together.

**Cursor pagination, never OFFSET.** The public feed is keyset-paginated on
``(sort column, id)``. ``OFFSET 40000`` makes PostgreSQL walk and discard forty
thousand rows on every request; the keyset form stays O(limit) at any depth and
cannot skip or duplicate a row when a book is inserted mid-scroll.

**Eager loading, never lazy.** Every relationship on ``Book`` is
``lazy="raise_on_sql"``, so a missing ``selectinload`` raises in a test instead of
becoming an N+1 in the p99. The loader options live here, next to the queries.

**Ratings are read, never computed.** ``rating_average`` is maintained on review
writes (see :mod:`services.reviews`). The catalogue never runs ``AVG()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from knowledgeos_core import (
    BadRequestError,
    BookStatus,
    ConflictError,
    NotFoundError,
    apply_search,
    decode_cursor,
    encode_cursor,
    get_logger,
)
from models import Author, Book, BookAuthor, BookCategory, BookVersion, Category, Publisher
from schemas import (
    BookCreate,
    BookUpdate,
    BookVersionCreate,
    CatalogueFacets,
    FacetValue,
)
from services.cache import CatalogueCache
from settings import Settings

logger = get_logger(__name__)

#: Columns a caller may order the catalogue by. A strict whitelist: user input
#: selects from this set, it is never interpolated into SQL.
SORTABLE: dict[str, Any] = {
    "published_at": Book.published_at,
    "created_at": Book.created_at,
    "price_minor": Book.price_minor,
    "rating_average": Book.rating_average,
    "view_count": Book.view_count,
    "title": Book.title,
}

#: Formats that can be filtered on, mapped to the column holding their file.
#: Audiobooks are excluded: they have no key column, so "has a file" cannot be
#: answered for them without an unindexable scan of the JSON ``formats`` list.
FORMAT_COLUMNS: dict[str, Any] = {
    "pdf": Book.pdf_key,
    "epub": Book.epub_key,
    "mobi": Book.mobi_key,
}

#: Sort keys whose column is nullable. Rows with a NULL sort key can never be
#: positioned by a keyset comparison, so they are excluded from that ordering
#: rather than being silently dropped on page two.
NULLABLE_SORTS = frozenset({"published_at"})

#: Sort keys whose cursor value needs rehydrating from its JSON form.
_CURSOR_DECODERS: dict[str, Any] = {
    "published_at": datetime.fromisoformat,
    "created_at": datetime.fromisoformat,
}

#: Facet buckets, in minor units. Deliberately coarse — a facet is a navigation
#: aid, not a report.
PRICE_BUCKETS: tuple[tuple[str, str, int, int | None], ...] = (
    ("free", "Free", 0, 0),
    ("under_200", "Under ₹200", 1, 20_000),
    ("200_500", "₹200 – ₹500", 20_001, 50_000),  # noqa: RUF001 - en dash is intentional prose
    ("over_500", "Over ₹500", 50_001, None),
)


@dataclass(slots=True)
class BookFilters:
    """Catalogue filters. All optional; all applied server-side."""

    q: str | None = None
    category: str | None = None
    author: str | None = None
    publisher: str | None = None
    language: str | None = None
    book_format: str | None = None
    min_price_minor: int | None = None
    max_price_minor: int | None = None
    free_only: bool = False
    #: Admin listings only. Public listings are pinned to ``published``.
    status: str | None = None


def _serialise_cursor_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _slugify_conflict(slug: str) -> ConflictError:
    return ConflictError("That slug is already taken.", details={"slug": slug})


class CatalogueService:
    """Everything that reads or writes a :class:`~models.Book`."""

    def __init__(self, settings: Settings, cache: CatalogueCache) -> None:
        self._settings = settings
        self._cache = cache

    # ---- loader options -------------------------------------------------

    @staticmethod
    def list_options() -> tuple[Any, ...]:
        """What a catalogue card needs: authors and categories, two extra queries.

        ``selectinload`` rather than ``joinedload``: a book with 3 authors and 4
        categories would otherwise be returned as 12 duplicated rows, and the
        duplication multiplies across the page.
        """
        return (
            selectinload(Book.author_links).selectinload(BookAuthor.author),
            selectinload(Book.category_links).selectinload(BookCategory.category),
        )

    @classmethod
    def detail_options(cls) -> tuple[Any, ...]:
        return (*cls.list_options(), selectinload(Book.publisher))

    # ---- lookups --------------------------------------------------------

    async def get_by_id(
        self,
        session: AsyncSession,
        book_id: uuid.UUID,
        *,
        include_unpublished: bool = False,
        detail: bool = True,
    ) -> Book:
        book = await self._fetch(
            session,
            Book.id == book_id,
            include_unpublished=include_unpublished,
            detail=detail,
        )
        if book is None:
            raise NotFoundError("Book not found.", details={"book_id": str(book_id)})
        return book

    async def get_by_slug(
        self,
        session: AsyncSession,
        slug: str,
        *,
        include_unpublished: bool = False,
        detail: bool = True,
    ) -> Book:
        book = await self._fetch(
            session, Book.slug == slug, include_unpublished=include_unpublished, detail=detail
        )
        if book is None:
            raise NotFoundError("Book not found.", details={"slug": slug})
        return book

    async def _fetch(
        self,
        session: AsyncSession,
        condition: Any,
        *,
        include_unpublished: bool,
        detail: bool,
    ) -> Book | None:
        options = self.detail_options() if detail else self.list_options()
        stmt = select(Book).where(condition, Book.deleted_at.is_(None)).options(*options)
        if not include_unpublished:
            stmt = stmt.where(Book.status == BookStatus.PUBLISHED)
        return (await session.execute(stmt)).scalars().unique().one_or_none()

    async def batch(
        self, session: AsyncSession, book_ids: list[uuid.UUID], *, include_unpublished: bool
    ) -> list[Book]:
        if not book_ids:
            return []
        stmt = (
            select(Book)
            .where(Book.id.in_(book_ids), Book.deleted_at.is_(None))
            .options(*self.detail_options())
        )
        if not include_unpublished:
            stmt = stmt.where(Book.status == BookStatus.PUBLISHED)
        return list((await session.execute(stmt)).scalars().unique().all())

    # ---- filtering ------------------------------------------------------

    def base_query(self, filters: BookFilters, *, public: bool) -> Select[Any]:
        stmt = select(Book).where(Book.deleted_at.is_(None))

        if public:
            stmt = stmt.where(Book.status == BookStatus.PUBLISHED)
        elif filters.status:
            stmt = stmt.where(Book.status == filters.status)

        if filters.q:
            stmt = apply_search(stmt, Book, term=filters.q, fields=["title", "subtitle"])
        if filters.category:
            stmt = stmt.where(
                Book.id.in_(
                    select(BookCategory.book_id)
                    .join(Category, Category.id == BookCategory.category_id)
                    .where(Category.slug == filters.category, Category.deleted_at.is_(None))
                )
            )
        if filters.author:
            stmt = stmt.where(
                Book.id.in_(
                    select(BookAuthor.book_id)
                    .join(Author, Author.id == BookAuthor.author_id)
                    .where(Author.slug == filters.author, Author.deleted_at.is_(None))
                )
            )
        if filters.publisher:
            stmt = stmt.where(
                Book.publisher_id.in_(
                    select(Publisher.id).where(
                        Publisher.slug == filters.publisher, Publisher.deleted_at.is_(None)
                    )
                )
            )
        if filters.language:
            stmt = stmt.where(Book.language == filters.language)
        if filters.book_format:
            column = FORMAT_COLUMNS.get(filters.book_format)
            if column is None:
                raise BadRequestError(
                    f"Cannot filter by format '{filters.book_format}'.",
                    details={"allowed": sorted(FORMAT_COLUMNS)},
                )
            # Filter on the stored file, not the declared `formats` list: a format
            # with no file is one the reader cannot actually open.
            stmt = stmt.where(column.is_not(None))
        if filters.free_only:
            stmt = stmt.where(or_(Book.price_minor == 0, Book.discount_price_minor == 0))
        if filters.min_price_minor is not None:
            stmt = stmt.where(Book.price_minor >= filters.min_price_minor)
        if filters.max_price_minor is not None:
            stmt = stmt.where(Book.price_minor <= filters.max_price_minor)
        return stmt

    # ---- cursor pagination ----------------------------------------------

    async def list_books(
        self,
        session: AsyncSession,
        *,
        filters: BookFilters,
        sort_by: str = "published_at",
        sort_order: str = "desc",
        cursor: str | None = None,
        limit: int = 20,
        public: bool = True,
    ) -> tuple[list[Book], str | None, bool]:
        """Return ``(rows, next_cursor, has_more)``.

        One row beyond the requested limit is fetched to decide ``has_more``
        without a second COUNT query.
        """
        if sort_by not in SORTABLE:
            raise BadRequestError(
                f"Cannot sort by '{sort_by}'.", details={"allowed": sorted(SORTABLE)}
            )
        if sort_order not in {"asc", "desc"}:
            raise BadRequestError("sort_order must be 'asc' or 'desc'.")

        column = SORTABLE[sort_by]
        stmt = self.base_query(filters, public=public).options(*self.list_options())
        if sort_by in NULLABLE_SORTS:
            stmt = stmt.where(column.is_not(None))

        if cursor:
            last_value, last_id = self._decode_cursor(cursor, sort_by, sort_order)
            if sort_order == "desc":
                stmt = stmt.where(
                    or_(column < last_value, and_(column == last_value, Book.id < last_id))
                )
            else:
                stmt = stmt.where(
                    or_(column > last_value, and_(column == last_value, Book.id > last_id))
                )

        ordering = (
            (column.desc(), Book.id.desc())
            if sort_order == "desc"
            else (column.asc(), Book.id.asc())
        )
        stmt = stmt.order_by(*ordering).limit(limit + 1)

        rows = list((await session.execute(stmt)).scalars().unique().all())
        has_more = len(rows) > limit
        rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = encode_cursor(
                {
                    "s": sort_by,
                    "o": sort_order,
                    "v": _serialise_cursor_value(getattr(last, sort_by)),
                    "id": str(last.id),
                }
            )
        return rows, next_cursor, has_more

    @staticmethod
    def _decode_cursor(cursor: str, sort_by: str, sort_order: str) -> tuple[Any, uuid.UUID]:
        data = decode_cursor(cursor)
        # A cursor is only meaningful for the ordering it was minted under; reusing
        # it against a different sort silently returns a wrong slice of the feed.
        if data.get("s") != sort_by or data.get("o") != sort_order:
            raise BadRequestError(
                "This cursor was issued for a different ordering.",
                details={"sort_by": sort_by, "sort_order": sort_order},
            )
        try:
            raw = data["v"]
            decoder = _CURSOR_DECODERS.get(sort_by)
            value = decoder(raw) if decoder is not None else raw
            return value, uuid.UUID(str(data["id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise BadRequestError("The pagination cursor is malformed.") from exc

    # ---- facets ---------------------------------------------------------

    async def facets(
        self, session: AsyncSession, filters: BookFilters, *, public: bool = True
    ) -> CatalogueFacets:
        """Counts for the filter sidebar, computed under the *same* filters.

        Three grouped aggregates rather than one query per facet value; each is a
        single index scan over the already-filtered set.
        """
        base = self.base_query(filters, public=public).with_only_columns(Book.id).order_by(None)
        book_ids = base.subquery()

        category_rows = (
            await session.execute(
                select(Category.slug, Category.name, func.count(BookCategory.book_id))
                .join(BookCategory, BookCategory.category_id == Category.id)
                .where(
                    BookCategory.book_id.in_(select(book_ids.c.id)),
                    Category.deleted_at.is_(None),
                )
                .group_by(Category.slug, Category.name)
                .order_by(func.count(BookCategory.book_id).desc(), Category.name)
            )
        ).all()

        language_rows = (
            await session.execute(
                select(Book.language, func.count(Book.id))
                .where(Book.id.in_(select(book_ids.c.id)))
                .group_by(Book.language)
                .order_by(func.count(Book.id).desc(), Book.language)
            )
        ).all()

        price_columns = []
        for key, _label, low, high in PRICE_BUCKETS:
            condition = Book.price_minor >= low
            if high is not None:
                condition = and_(condition, Book.price_minor <= high)
            price_columns.append(func.sum(case((condition, 1), else_=0)).label(key))
        price_row = (
            await session.execute(select(*price_columns).where(Book.id.in_(select(book_ids.c.id))))
        ).one()

        return CatalogueFacets(
            categories=[
                FacetValue(value=slug, label=name, count=int(count))
                for slug, name, count in category_rows
            ],
            languages=[
                FacetValue(value=code, label=code, count=int(count))
                for code, count in language_rows
            ],
            price_ranges=[
                FacetValue(value=key, label=label, count=int(getattr(price_row, key) or 0))
                for key, label, _low, _high in PRICE_BUCKETS
            ],
        )

    # ---- related --------------------------------------------------------

    async def related(self, session: AsyncSession, book: Book, *, limit: int) -> list[Book]:
        """Books sharing a category with this one, best-rated first.

        Deliberately not a recommender: this is the "readers also liked" strip and
        it must render from one index scan, not a model call.
        """
        category_ids = select(BookCategory.category_id).where(BookCategory.book_id == book.id)
        stmt = (
            select(Book)
            .where(
                Book.id != book.id,
                Book.deleted_at.is_(None),
                Book.status == BookStatus.PUBLISHED,
                Book.id.in_(
                    select(BookCategory.book_id).where(BookCategory.category_id.in_(category_ids))
                ),
            )
            .options(*self.list_options())
            .order_by(Book.rating_average.desc(), Book.rating_count.desc(), Book.id.desc())
            .limit(limit)
        )
        rows = list((await session.execute(stmt)).scalars().unique().all())
        if rows:
            return rows

        # No shared category (a brand new or uncategorised book): fall back to the
        # best-rated published books so the strip is never empty.
        fallback = (
            select(Book)
            .where(
                Book.id != book.id,
                Book.deleted_at.is_(None),
                Book.status == BookStatus.PUBLISHED,
            )
            .options(*self.list_options())
            .order_by(Book.rating_average.desc(), Book.id.desc())
            .limit(limit)
        )
        return list((await session.execute(fallback)).scalars().unique().all())

    # ---- writes ---------------------------------------------------------

    async def create(
        self, session: AsyncSession, payload: BookCreate, *, actor_id: uuid.UUID | None
    ) -> Book:
        await self._assert_slug_free(session, payload.slug)
        data = payload.model_dump(exclude={"author_ids", "category_ids"})
        book = Book(**data, created_by=actor_id, updated_by=actor_id)
        session.add(book)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise _slugify_conflict(payload.slug) from exc

        await self._replace_contributors(session, book, payload.author_ids)
        await self._replace_categories(session, book, payload.category_ids)
        await session.flush()
        logger.info("book.created", book_id=str(book.id), slug=book.slug)
        return await self.get_by_id(session, book.id, include_unpublished=True)

    async def update(
        self,
        session: AsyncSession,
        book: Book,
        payload: BookUpdate,
        *,
        actor_id: uuid.UUID | None,
    ) -> Book:
        previous_slug = book.slug
        data = payload.model_dump(exclude_unset=True, exclude={"author_ids", "category_ids"})
        if "slug" in data and data["slug"] != book.slug:
            await self._assert_slug_free(session, data["slug"])
        for field, value in data.items():
            setattr(book, field, value)
        book.updated_by = actor_id

        if payload.author_ids is not None:
            await self._replace_contributors(session, book, payload.author_ids)
        if payload.category_ids is not None:
            await self._replace_categories(session, book, payload.category_ids)

        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise _slugify_conflict(str(data.get("slug", book.slug))) from exc

        await self._cache.invalidate_book(book_id=book.id, slugs=(previous_slug, book.slug))
        logger.info("book.updated", book_id=str(book.id), fields=sorted(data))
        return await self.get_by_id(session, book.id, include_unpublished=True)

    async def publish(
        self,
        session: AsyncSession,
        book: Book,
        *,
        actor_id: uuid.UUID | None,
        published_at: datetime | None = None,
    ) -> Book:
        if not book.available_formats:
            # Publishing a book with no readable file produces a store listing that
            # sells something nobody can open.
            raise ConflictError(
                "This book has no uploaded file, so it cannot be published.",
                details={"book_id": str(book.id)},
            )
        book.status = BookStatus.PUBLISHED
        book.published_at = published_at or book.published_at or datetime.now(UTC)
        book.updated_by = actor_id
        await session.flush()
        await self._cache.invalidate_book(book_id=book.id, slugs=(book.slug,))
        logger.info("book.published", book_id=str(book.id), slug=book.slug)
        return book

    async def unpublish(
        self, session: AsyncSession, book: Book, *, actor_id: uuid.UUID | None
    ) -> Book:
        book.status = BookStatus.UNPUBLISHED
        book.updated_by = actor_id
        await session.flush()
        await self._cache.invalidate_book(book_id=book.id, slugs=(book.slug,))
        logger.info("book.unpublished", book_id=str(book.id), slug=book.slug)
        return book

    async def soft_delete(
        self, session: AsyncSession, book: Book, *, actor_id: uuid.UUID | None
    ) -> None:
        """Soft delete. Entitlements reference books with ``ON DELETE RESTRICT``:
        someone paid for this file and must keep being able to open it."""
        book.deleted_at = datetime.now(UTC)
        book.status = BookStatus.ARCHIVED
        book.updated_by = actor_id
        await session.flush()
        await self._cache.invalidate_book(book_id=book.id, slugs=(book.slug,))
        logger.info("book.deleted", book_id=str(book.id), slug=book.slug)

    async def add_version(
        self,
        session: AsyncSession,
        book: Book,
        payload: BookVersionCreate,
        *,
        actor_id: uuid.UUID | None,
    ) -> BookVersion:
        """Record a new edition and promote its files onto the book.

        The previous version's rows are left untouched, so a bad upload is reversed
        by pointing the book back at the older keys rather than by restoring a
        backup.
        """
        next_version = book.version + 1
        version = BookVersion(
            book_id=book.id,
            version=next_version,
            created_by=actor_id,
            **payload.model_dump(),
        )
        session.add(version)
        for field in ("pdf_key", "epub_key", "mobi_key", "cover_key"):
            value = getattr(payload, field)
            if value is not None:
                setattr(book, field, value)
        book.version = next_version
        book.updated_by = actor_id
        await session.flush()
        await self._cache.invalidate_book(book_id=book.id, slugs=(book.slug,))
        return version

    async def increment_download_count(self, session: AsyncSession, book_id: uuid.UUID) -> None:
        """In-database increment so concurrent downloads do not lose counts.

        A read-modify-write in Python would drop increments under load; the counter
        is also not worth a row lock, hence the UPDATE-in-place.
        """
        await session.execute(
            Book.__table__.update()
            .where(Book.id == book_id)
            .values(download_count=Book.download_count + 1)
        )

    # ---- helpers --------------------------------------------------------

    async def _assert_slug_free(self, session: AsyncSession, slug: str) -> None:
        exists = (
            await session.execute(select(Book.id).where(Book.slug == slug).limit(1))
        ).scalar_one_or_none()
        if exists is not None:
            raise _slugify_conflict(slug)

    async def _replace_contributors(
        self, session: AsyncSession, book: Book, contributors: list[Any]
    ) -> None:
        await session.execute(delete(BookAuthor).where(BookAuthor.book_id == book.id))
        if not contributors:
            return
        known = set(
            (
                await session.execute(
                    select(Author.id).where(
                        Author.id.in_([c.author_id for c in contributors]),
                        Author.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        missing = [str(c.author_id) for c in contributors if c.author_id not in known]
        if missing:
            raise NotFoundError(
                "One or more authors do not exist.", details={"author_ids": missing}
            )
        seen: set[uuid.UUID] = set()
        for contributor in contributors:
            if contributor.author_id in seen:
                continue
            seen.add(contributor.author_id)
            session.add(
                BookAuthor(
                    book_id=book.id,
                    author_id=contributor.author_id,
                    role=contributor.role,
                    display_order=contributor.display_order,
                )
            )

    async def _replace_categories(
        self, session: AsyncSession, book: Book, category_ids: list[uuid.UUID]
    ) -> None:
        await session.execute(delete(BookCategory).where(BookCategory.book_id == book.id))
        if not category_ids:
            return
        known = set(
            (
                await session.execute(
                    select(Category.id).where(
                        Category.id.in_(category_ids), Category.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        missing = [str(cid) for cid in category_ids if cid not in known]
        if missing:
            raise NotFoundError(
                "One or more categories do not exist.", details={"category_ids": missing}
            )
        for index, category_id in enumerate(dict.fromkeys(category_ids)):
            session.add(
                BookCategory(book_id=book.id, category_id=category_id, is_primary=index == 0)
            )

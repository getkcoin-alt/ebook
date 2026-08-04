"""SQLAlchemy models for the books service.

Everything lives in the ``books`` schema (ADR 0002). No other service reads these
tables; cross-service data travels over HTTP or the event stream.

Three conventions worth knowing before editing this file:

* **Money is integer minor units.** ``price_minor`` is paise/cents. There is no
  ``Float`` or ``Numeric`` price column anywhere, and there never will be.
* **Rating is denormalised.** ``rating_average`` / ``rating_count`` / ``rating_sum``
  live on ``books`` and are maintained on review writes. The catalogue must never
  ``AVG()`` across ``reviews`` on a read.
* **Relationships are ``lazy="raise_on_sql"``.** A relationship that lazy-loads in a
  list endpoint is an N+1 in production; making it raise means the query has to
  declare its ``selectinload`` up front and the mistake is caught in tests instead
  of in the p99.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from knowledgeos_core import (
    Base,
    BookFormat,
    BookStatus,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from schemas import ContributorRole, EntitlementSource, ReviewStatus

#: This service's schema. Set explicitly on every table.
SCHEMA = "books"

#: JSON on any backend, JSONB on PostgreSQL (indexable, no re-parse on read).
#: The variant keeps the models usable against SQLite in tests.
JSONType = JSON().with_variant(JSONB, "postgresql")


def _enum(enum_cls: type, name: str) -> SAEnum:
    """A VARCHAR + CHECK constraint rather than a native PostgreSQL ENUM type.

    Native enums need an ``ALTER TYPE`` (which cannot run inside a transaction on
    older PostgreSQL) to add a value; a check constraint is a plain, reversible
    migration. Also keeps the models portable for tests.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


def _uuid_fk(target: str, *, ondelete: str = "CASCADE", **kwargs: object) -> Mapped[uuid.UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), ForeignKey(f"{SCHEMA}.{target}", ondelete=ondelete), **kwargs
    )


# ---------------------------------------------------------------------------
# Catalogue reference data
# ---------------------------------------------------------------------------


class Publisher(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "publishers"
    __table_args__ = (
        Index("ix_publishers_name", "name"),
        {"schema": SCHEMA},
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(String(500))
    logo_key: Mapped[str | None] = mapped_column(String(500))
    contact_email: Mapped[str | None] = mapped_column(String(320))


class Author(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "authors"
    __table_args__ = (
        Index("ix_authors_name", "name"),
        Index("ix_authors_is_featured_name", "is_featured", "name"),
        {"schema": SCHEMA},
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    bio: Mapped[str | None] = mapped_column(Text)
    avatar_key: Mapped[str | None] = mapped_column(String(500))
    website: Mapped[str | None] = mapped_column(String(500))
    socials: Mapped[dict[str, str]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    is_featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class Category(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """Self-referencing tree. Depth is small (2-3), so the tree is assembled in
    Python from one flat SELECT and cached, rather than with a recursive CTE per
    request."""

    __tablename__ = "categories"
    __table_args__ = (
        Index("ix_categories_parent_id_display_order", "parent_id", "display_order"),
        Index("ix_categories_is_active_display_order", "is_active", "display_order"),
        {"schema": SCHEMA},
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(100))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


class Book(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "books"
    __table_args__ = (
        # The catalogue feed: WHERE status = :s ORDER BY published_at DESC, id DESC.
        # `id` is in the index so the keyset tiebreaker is served from it too. The
        # index is stored ASC: PostgreSQL scans a btree backwards at the same cost,
        # so a DESC index would buy nothing and cost portability.
        Index("ix_books_status_published_at_id", "status", "published_at", "id"),
        Index("ix_books_status_created_at_id", "status", "created_at", "id"),
        Index("ix_books_status_price_minor_id", "status", "price_minor", "id"),
        Index("ix_books_status_rating_average_id", "status", "rating_average", "id"),
        Index("ix_books_status_view_count_id", "status", "view_count", "id"),
        Index("ix_books_status_language", "status", "language"),
        Index("ix_books_publisher_id", "publisher_id"),
        Index("ix_books_created_by", "created_by"),
        CheckConstraint("price_minor >= 0", name="price_minor_non_negative"),
        CheckConstraint(
            "discount_price_minor IS NULL OR discount_price_minor <= price_minor",
            name="discount_not_above_price",
        ),
        CheckConstraint("rating_count >= 0", name="rating_count_non_negative"),
        {"schema": SCHEMA},
    )

    # ---- identity ----
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    subtitle: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    isbn10: Mapped[str | None] = mapped_column(String(13), unique=True)
    isbn13: Mapped[str | None] = mapped_column(String(17), unique=True)
    language: Mapped[str] = mapped_column(
        String(10), nullable=False, default="en", server_default=text("'en'")
    )
    page_count: Mapped[int | None] = mapped_column(Integer)
    publication_date: Mapped[date | None] = mapped_column(Date)

    status: Mapped[BookStatus] = mapped_column(
        _enum(BookStatus, "book_status"),
        nullable=False,
        default=BookStatus.DRAFT,
        server_default=text("'draft'"),
    )

    # ---- commerce (integer minor units, never float) ----
    price_minor: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    discount_price_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default=text("'INR'")
    )

    # ---- files ----
    formats: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )
    pdf_key: Mapped[str | None] = mapped_column(String(500))
    epub_key: Mapped[str | None] = mapped_column(String(500))
    mobi_key: Mapped[str | None] = mapped_column(String(500))
    sample_key: Mapped[str | None] = mapped_column(String(500))
    cover_key: Mapped[str | None] = mapped_column(String(500))
    thumbnail_key: Mapped[str | None] = mapped_column(String(500))

    # ---- SEO ----
    meta_title: Mapped[str | None] = mapped_column(String(255))
    meta_description: Mapped[str | None] = mapped_column(String(500))
    og_image_key: Mapped[str | None] = mapped_column(String(500))

    # ---- AI-generated ----
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_tags: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list, server_default=text("'[]'")
    )

    # ---- denormalised aggregates ----
    # Maintained on review write. `rating_sum` exists so the average can be
    # recomputed in O(1) on every insert/update/delete without an AVG() scan.
    rating_average: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    rating_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    rating_sum: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    view_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    download_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )

    # ---- lifecycle ----
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    publisher_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.publishers.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    publisher: Mapped[Publisher | None] = relationship(lazy="raise_on_sql")
    author_links: Mapped[list[BookAuthor]] = relationship(
        back_populates="book",
        cascade="all, delete-orphan",
        order_by="BookAuthor.display_order",
        lazy="raise_on_sql",
    )
    category_links: Mapped[list[BookCategory]] = relationship(
        back_populates="book",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
    )
    versions: Mapped[list[BookVersion]] = relationship(
        back_populates="book",
        cascade="all, delete-orphan",
        order_by="BookVersion.version.desc()",
        lazy="raise_on_sql",
    )

    # ---- derived, read by the response schemas ----

    @property
    def effective_price_minor(self) -> int:
        if self.discount_price_minor is not None and self.discount_price_minor < self.price_minor:
            return self.discount_price_minor
        return self.price_minor

    @property
    def is_free(self) -> bool:
        return self.effective_price_minor == 0

    @property
    def has_sample(self) -> bool:
        return bool(self.sample_key)

    @property
    def available_formats(self) -> list[str]:
        """Formats a reader can actually open — i.e. those with a stored file."""
        present = [
            fmt
            for fmt, key in (
                (BookFormat.PDF.value, self.pdf_key),
                (BookFormat.EPUB.value, self.epub_key),
                (BookFormat.MOBI.value, self.mobi_key),
            )
            if key
        ]
        # Audiobooks have no key column of their own; trust the declared list.
        if BookFormat.AUDIOBOOK.value in (self.formats or []):
            present.append(BookFormat.AUDIOBOOK.value)
        return present

    @property
    def authors(self) -> list[Author]:
        return [link.author for link in self.author_links]

    @property
    def categories(self) -> list[Category]:
        return [link.category for link in self.category_links]

    @property
    def contributors(self) -> list[BookAuthor]:
        return list(self.author_links)

    def file_key_for(self, book_format: str) -> str | None:
        return {
            BookFormat.PDF.value: self.pdf_key,
            BookFormat.EPUB.value: self.epub_key,
            BookFormat.MOBI.value: self.mobi_key,
        }.get(book_format)


class BookAuthor(Base, TimestampMixin):
    """Association object: a book's contributors, with role and ordering."""

    __tablename__ = "book_authors"
    __table_args__ = (
        Index("ix_book_authors_author_id_book_id", "author_id", "book_id"),
        {"schema": SCHEMA},
    )

    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", primary_key=True)
    author_id: Mapped[uuid.UUID] = _uuid_fk("authors.id", primary_key=True)
    role: Mapped[ContributorRole] = mapped_column(
        _enum(ContributorRole, "contributor_role"),
        nullable=False,
        default=ContributorRole.AUTHOR,
        server_default=text("'author'"),
    )
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    book: Mapped[Book] = relationship(back_populates="author_links", lazy="raise_on_sql")
    author: Mapped[Author] = relationship(lazy="raise_on_sql")


class BookCategory(Base, TimestampMixin):
    __tablename__ = "book_categories"
    __table_args__ = (
        Index("ix_book_categories_category_id_book_id", "category_id", "book_id"),
        {"schema": SCHEMA},
    )

    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", primary_key=True)
    category_id: Mapped[uuid.UUID] = _uuid_fk("categories.id", primary_key=True)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    book: Mapped[Book] = relationship(back_populates="category_links", lazy="raise_on_sql")
    category: Mapped[Category] = relationship(lazy="raise_on_sql")


class BookVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One immutable edition of a book's files.

    A re-upload writes a new row rather than overwriting the previous keys, so the
    edition a customer already downloaded remains retrievable and an accidental
    upload is reversible.
    """

    __tablename__ = "book_versions"
    __table_args__ = (
        UniqueConstraint("book_id", "version", name="uq_book_versions_book_id_version"),
        Index("ix_book_versions_book_id_version", "book_id", "version"),
        {"schema": SCHEMA},
    )

    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    pdf_key: Mapped[str | None] = mapped_column(String(500))
    epub_key: Mapped[str | None] = mapped_column(String(500))
    mobi_key: Mapped[str | None] = mapped_column(String(500))
    cover_key: Mapped[str | None] = mapped_column(String(500))
    changelog: Mapped[str | None] = mapped_column(Text)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    book: Mapped[Book] = relationship(back_populates="versions", lazy="raise_on_sql")


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


class Review(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "reviews"
    __table_args__ = (
        # One review per user per book, enforced by the database rather than by a
        # check-then-insert that two concurrent requests can both pass.
        UniqueConstraint("book_id", "user_id", name="uq_reviews_book_id_user_id"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="rating_between_1_and_5"),
        Index("ix_reviews_book_id_status_created_at_id", "book_id", "status", "created_at", "id"),
        Index("ix_reviews_book_id_status_helpful_count_id", "book_id", "status", "helpful_count",
              "id"),
        Index("ix_reviews_user_id_created_at", "user_id", "created_at"),
        {"schema": SCHEMA},
    )

    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", nullable=False)
    #: The reviewer, taken from the access token — never from the request body.
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    verified_purchase: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[ReviewStatus] = mapped_column(
        _enum(ReviewStatus, "review_status"),
        nullable=False,
        default=ReviewStatus.APPROVED,
        server_default=text("'approved'"),
    )
    helpful_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    moderated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    moderation_note: Mapped[str | None] = mapped_column(String(1_000))


class ReviewVote(Base, TimestampMixin):
    """One helpful/unhelpful vote per user per review."""

    __tablename__ = "review_votes"
    __table_args__ = (
        Index("ix_review_votes_user_id", "user_id"),
        {"schema": SCHEMA},
    )

    review_id: Mapped[uuid.UUID] = _uuid_fk("reviews.id", primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    is_helpful: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


# ---------------------------------------------------------------------------
# Reading: bookmarks, progress
# ---------------------------------------------------------------------------


class Bookmark(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "bookmarks"
    __table_args__ = (
        Index("ix_bookmarks_user_id_book_id_created_at_id", "user_id", "book_id", "created_at",
              "id"),
        Index("ix_bookmarks_book_id", "book_id"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", nullable=False)
    position: Mapped[str] = mapped_column(String(255), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    colour: Mapped[str | None] = mapped_column(String(16))


class ReadingProgress(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "reading_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_reading_progress_user_id_book_id"),
        Index("ix_reading_progress_user_id_last_read_at_id", "user_id", "last_read_at", "id"),
        Index("ix_reading_progress_book_id", "book_id"),
        CheckConstraint("percent >= 0 AND percent <= 100", name="percent_between_0_and_100"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", nullable=False)
    position: Mapped[str | None] = mapped_column(String(255))
    page_number: Mapped[int | None] = mapped_column(Integer)
    percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    total_reading_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_finished: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# User-curated lists
# ---------------------------------------------------------------------------


class WishlistItem(Base, TimestampMixin):
    __tablename__ = "wishlist"
    __table_args__ = (
        Index("ix_wishlist_user_id_created_at_book_id", "user_id", "created_at", "book_id"),
        Index("ix_wishlist_book_id", "book_id"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", primary_key=True)
    note: Mapped[str | None] = mapped_column(String(1_000))


class Collection(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="uq_collections_user_id_slug"),
        Index("ix_collections_user_id_created_at_id", "user_id", "created_at", "id"),
        Index("ix_collections_is_public_created_at_id", "is_public", "created_at", "id"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    cover_key: Mapped[str | None] = mapped_column(String(500))
    item_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    items: Mapped[list[CollectionItem]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        order_by="CollectionItem.display_order",
        lazy="raise_on_sql",
    )


class CollectionItem(Base, TimestampMixin):
    __tablename__ = "collection_items"
    __table_args__ = (
        Index("ix_collection_items_collection_id_display_order", "collection_id", "display_order"),
        Index("ix_collection_items_book_id", "book_id"),
        {"schema": SCHEMA},
    )

    collection_id: Mapped[uuid.UUID] = _uuid_fk("collections.id", primary_key=True)
    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", primary_key=True)
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    note: Mapped[str | None] = mapped_column(String(1_000))

    collection: Mapped[Collection] = relationship(back_populates="items", lazy="raise_on_sql")


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class Entitlement(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Who may read or download which book, and why.

    This is the access-control table the reader depends on. Nothing else grants
    access to a paid file — not an order row, not a session flag, not a claim in a
    token. The unique constraint over ``(user_id, book_id, source, external_ref)``
    is what makes a redelivered ``payment.succeeded`` a no-op rather than a
    duplicate grant.
    """

    __tablename__ = "entitlements"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "book_id",
            "source",
            "external_ref",
            name="uq_entitlements_user_id_book_id_source_external_ref",
        ),
        Index("ix_entitlements_user_id_granted_at_id", "user_id", "granted_at", "id"),
        Index("ix_entitlements_user_id_book_id", "user_id", "book_id"),
        Index("ix_entitlements_book_id", "book_id"),
        Index("ix_entitlements_order_id", "order_id"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    book_id: Mapped[uuid.UUID] = _uuid_fk("books.id", ondelete="RESTRICT", nullable=False)
    source: Mapped[EntitlementSource] = mapped_column(
        _enum(EntitlementSource, "entitlement_source"), nullable=False
    )
    #: Order id, payment id or subscription id — whatever the granting fact was.
    #: Not nullable (empty string instead) because PostgreSQL treats NULLs as
    #: distinct in a unique constraint, which would defeat the idempotency guard.
    external_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=text("''")
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    can_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    can_download: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(500))


class ProcessedEvent(Base):
    """Idempotency ledger for the event consumer.

    Redis Streams deliver at-least-once. Recording the event id before acting, in
    the same transaction as the effect, turns a redelivery into a no-op.
    """

    __tablename__ = "processed_events"
    __table_args__ = ({"schema": SCHEMA},)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

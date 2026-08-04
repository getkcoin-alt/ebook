"""Request and response schemas for the books service.

Every schema subclasses :class:`~knowledgeos_core.BaseSchema`, which sets
``from_attributes=True`` (so responses are built straight from ORM rows) and
``extra="forbid"`` (so a typo'd field is a 422 rather than being silently dropped).

Money is always an **integer of the currency's minor unit** — paise, cents. There is
no float price anywhere in this service.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from knowledgeos_core import BaseSchema, BookFormat, BookStatus, Currency

#: Slugs are the public identity of a book/author/category, so they are restricted
#: to a shape that is safe in a URL path and stable in a sitemap.
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


# ---------------------------------------------------------------------------
# Enums owned by this service
# ---------------------------------------------------------------------------


class ReviewStatus(StrEnum):
    """Moderation state of a review."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FLAGGED = "flagged"


class EntitlementSource(StrEnum):
    """Why a user may read a book. This is the audit trail for access."""

    PURCHASE = "purchase"
    SUBSCRIPTION = "subscription"
    FREE = "free"
    GIFT = "gift"
    ADMIN = "admin"


class ContributorRole(StrEnum):
    AUTHOR = "author"
    CO_AUTHOR = "co_author"
    EDITOR = "editor"
    TRANSLATOR = "translator"
    ILLUSTRATOR = "illustrator"
    NARRATOR = "narrator"


class UploadCategory(StrEnum):
    """Upload kinds this service mints presigned targets for.

    Mirrors the allowlist in ``knowledgeos_core.storage.ALLOWED_UPLOAD_TYPES``; the
    storage layer validates the declared content type against it regardless.
    """

    BOOK = "book"
    COVER = "cover"
    AVATAR = "avatar"


# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------


class PublisherCreate(BaseSchema):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5_000)
    website: str | None = Field(default=None, max_length=500)
    logo_key: str | None = Field(default=None, max_length=500)
    contact_email: str | None = Field(default=None, max_length=320)


class PublisherUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5_000)
    website: str | None = Field(default=None, max_length=500)
    logo_key: str | None = Field(default=None, max_length=500)
    contact_email: str | None = Field(default=None, max_length=320)


class PublisherSummary(BaseSchema):
    id: uuid.UUID
    name: str
    slug: str
    logo_key: str | None = None


class PublisherOut(PublisherSummary):
    description: str | None = None
    website: str | None = None
    contact_email: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------


class AuthorCreate(BaseSchema):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=SLUG_PATTERN)
    bio: str | None = Field(default=None, max_length=10_000)
    avatar_key: str | None = Field(default=None, max_length=500)
    website: str | None = Field(default=None, max_length=500)
    socials: dict[str, str] = Field(default_factory=dict)
    is_featured: bool = False


class AuthorUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=SLUG_PATTERN)
    bio: str | None = Field(default=None, max_length=10_000)
    avatar_key: str | None = Field(default=None, max_length=500)
    website: str | None = Field(default=None, max_length=500)
    socials: dict[str, str] | None = None
    is_featured: bool | None = None


class AuthorSummary(BaseSchema):
    id: uuid.UUID
    name: str
    slug: str
    avatar_key: str | None = None


class AuthorOut(AuthorSummary):
    bio: str | None = None
    website: str | None = None
    socials: dict[str, str] = Field(default_factory=dict)
    is_featured: bool = False
    created_at: datetime
    updated_at: datetime


class ContributorOut(BaseSchema):
    """An author together with the role they played on a specific book."""

    author: AuthorSummary
    role: ContributorRole
    display_order: int = 0


class BookContributorIn(BaseSchema):
    author_id: uuid.UUID
    role: ContributorRole = ContributorRole.AUTHOR
    display_order: int = Field(default=0, ge=0, le=1_000)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class CategoryCreate(BaseSchema):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5_000)
    icon: str | None = Field(default=None, max_length=100)
    parent_id: uuid.UUID | None = None
    display_order: int = Field(default=0, ge=0, le=100_000)
    is_active: bool = True


class CategoryUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5_000)
    icon: str | None = Field(default=None, max_length=100)
    parent_id: uuid.UUID | None = None
    display_order: int | None = Field(default=None, ge=0, le=100_000)
    is_active: bool | None = None


class CategorySummary(BaseSchema):
    id: uuid.UUID
    name: str
    slug: str
    icon: str | None = None


class CategoryOut(CategorySummary):
    description: str | None = None
    parent_id: uuid.UUID | None = None
    display_order: int = 0
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class CategoryNode(CategorySummary):
    """One node of the category tree."""

    description: str | None = None
    parent_id: uuid.UUID | None = None
    display_order: int = 0
    children: list[CategoryNode] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


class BookBase(BaseSchema):
    title: str = Field(min_length=1, max_length=500)
    subtitle: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=50_000)
    isbn10: str | None = Field(default=None, min_length=10, max_length=10)
    isbn13: str | None = Field(default=None, min_length=13, max_length=13)
    language: str = Field(default="en", min_length=2, max_length=10)
    page_count: int | None = Field(default=None, ge=1, le=100_000)
    publication_date: date | None = None

    price_minor: int = Field(default=0, ge=0, description="Price in minor units (paise/cents).")
    discount_price_minor: int | None = Field(default=None, ge=0)
    currency: Currency = Currency.INR

    formats: list[BookFormat] = Field(default_factory=list)
    cover_key: str | None = Field(default=None, max_length=500)
    thumbnail_key: str | None = Field(default=None, max_length=500)

    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=500)
    og_image_key: str | None = Field(default=None, max_length=500)

    ai_summary: str | None = Field(default=None, max_length=20_000)
    ai_tags: list[str] = Field(default_factory=list)

    @field_validator("isbn10", "isbn13")
    @classmethod
    def _digits_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.replace("-", "").replace(" ", "")
        # The final ISBN-10 check digit may be 'X'; everything else is numeric.
        if not stripped[:-1].isdigit() or not (stripped[-1].isdigit() or stripped[-1] in "Xx"):
            raise ValueError("ISBN must contain only digits (a trailing 'X' is allowed).")
        return stripped.upper()

    @model_validator(mode="after")
    def _discount_below_price(self) -> BookBase:
        if self.discount_price_minor is not None and self.discount_price_minor > self.price_minor:
            raise ValueError("discount_price_minor cannot exceed price_minor.")
        return self


class BookCreate(BookBase):
    slug: str = Field(min_length=1, max_length=255, pattern=SLUG_PATTERN)
    publisher_id: uuid.UUID | None = None
    author_ids: list[BookContributorIn] = Field(default_factory=list)
    category_ids: list[uuid.UUID] = Field(default_factory=list)

    pdf_key: str | None = Field(default=None, max_length=500)
    epub_key: str | None = Field(default=None, max_length=500)
    mobi_key: str | None = Field(default=None, max_length=500)
    sample_key: str | None = Field(default=None, max_length=500)


class BookUpdate(BaseSchema):
    """Partial update. Only fields explicitly present in the body are applied."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=SLUG_PATTERN)
    subtitle: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=50_000)
    isbn10: str | None = Field(default=None, min_length=10, max_length=13)
    isbn13: str | None = Field(default=None, min_length=13, max_length=17)
    language: str | None = Field(default=None, min_length=2, max_length=10)
    page_count: int | None = Field(default=None, ge=1, le=100_000)
    publication_date: date | None = None

    price_minor: int | None = Field(default=None, ge=0)
    discount_price_minor: int | None = Field(default=None, ge=0)
    currency: Currency | None = None

    formats: list[BookFormat] | None = None
    cover_key: str | None = Field(default=None, max_length=500)
    thumbnail_key: str | None = Field(default=None, max_length=500)
    pdf_key: str | None = Field(default=None, max_length=500)
    epub_key: str | None = Field(default=None, max_length=500)
    mobi_key: str | None = Field(default=None, max_length=500)
    sample_key: str | None = Field(default=None, max_length=500)

    meta_title: str | None = Field(default=None, max_length=255)
    meta_description: str | None = Field(default=None, max_length=500)
    og_image_key: str | None = Field(default=None, max_length=500)

    ai_summary: str | None = Field(default=None, max_length=20_000)
    ai_tags: list[str] | None = None

    publisher_id: uuid.UUID | None = None
    author_ids: list[BookContributorIn] | None = None
    category_ids: list[uuid.UUID] | None = None


class BookListItem(BaseSchema):
    """The catalogue card. Deliberately narrow — this is the hot list payload."""

    id: uuid.UUID
    slug: str
    title: str
    subtitle: str | None = None
    status: BookStatus
    language: str

    price_minor: int
    discount_price_minor: int | None = None
    effective_price_minor: int
    currency: Currency

    cover_key: str | None = None
    thumbnail_key: str | None = None
    available_formats: list[BookFormat] = Field(default_factory=list)

    rating_average: float = 0.0
    rating_count: int = 0
    view_count: int = 0
    published_at: datetime | None = None

    authors: list[AuthorSummary] = Field(default_factory=list)
    categories: list[CategorySummary] = Field(default_factory=list)


class BookDetail(BookListItem):
    description: str | None = None
    isbn10: str | None = None
    isbn13: str | None = None
    page_count: int | None = None
    publication_date: date | None = None

    meta_title: str | None = None
    meta_description: str | None = None
    og_image_key: str | None = None

    ai_summary: str | None = None
    ai_tags: list[str] = Field(default_factory=list)

    download_count: int = 0
    version: int = 1
    has_sample: bool = False

    publisher: PublisherSummary | None = None
    contributors: list[ContributorOut] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime


class BookAdminDetail(BookDetail):
    """Adds the private storage keys. Never returned on a public route."""

    pdf_key: str | None = None
    epub_key: str | None = None
    mobi_key: str | None = None
    sample_key: str | None = None
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None
    deleted_at: datetime | None = None


class BookVersionCreate(BaseSchema):
    pdf_key: str | None = Field(default=None, max_length=500)
    epub_key: str | None = Field(default=None, max_length=500)
    mobi_key: str | None = Field(default=None, max_length=500)
    cover_key: str | None = Field(default=None, max_length=500)
    changelog: str | None = Field(default=None, max_length=5_000)
    file_size_bytes: int | None = Field(default=None, ge=0)
    checksum: str | None = Field(default=None, max_length=128)


class BookVersionOut(BaseSchema):
    id: uuid.UUID
    book_id: uuid.UUID
    version: int
    pdf_key: str | None = None
    epub_key: str | None = None
    mobi_key: str | None = None
    cover_key: str | None = None
    changelog: str | None = None
    file_size_bytes: int | None = None
    checksum: str | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


class ReviewCreate(BaseSchema):
    rating: int = Field(ge=1, le=5)
    title: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=10_000)


class ReviewUpdate(BaseSchema):
    rating: int | None = Field(default=None, ge=1, le=5)
    title: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=10_000)


class ReviewModerate(BaseSchema):
    status: ReviewStatus
    moderation_note: str | None = Field(default=None, max_length=1_000)


class ReviewVoteIn(BaseSchema):
    is_helpful: bool = True


class ReviewOut(BaseSchema):
    id: uuid.UUID
    book_id: uuid.UUID
    user_id: uuid.UUID
    rating: int
    title: str | None = None
    body: str | None = None
    verified_purchase: bool = False
    status: ReviewStatus
    helpful_count: int = 0
    created_at: datetime
    updated_at: datetime


class RatingSummary(BaseSchema):
    book_id: uuid.UUID
    rating_average: float
    rating_count: int


# ---------------------------------------------------------------------------
# Bookmarks and reading progress
# ---------------------------------------------------------------------------


class BookmarkCreate(BaseSchema):
    position: str = Field(min_length=1, max_length=255, description="CFI or page anchor.")
    page_number: int | None = Field(default=None, ge=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=5_000)
    colour: str | None = Field(default=None, max_length=16)


class BookmarkUpdate(BaseSchema):
    position: str | None = Field(default=None, min_length=1, max_length=255)
    page_number: int | None = Field(default=None, ge=0, le=1_000_000)
    note: str | None = Field(default=None, max_length=5_000)
    colour: str | None = Field(default=None, max_length=16)


class BookmarkOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    book_id: uuid.UUID
    position: str
    page_number: int | None = None
    note: str | None = None
    colour: str | None = None
    created_at: datetime
    updated_at: datetime


class ProgressUpdate(BaseSchema):
    position: str = Field(min_length=1, max_length=255)
    percent: float = Field(ge=0.0, le=100.0)
    page_number: int | None = Field(default=None, ge=0, le=1_000_000)
    #: Seconds read since the last sync. Accumulated server-side, never overwritten
    #: from the client, so a replayed sync cannot rewrite total reading time.
    session_seconds: int = Field(default=0, ge=0, le=86_400)


class ProgressOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    book_id: uuid.UUID
    position: str | None = None
    page_number: int | None = None
    percent: float = 0.0
    total_reading_seconds: int = 0
    is_finished: bool = False
    finished_at: datetime | None = None
    last_read_at: datetime
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Wishlist and collections
# ---------------------------------------------------------------------------


class WishlistAdd(BaseSchema):
    book_id: uuid.UUID
    note: str | None = Field(default=None, max_length=1_000)


class WishlistItemOut(BaseSchema):
    user_id: uuid.UUID
    book_id: uuid.UUID
    note: str | None = None
    created_at: datetime
    book: BookListItem | None = None


class CollectionCreate(BaseSchema):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5_000)
    is_public: bool = False
    cover_key: str | None = Field(default=None, max_length=500)


class CollectionUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5_000)
    is_public: bool | None = None
    cover_key: str | None = Field(default=None, max_length=500)


class CollectionItemAdd(BaseSchema):
    book_id: uuid.UUID
    display_order: int = Field(default=0, ge=0, le=100_000)
    note: str | None = Field(default=None, max_length=1_000)


class CollectionOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    is_public: bool = False
    cover_key: str | None = None
    item_count: int = 0
    created_at: datetime
    updated_at: datetime


class CollectionDetail(CollectionOut):
    items: list[BookListItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Entitlements, library and downloads
# ---------------------------------------------------------------------------


class EntitlementOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID
    book_id: uuid.UUID
    source: EntitlementSource
    order_id: uuid.UUID | None = None
    external_ref: str = ""
    can_read: bool = True
    can_download: bool = True
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class AccessOut(BaseSchema):
    """Answer to 'may this user open this book?' — used by the reader."""

    book_id: uuid.UUID
    has_access: bool
    can_download: bool = False
    source: EntitlementSource | None = None
    expires_at: datetime | None = None


class LibraryItem(BaseSchema):
    book: BookListItem
    source: EntitlementSource
    granted_at: datetime
    expires_at: datetime | None = None
    can_download: bool = True
    progress_percent: float = 0.0


class EntitlementGrantIn(BaseSchema):
    """Admin/manual grant. The user id is explicit here *because* the caller is
    staff acting on someone else's behalf; on every user-facing route the user id
    comes from the token instead."""

    user_id: uuid.UUID
    book_id: uuid.UUID
    source: EntitlementSource = EntitlementSource.ADMIN
    external_ref: str = Field(default="", max_length=255)
    expires_at: datetime | None = None
    can_download: bool = True


class DownloadTicket(BaseSchema):
    """A short-lived presigned URL. The file never passes through this service."""

    book_id: uuid.UUID
    format: BookFormat
    url: str
    expires_in: int
    expires_at: datetime
    filename: str


class UploadTargetOut(BaseSchema):
    url: str
    key: str
    fields: dict[str, str] = Field(default_factory=dict)
    expires_in: int
    max_bytes: int


class UploadRequest(BaseSchema):
    category: UploadCategory
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=3, max_length=127)


# ---------------------------------------------------------------------------
# Internal (service-to-service) payloads
# ---------------------------------------------------------------------------


class BatchBooksRequest(BaseSchema):
    book_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    #: Include books that are not published — the search service reindexes drafts
    #: too, so it asks for them explicitly.
    include_unpublished: bool = False


class BatchBooksResponse(BaseSchema):
    items: list[BookDetail]
    missing: list[uuid.UUID] = Field(default_factory=list)


class InternalPublishRequest(BaseSchema):
    #: Automation calls this when a generated book clears its pipeline.
    reason: str | None = Field(default=None, max_length=255)
    published_at: datetime | None = None


# ---------------------------------------------------------------------------
# Catalogue envelope
# ---------------------------------------------------------------------------


class FacetValue(BaseSchema):
    """One row of a filter sidebar: what to send back, what to show, how many."""

    value: str
    label: str
    count: int


class CatalogueFacets(BaseSchema):
    """Counts computed under the *same* filters as the page they accompany."""

    categories: list[FacetValue] = Field(default_factory=list)
    languages: list[FacetValue] = Field(default_factory=list)
    price_ranges: list[FacetValue] = Field(default_factory=list)


class CataloguePage(BaseSchema):
    """Cursor-paginated catalogue feed.

    There is no ``total``: counting a filtered catalogue costs a full scan on every
    page, and infinite scroll never displays the number. Ask for ``include_facets``
    when the UI needs counts, and pay for them once.
    """

    items: list[BookListItem] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    facets: CatalogueFacets | None = None


CategoryNode.model_rebuild()

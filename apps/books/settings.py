"""Books service settings.

Everything the platform guarantees (database, redis, storage, auth, events) comes
from :class:`~knowledgeos_core.ServiceSettings`. Only knobs that are genuinely
specific to the catalogue live here.
"""

from __future__ import annotations

from knowledgeos_core import ServiceSettings


class Settings(ServiceSettings):
    """Configuration for the books service."""

    service_name: str = "books"
    #: THIS SERVICE'S SCHEMA. Never read or write another service's schema.
    database_schema: str = "books"
    port: int = 8002

    # ---- caching --------------------------------------------------------
    # The catalogue is the highest-traffic read surface on the platform, so hot
    # reads are served from Redis and invalidated explicitly on write rather than
    # being left to expire.
    book_cache_ttl: int = 300
    category_tree_cache_ttl: int = 900
    related_books_cache_ttl: int = 600
    #: Set false to bypass the read cache entirely (useful while debugging staleness).
    cache_enabled: bool = True

    # ---- downloads ------------------------------------------------------
    #: Lifetime of a presigned book download URL. The URL *is* the capability, so
    #: it is minted only after an entitlement check and expires in minutes.
    download_url_ttl: int = 300

    # ---- uploads --------------------------------------------------------
    max_book_upload_bytes: int = 200 * 1024 * 1024
    max_cover_upload_bytes: int = 10 * 1024 * 1024

    # ---- catalogue behaviour -------------------------------------------
    #: Page size cap for cursor-paginated feeds.
    max_cursor_limit: int = 100
    default_cursor_limit: int = 20
    related_books_limit: int = 12
    #: When true a new review is held at ``pending`` until a moderator approves it.
    reviews_require_moderation: bool = False
    #: Books priced at zero are readable by anyone signed in, without an
    #: entitlement row. Turn off to require an explicit grant for every book.
    free_books_readable: bool = True

    # ---- events ---------------------------------------------------------
    #: Consume payment events to grant entitlements. Disabled in tests, where the
    #: consumer's blocking XREADGROUP has no fakeredis equivalent.
    events_enabled: bool = True
    event_consumer_group: str = "books"


settings = Settings()

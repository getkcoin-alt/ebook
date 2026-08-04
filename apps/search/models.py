"""SQLAlchemy models for the search service.

Everything lives in the ``search`` schema. **None of it is the search index** —
Meilisearch holds that, and it is disposable. These tables hold the two things the
index cannot: the ledger that makes at-least-once event delivery idempotent, and
the query analytics that tell us whether the index is any good.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from knowledgeos_core import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UUIDType,
)

#: This service's schema. Set explicitly on every table.
SCHEMA = "search"

#: JSON on any backend, JSONB on PostgreSQL. Keeps the models usable on the
#: SQLite the test suite runs against.
JSONType = JSON().with_variant(JSONB, "postgresql")


class IndexedDocument(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per document we believe is in the index.

    This is what makes the event handlers idempotent. Delivery is at-least-once,
    so ``book.published`` for the same book arrives twice more often than anyone
    expects (a consumer restart mid-batch is enough). Before touching Meilisearch
    the indexer checks ``last_event_id``; a replay is a no-op instead of a second
    round trip, and — more importantly — instead of a second *out of order* write
    that could resurrect a document deleted by a later event.

    ``checksum`` covers the rendered document, so a re-delivered ``book.updated``
    carrying identical content is also skipped even when the event id differs.
    """

    __tablename__ = "indexed_documents"
    __table_args__ = (
        # One row per (index, document). The upsert path relies on this.
        UniqueConstraint("index_name", "document_id", name="uq_indexed_documents_index_document"),
        Index("ix_indexed_documents_index_name_indexed_at", "index_name", "indexed_at"),
        Index("ix_indexed_documents_last_event_id", "last_event_id"),
        Index("ix_indexed_documents_deleted_at", "deleted_at"),
        {"schema": SCHEMA},
    )

    index_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The source aggregate's id (a book id, author id, ...). Not a UUID column:
    #: Meilisearch primary keys are strings and other sources may not use UUIDs.
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 of the canonical JSON we last sent.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Id of the event that produced this state, or ``reindex:<run id>``.
    last_event_id: Mapped[str | None] = mapped_column(String(120))
    #: Event type that produced this state, for debugging a divergent index.
    last_event_type: Mapped[str | None] = mapped_column(String(64))
    #: Monotonic counter, bumped on every applied write.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Set when the document was removed from the index. The row is kept so a
    #: redelivered ``book.published`` from before the deletion cannot re-add it.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReindexRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A full rebuild or a reconciliation pass.

    Kept because "is the index stale?" is an operational question that needs an
    answer at 3am, and because a reconciliation that silently half-failed is
    indistinguishable from a healthy one without a record of it.
    """

    __tablename__ = "reindex_runs"
    __table_args__ = (
        Index("ix_reindex_runs_status_started_at", "status", "started_at"),
        Index("ix_reindex_runs_index_name_started_at", "index_name", "started_at"),
        {"schema": SCHEMA},
    )

    index_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: "full" (drop and rebuild) or "reconcile" (diff against the source).
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    #: queued | running | succeeded | failed
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default=text("'queued'")
    )
    triggered_by: Mapped[str | None] = mapped_column(String(120))
    documents_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    documents_indexed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    documents_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    documents_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Operator-facing failure summary. Never contains a key or a URL with one.
    error: Mapped[str | None] = mapped_column(Text)


class SearchQuery(Base, UUIDPrimaryKeyMixin):
    """One executed query.

    Drives two things that pay for the storage: the zero-result report (what
    people look for and do not find is the highest-signal product feedback on the
    platform) and click-through, which is how the ranking rules get tuned.
    """

    __tablename__ = "search_queries"
    __table_args__ = (
        Index("ix_search_queries_created_at", "created_at"),
        Index("ix_search_queries_normalized_created_at", "normalized_query", "created_at"),
        # Partial-index candidate in production; the zero-result report is the
        # only consumer and it always filters on this column.
        Index("ix_search_queries_has_results_created_at", "has_results", "created_at"),
        Index("ix_search_queries_user_id", "user_id"),
        {"schema": SCHEMA},
    )

    query_text: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Lower-cased, whitespace-collapsed. Grouping on the raw text would report
    #: "Machine Learning" and "machine  learning" as different searches.
    normalized_query: Mapped[str] = mapped_column(String(255), nullable=False)
    filters: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    sort: Mapped[str | None] = mapped_column(String(32))
    result_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    has_results: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    took_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: From the access token when present. Anonymous searches are still recorded,
    #: keyed only by the (already anonymous) session id.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType)
    session_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=False
    )


class SearchClick(Base, UUIDPrimaryKeyMixin):
    """A result a user actually opened.

    Position is recorded because click-through only means something relative to
    rank: a click at position 9 says far more about relevance than one at 1.
    """

    __tablename__ = "search_clicks"
    __table_args__ = (
        Index("ix_search_clicks_query_id", "query_id"),
        Index("ix_search_clicks_book_id_created_at", "book_id", "created_at"),
        {"schema": SCHEMA},
    )

    query_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey(f"{SCHEMA}.search_queries.id", ondelete="CASCADE"),
        nullable=False,
    )
    book_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

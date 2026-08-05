"""Writing to the index: events, bulk batches, full rebuilds, reconciliation.

Three properties this module has to guarantee, in order:

**Idempotence.** Redis Streams deliver at least once. A consumer that restarts
mid-batch redelivers everything it had not acknowledged, so a handler that is not
idempotent will double-apply on an ordinary deploy — not just in a disaster. Every
write goes through :class:`IndexLedger`, which records the event id and a checksum
of what was sent; a replay is dropped before it reaches Meilisearch.

**Backpressure.** Meilisearch accepts documents into an internal task queue and
indexes asynchronously. Push 50,000 documents at it as fast as the network allows
and the queue grows without bound, live updates from events queue behind the
rebuild, and search results go stale precisely while you are trying to fix them.
So: fixed batch size, a bounded number of batches in flight, and a small pause
between them.

**Failure isolation.** A reconciliation that cannot reach the books service must
leave the existing index alone. Serving slightly stale results beats serving none.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledgeos_core import Event, EventType, ServiceUnavailableError, get_logger
from models import IndexedDocument, ReindexRun
from services.catalogue import CatalogueGateway
from services.documents import (
    build_author_document,
    build_book_document,
    build_category_document,
    checksum,
)
from services.meili import MeiliClient
from settings import Settings

logger = get_logger(__name__)

#: Events that put a book into the index, and those that take it out.
UPSERT_EVENTS = (EventType.BOOK_PUBLISHED, EventType.BOOK_UPDATED)
REMOVE_EVENTS = (EventType.BOOK_UNPUBLISHED, EventType.BOOK_DELETED)


class IndexLedger:
    """The record of what we believe is in the index, and why.

    Separate from the indexer so the idempotency decision is testable on its own —
    it is the single most important behaviour in this service and the easiest to
    break by accident.
    """

    def __init__(self, index_name: str) -> None:
        self._index = index_name

    async def get(self, session: AsyncSession, document_id: str) -> IndexedDocument | None:
        result = await session.execute(
            select(IndexedDocument).where(
                IndexedDocument.index_name == self._index,
                IndexedDocument.document_id == document_id,
            )
        )
        return result.scalar_one_or_none()

    async def should_apply(
        self,
        session: AsyncSession,
        document_id: str,
        *,
        event_id: str | None,
        content_hash: str | None,
        force: bool = False,
    ) -> bool:
        """Is this write new information?

        ``False`` for a duplicate — which is either the same event id arriving
        twice, or a different event carrying content we have already indexed.
        Both are common; the second is what a books service that publishes
        ``book.updated`` on every save produces.
        """
        if force:
            return True
        existing = await self.get(session, document_id)
        if existing is None:
            return True
        if event_id and existing.last_event_id == event_id:
            logger.info(
                "search.event_duplicate",
                index=self._index,
                document_id=document_id,
                event_id=event_id,
            )
            return False
        if content_hash and existing.checksum == content_hash and existing.deleted_at is None:
            logger.debug("search.document_unchanged", index=self._index, document_id=document_id)
            return False
        return True

    async def record(
        self,
        session: AsyncSession,
        document_id: str,
        *,
        content_hash: str,
        event_id: str | None,
        event_type: str | None,
        deleted: bool = False,
    ) -> None:
        existing = await self.get(session, document_id)
        now = datetime.now(UTC)
        if existing is None:
            session.add(
                IndexedDocument(
                    index_name=self._index,
                    document_id=document_id,
                    checksum=content_hash,
                    last_event_id=event_id,
                    last_event_type=event_type,
                    revision=1,
                    indexed_at=now,
                    deleted_at=now if deleted else None,
                )
            )
            return
        existing.checksum = content_hash
        existing.last_event_id = event_id
        existing.last_event_type = event_type
        existing.revision = existing.revision + 1
        existing.indexed_at = now
        existing.deleted_at = now if deleted else None

    async def mark_missing_deleted(
        self, session: AsyncSession, keep_ids: Sequence[str]
    ) -> list[str]:
        """Documents the ledger knows about that the source no longer has.

        The other half of reconciliation: without it a book deleted while the
        consumer was down stays searchable forever, because no event will ever
        arrive to remove it.
        """
        result = await session.execute(
            select(IndexedDocument.document_id).where(
                IndexedDocument.index_name == self._index,
                IndexedDocument.deleted_at.is_(None),
            )
        )
        known = set(result.scalars().all())
        stale = sorted(known - set(keep_ids))
        if stale:
            await session.execute(
                update(IndexedDocument)
                .where(
                    IndexedDocument.index_name == self._index,
                    IndexedDocument.document_id.in_(stale),
                )
                .values(deleted_at=datetime.now(UTC))
            )
        return stale


class Indexer:
    """Applies changes to one Meilisearch index, in batches, idempotently."""

    def __init__(
        self,
        settings: Settings,
        meili: MeiliClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        catalogue: CatalogueGateway | None = None,
    ) -> None:
        self._settings = settings
        self._meili = meili
        self._sessionmaker = sessionmaker
        self._catalogue = catalogue

    # ---- index setup -----------------------------------------------------

    async def ensure_indexes(self) -> None:
        """Create the indexes and push their configuration.

        Idempotent, and best-effort: a search engine that is down at boot must
        not stop the service from starting. Readiness will report it, the query
        path will 503 cleanly, and the next reindex applies the settings.
        """
        from services.indexes import PRIMARY_KEY, settings_for, with_embedder

        for name in self._settings.index_names:
            try:
                await self._meili.create_index(name, primary_key=PRIMARY_KEY)
                config = settings_for(
                    name,
                    books=self._settings.books_index,
                    authors=self._settings.authors_index,
                    categories=self._settings.categories_index,
                )
                if self._settings.semantic_enabled and name == self._settings.books_index:
                    config = with_embedder(
                        config,
                        name=self._settings.semantic_embedder,
                        dimensions=self._settings.embedding_dimensions,
                    )
                await self._meili.update_settings(name, config)
                logger.info("search.index_configured", index=name)
            except ServiceUnavailableError:
                logger.warning("search.index_setup_deferred", index=name)
                return
            except Exception as exc:
                logger.warning("search.index_setup_failed", index=name, error=str(exc))

    # ---- event handling --------------------------------------------------

    async def handle_book_event(self, event: Event) -> str:
        """Apply one ``book.*`` event. Returns what it did, for the logs.

        MUST be idempotent — this is the contract for every consumer on the
        platform (ADR 0004), and the reason for the ledger check below.
        """
        book_id = str(event.payload.get("book_id") or event.payload.get("id") or "")
        if not book_id:
            logger.warning("search.event_missing_book_id", event_type=event.type)
            return "ignored"

        index = self._settings.books_index
        ledger = IndexLedger(index)

        async with self._sessionmaker() as session:
            if event.type in REMOVE_EVENTS:
                if not await ledger.should_apply(
                    session, book_id, event_id=event.id, content_hash=None
                ):
                    return "duplicate"
                await self._meili.delete_documents(index, [book_id])
                await ledger.record(
                    session,
                    book_id,
                    content_hash="",
                    event_id=event.id,
                    event_type=event.type,
                    deleted=True,
                )
                await session.commit()
                logger.info("search.document_removed", book_id=book_id, event_type=event.type)
                return "removed"

            if event.type not in UPSERT_EVENTS:
                return "ignored"

            # The event carries an id, not a document. Read the current record —
            # which also means a burst of updates converges on the latest state
            # rather than replaying a stale payload from the stream.
            book = await self._load_book(book_id, event.payload)
            if book is None:
                # Published-then-deleted before we got here. Make the index agree
                # with reality rather than leaving a phantom hit.
                await self._meili.delete_documents(index, [book_id])
                await ledger.record(
                    session,
                    book_id,
                    content_hash="",
                    event_id=event.id,
                    event_type=event.type,
                    deleted=True,
                )
                await session.commit()
                return "removed"

            document = build_book_document(book)
            content_hash = checksum(document)
            if not await ledger.should_apply(
                session, book_id, event_id=event.id, content_hash=content_hash
            ):
                return "duplicate"

            await self._meili.add_documents(index, [document])
            await ledger.record(
                session,
                book_id,
                content_hash=content_hash,
                event_id=event.id,
                event_type=event.type,
            )
            await session.commit()
            logger.info("search.document_indexed", book_id=book_id, event_type=event.type)
            return "indexed"

    async def _load_book(self, book_id: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Fetch the book, falling back to the event payload.

        The fallback matters: if the books service is briefly unreachable but the
        event happens to carry a complete record (the automation pipeline
        publishes one), indexing it is better than dead-lettering.
        """
        if self._catalogue is not None:
            with contextlib.suppress(Exception):
                book = await self._catalogue.get_book(book_id)
                if book is not None:
                    return book
        if payload.get("title") and payload.get("slug"):
            return dict(payload)
        if self._catalogue is None:
            return None
        # Re-raise through the catalogue so a genuine outage retries via the
        # consumer's redelivery rather than silently dropping the update.
        return await self._catalogue.get_book(book_id)

    # ---- bulk writes -----------------------------------------------------

    async def index_documents(
        self,
        index: str,
        documents: Sequence[Mapping[str, Any]],
        *,
        event_id: str | None = None,
        event_type: str | None = None,
        force: bool = False,
    ) -> tuple[int, int, int]:
        """Send documents in batches. Returns ``(indexed, skipped, failed)``.

        Backpressure lives here: ``index_batch_size`` documents per request, at
        most ``index_max_concurrent_batches`` requests in flight, and a short
        pause between waves so Meilisearch's task queue can drain.
        """
        ledger = IndexLedger(index)
        pending: list[Mapping[str, Any]] = []
        skipped = 0

        async with self._sessionmaker() as session:
            for document in documents:
                document_id = str(document.get("id", ""))
                if not document_id:
                    continue
                content_hash = checksum(document)
                if not await ledger.should_apply(
                    session,
                    document_id,
                    event_id=event_id,
                    content_hash=content_hash,
                    force=force,
                ):
                    skipped += 1
                    continue
                pending.append(document)
            # Commit nothing yet: the ledger is only truthful once the documents
            # are actually accepted by the engine.
            await session.rollback()

        if not pending:
            return 0, skipped, 0

        accepted, failed = await self._push_batches(index, pending)

        if accepted:
            async with self._sessionmaker() as session:
                # Only documents the engine actually accepted are recorded. A
                # ledger entry for a batch that failed would make the next run
                # skip it as "already indexed" — the index would stay wrong and
                # nothing would ever notice.
                for document in accepted:
                    await ledger.record(
                        session,
                        str(document["id"]),
                        content_hash=checksum(document),
                        event_id=event_id,
                        event_type=event_type,
                    )
                await session.commit()
        return len(accepted), skipped, failed

    async def _push_batches(
        self, index: str, documents: Sequence[Mapping[str, Any]]
    ) -> tuple[list[Mapping[str, Any]], int]:
        batch_size = max(1, self._settings.index_batch_size)
        batches = [
            list(documents[start : start + batch_size])
            for start in range(0, len(documents), batch_size)
        ]
        semaphore = asyncio.Semaphore(max(1, self._settings.index_max_concurrent_batches))
        accepted: list[Mapping[str, Any]] = []
        failed = 0

        async def send(batch: list[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], int]:
            async with semaphore:
                try:
                    await self._meili.add_documents(index, [dict(doc) for doc in batch])
                except ServiceUnavailableError:
                    # Bubble out: a rebuild against a dead engine should stop,
                    # not spend ten minutes failing one batch at a time.
                    raise
                except Exception as exc:
                    logger.warning(
                        "search.batch_failed", index=index, size=len(batch), error=str(exc)
                    )
                    return [], len(batch)
                if self._settings.index_batch_pause_seconds > 0:
                    # Lets the engine's task queue drain between waves. Without
                    # it a rebuild starves the live event stream of throughput.
                    await asyncio.sleep(self._settings.index_batch_pause_seconds)
                return list(batch), 0

        for batch_accepted, batch_failed in await asyncio.gather(
            *(send(batch) for batch in batches)
        ):
            accepted.extend(batch_accepted)
            failed += batch_failed
        return accepted, failed

    async def delete_documents(self, index: str, document_ids: Sequence[str]) -> int:
        if not document_ids:
            return 0
        await self._meili.delete_documents(index, list(document_ids))
        async with self._sessionmaker() as session:
            ledger = IndexLedger(index)
            for document_id in document_ids:
                await ledger.record(
                    session,
                    document_id,
                    content_hash="",
                    event_id=None,
                    event_type="manual.delete",
                    deleted=True,
                )
            await session.commit()
        return len(document_ids)

    async def index_book_ids(self, book_ids: Sequence[str]) -> tuple[int, int]:
        """Index specific books by id, reading each from the catalogue.

        What the automation pipeline calls when a book finishes processing. It could
        wait for `book.published` to arrive through the event bus — and it does, this
        is belt as well as braces — but the pipeline knows the exact moment the
        record became correct, and a book that is live in the catalogue while absent
        from search reads to a customer as a book that does not exist.

        By id rather than by document: building a search document is this service's
        job, and a caller that constructs one is a caller that has to be redeployed
        every time the mapping changes.

        Returns ``(indexed, missing)``. Never raises for a book that is simply gone —
        that is removed from the index, which is the correct state, not an error.
        """
        index = self._settings.books_index
        ledger = IndexLedger(index)
        indexed = missing = 0

        async with self._sessionmaker() as session:
            for book_id in book_ids:
                book = await self._load_book(book_id, {})
                if book is None:
                    await self._meili.delete_documents(index, [book_id])
                    await ledger.record(
                        session,
                        book_id,
                        content_hash="",
                        event_id=None,
                        event_type="automation.index",
                        deleted=True,
                    )
                    missing += 1
                    continue

                document = build_book_document(book)
                content_hash = checksum(document)
                # Not gated on `should_apply`: the caller is asserting the record
                # just changed, and an unchanged checksum here means the write it is
                # confirming has not propagated yet. Skipping would leave the index
                # stale precisely when someone asked it not to be.
                await self._meili.add_documents(index, [document])
                await ledger.record(
                    session,
                    book_id,
                    content_hash=content_hash,
                    event_id=None,
                    event_type="automation.index",
                )
                indexed += 1
            await session.commit()

        logger.info("search.books_indexed_by_id", indexed=indexed, missing=missing)
        return indexed, missing

    # ---- rebuilds --------------------------------------------------------

    async def run(
        self,
        *,
        index: str,
        mode: str,
        triggered_by: str,
        force: bool = False,
    ) -> ReindexRun:
        """Rebuild or reconcile one index, recording the outcome.

        ``full`` empties the index first; ``reconcile`` diffs against the source
        and repairs only the difference, which is cheap enough to run hourly.
        """
        run = ReindexRun(
            index_name=index,
            mode=mode,
            status="running",
            triggered_by=triggered_by[:120],
            started_at=datetime.now(UTC),
        )
        async with self._sessionmaker() as session:
            session.add(run)
            await session.commit()
            run_id = run.id

        try:
            seen, indexed, deleted, failed = await self._execute(index, mode, force=force)
            status, error = "succeeded", None
        except ServiceUnavailableError as exc:
            seen = indexed = deleted = failed = 0
            status, error = "failed", exc.message
            logger.warning("search.reindex_unavailable", index=index)
        except Exception as exc:
            seen = indexed = deleted = failed = 0
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            logger.exception("search.reindex_failed", index=index)

        async with self._sessionmaker() as session:
            stored = await session.get(ReindexRun, run_id)
            if stored is not None:
                stored.status = status
                stored.documents_seen = seen
                stored.documents_indexed = indexed
                stored.documents_deleted = deleted
                stored.documents_failed = failed
                stored.finished_at = datetime.now(UTC)
                stored.error = error
                await session.commit()
                await session.refresh(stored)
                session.expunge(stored)
                return stored
        return run

    async def _execute(self, index: str, mode: str, *, force: bool) -> tuple[int, int, int, int]:
        if self._catalogue is None:
            raise ServiceUnavailableError(
                "The catalogue service is not configured for this instance.",
                code="catalogue_unavailable",
            )

        pages = self._pages_for(index)
        if mode == "full":
            # Emptied first so a book that vanished from the source cannot
            # survive the rebuild. The window where the index is empty is why
            # `full` is an operator action and `reconcile` is the scheduled one.
            await self._meili.delete_all_documents(index)
            async with self._sessionmaker() as session:
                await session.execute(
                    update(IndexedDocument)
                    .where(IndexedDocument.index_name == index)
                    .values(deleted_at=datetime.now(UTC))
                )
                await session.commit()

        seen = indexed = failed = 0
        keep_ids: list[str] = []
        builder = self._builder_for(index)

        async for page in pages:
            documents = [builder(record) for record in page]
            documents = [doc for doc in documents if doc.get("id")]
            seen += len(documents)
            keep_ids.extend(str(doc["id"]) for doc in documents)
            added, _skipped, batch_failed = await self.index_documents(
                index,
                documents,
                event_id=None,
                event_type=f"{mode}.run",
                force=force or mode == "full",
            )
            indexed += added
            failed += batch_failed

        deleted = 0
        if mode == "reconcile" and keep_ids:
            async with self._sessionmaker() as session:
                ledger = IndexLedger(index)
                stale = await ledger.mark_missing_deleted(session, keep_ids)
                if stale:
                    await self._meili.delete_documents(index, stale)
                    deleted = len(stale)
                await session.commit()
        return seen, indexed, deleted, failed

    def _pages_for(self, index: str) -> Any:
        assert self._catalogue is not None
        limit = self._settings.reconcile_max_documents
        if index == self._settings.books_index:
            return self._catalogue.iter_books(max_documents=limit)
        if index == self._settings.authors_index:
            return self._catalogue.iter_authors(max_documents=limit)
        return self._catalogue.iter_categories(max_documents=limit)

    def _builder_for(self, index: str) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        if index == self._settings.books_index:
            return build_book_document
        if index == self._settings.authors_index:
            return build_author_document
        return build_category_document


def register_handlers(
    consumer: Any,
    indexer: Indexer,
) -> Callable[[Event], Awaitable[None]]:
    """Subscribe the indexer to every book event, on one handler.

    One function for all four event types keeps the idempotency check in exactly
    one place — a per-event-type handler is how a subtly non-idempotent branch
    gets added six months from now.
    """

    async def handle(event: Event) -> None:
        outcome = await indexer.handle_book_event(event)
        logger.debug("search.event_handled", event_type=event.type, outcome=outcome)

    for event_type in (*UPSERT_EVENTS, *REMOVE_EVENTS):
        consumer.on(event_type)(handle)
    return handle

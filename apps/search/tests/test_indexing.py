"""Indexing: document mapping, idempotency, reconciliation and analytics.

The index is a cache, so the property that matters is **convergence**: whatever
sequence of events arrives, in whatever order, however many times, the index ends up
matching the source. The ledger is what makes that cheap; the unique document id is
what makes it correct.
"""

from __future__ import annotations

import uuid

import pytest

from knowledgeos_core import Event, EventType
from models import IndexedDocument, SearchQuery
from services import build_book_document, checksum, price_bucket
from tests.conftest import catalogue_book

# ---------------------------------------------------------------------------
# Document mapping (pure)
# ---------------------------------------------------------------------------


def test_a_catalogue_record_maps_onto_an_index_document():
    book = catalogue_book(title="Deep Work", slug="deep-work")
    document = build_book_document(book)

    assert document["id"] == book["id"]
    assert document["title"] == "Deep Work"
    assert document["slug"] == "deep-work"
    assert document["author_slugs"] == ["jane-doe"]
    assert document["category_slugs"] == ["fiction"]
    assert "pdf" in document["formats"]


def test_the_document_carries_a_sortable_timestamp():
    """Meilisearch cannot sort an ISO string chronologically; it needs a number."""
    document = build_book_document(catalogue_book(published_at="2026-01-01T00:00:00Z"))
    assert isinstance(document["published_at_ts"], int)
    assert document["published_at_ts"] > 0


def test_a_missing_publication_date_does_not_break_the_mapping():
    document = build_book_document(catalogue_book(published_at=None))
    assert document["published_at_ts"] is None


def test_free_books_are_flagged_and_bucketed():
    document = build_book_document(catalogue_book(effective_price_minor=0, price_minor=0))
    assert document["is_free"] is True
    assert document["price_bucket"] == "free"


def test_the_discounted_price_is_what_gets_indexed():
    """Indexing the list price instead makes every sale invisible to the price
    filter and the price facet."""
    document = build_book_document(catalogue_book(price_minor=99_900, effective_price_minor=49_900))
    assert document["price_minor"] == 49_900


def test_a_publisher_object_is_flattened_to_its_name():
    """The books service sends an object; the index facets on a single string."""
    document = build_book_document(
        catalogue_book(publisher={"id": "p1", "slug": "kos", "name": "KnowledgeOS Press"})
    )
    assert document["publisher"] == "KnowledgeOS Press"


def test_price_buckets_are_a_closed_set():
    """Facet values must be stable strings, not derived numbers — a bucket that
    changes shape breaks every saved filter URL."""
    from services.documents import PRICE_BUCKET_LABELS

    buckets = {price_bucket(price) for price in (0, 5_000, 30_000, 90_000, 500_000)}
    assert buckets == set(PRICE_BUCKET_LABELS)


def test_every_price_bucket_has_a_display_label():
    """A facet value with no label renders as a raw slug in the sidebar."""
    from services.documents import PRICE_BUCKET_LABELS, PRICE_BUCKETS

    assert {label for label, _low, _high in PRICE_BUCKETS} == set(PRICE_BUCKET_LABELS)


def test_the_checksum_ignores_key_order():
    """It has to, or every reindex would rewrite every document."""
    assert checksum({"a": 1, "b": 2}) == checksum({"b": 2, "a": 1})


def test_the_checksum_changes_when_content_changes():
    book = catalogue_book()
    before = checksum(build_book_document(book))
    after = checksum(build_book_document({**book, "title": "Something Else"}))
    assert before != after


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


async def test_documents_reach_the_engine(settings, services, meili_engine):
    book = build_book_document(catalogue_book())
    indexed, _skipped, failed = await services["indexer"].index_documents(
        settings.books_index, [book]
    )

    assert (indexed, failed) == (1, 0)
    assert book["id"] in meili_engine.indexes[settings.books_index]


async def test_reindexing_unchanged_documents_skips_them(settings, services, meili_engine):
    """The ledger is what makes an hourly reconcile cheap."""
    book = build_book_document(catalogue_book())
    await services["indexer"].index_documents(settings.books_index, [book])
    meili_engine.requests.clear()

    indexed, skipped, _failed = await services["indexer"].index_documents(
        settings.books_index, [book]
    )
    assert (indexed, skipped) == (0, 1)
    assert meili_engine.bodies("/documents") == []  # nothing was sent


async def test_a_changed_document_is_sent_again(settings, services, meili_engine):
    book = catalogue_book()
    await services["indexer"].index_documents(settings.books_index, [build_book_document(book)])
    indexed, skipped, _failed = await services["indexer"].index_documents(
        settings.books_index, [build_book_document({**book, "title": "Renamed"})]
    )
    assert (indexed, skipped) == (1, 0)
    assert meili_engine.indexes[settings.books_index][book["id"]]["title"] == "Renamed"


async def test_force_resends_even_unchanged_documents(settings, services):
    """The escape hatch for when the index and the ledger disagree."""
    book = build_book_document(catalogue_book())
    await services["indexer"].index_documents(settings.books_index, [book])
    indexed, skipped, _failed = await services["indexer"].index_documents(
        settings.books_index, [book], force=True
    )
    assert (indexed, skipped) == (1, 0)


async def test_a_large_batch_is_split(settings, services, meili_engine, monkeypatch):
    """50k documents in one body is a request that times out, retries, and times
    out again."""
    monkeypatch.setattr(settings, "index_batch_size", 2)
    books = [build_book_document(catalogue_book(slug=f"b-{n}")) for n in range(5)]

    indexed, _skipped, _failed = await services["indexer"].index_documents(
        settings.books_index, books
    )
    assert indexed == 5
    assert len(meili_engine.bodies("/documents")) == 3  # 2 + 2 + 1


async def test_deleting_documents_removes_them(settings, services, meili_engine):
    book = build_book_document(catalogue_book())
    await services["indexer"].index_documents(settings.books_index, [book])

    removed = await services["indexer"].delete_documents(settings.books_index, [book["id"]])
    assert removed == 1
    assert book["id"] not in meili_engine.indexes[settings.books_index]


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------


async def test_a_publish_event_indexes_the_book(settings, services, catalogue, meili_engine):
    book = catalogue_book()
    catalogue.books.append(book)

    outcome = await services["indexer"].handle_book_event(
        Event(type=EventType.BOOK_PUBLISHED, payload={"book_id": book["id"]})
    )
    assert outcome in ("indexed", "upserted")
    assert book["id"] in meili_engine.indexes[settings.books_index]


async def test_a_delete_event_removes_the_book(settings, services, catalogue, meili_engine):
    book = catalogue_book()
    catalogue.books.append(book)
    await services["indexer"].handle_book_event(
        Event(type=EventType.BOOK_PUBLISHED, payload={"book_id": book["id"]})
    )

    await services["indexer"].handle_book_event(
        Event(type=EventType.BOOK_DELETED, payload={"book_id": book["id"]})
    )
    assert book["id"] not in meili_engine.indexes[settings.books_index]


async def test_a_redelivered_event_is_a_no_op(settings, services, catalogue, meili_engine):
    """Redis Streams deliver at least once; every handler must be idempotent."""
    book = catalogue_book()
    catalogue.books.append(book)
    event = Event(type=EventType.BOOK_PUBLISHED, payload={"book_id": book["id"]})

    await services["indexer"].handle_book_event(event)
    meili_engine.requests.clear()
    await services["indexer"].handle_book_event(event)

    assert meili_engine.bodies("/documents") == []


async def test_an_event_for_a_deleted_book_removes_it_rather_than_failing(
    settings, services, catalogue, meili_engine
):
    """The race the reconciler exists to resolve: the book is gone by the time the
    event is handled. It must not dead-letter."""
    book = catalogue_book()
    catalogue.books.append(book)
    await services["indexer"].handle_book_event(
        Event(type=EventType.BOOK_PUBLISHED, payload={"book_id": book["id"]})
    )

    catalogue.books.clear()  # deleted between publish and the update event
    outcome = await services["indexer"].handle_book_event(
        Event(type=EventType.BOOK_UPDATED, payload={"book_id": book["id"]})
    )
    assert outcome in ("deleted", "missing", "removed")
    assert book["id"] not in meili_engine.indexes[settings.books_index]


async def test_an_event_without_a_book_id_is_ignored(services):
    outcome = await services["indexer"].handle_book_event(
        Event(type=EventType.BOOK_UPDATED, payload={})
    )
    assert outcome in ("ignored", "skipped")


# ---------------------------------------------------------------------------
# Reindex runs
# ---------------------------------------------------------------------------


async def test_a_reconcile_run_indexes_the_catalogue(
    settings, services, catalogue, meili_engine, session
):
    catalogue.books.extend(catalogue_book(slug=f"book-{n}") for n in range(3))

    run = await services["indexer"].run(
        index=settings.books_index, mode="reconcile", triggered_by="test"
    )
    assert run.status == "succeeded"
    assert run.documents_indexed == 3
    assert len(meili_engine.indexes[settings.books_index]) == 3


async def test_a_run_is_recorded_even_when_it_fails(settings, services, meili_engine):
    """An operator needs to see the failure; a silent one is unactionable."""
    meili_engine.healthy = False
    run = await services["indexer"].run(
        index=settings.books_index, mode="full", triggered_by="test"
    )
    assert run.status == "failed"
    assert run.error


async def test_the_ledger_records_what_was_indexed(settings, services, catalogue, session):
    from sqlalchemy import select

    catalogue.books.append(catalogue_book())
    await services["indexer"].run(index=settings.books_index, mode="reconcile", triggered_by="test")

    rows = (await session.execute(select(IndexedDocument))).scalars().all()
    assert len(rows) == 1
    assert rows[0].checksum


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


async def test_a_recorded_query_is_normalised(services, session, engine):
    from sqlalchemy import select

    query_id = await services["analytics"].record_query(
        services["_sessionmaker"],
        query="  Machine   LEARNING ",
        filters={},
        sort="relevance",
        result_count=5,
        took_ms=12,
    )
    assert query_id is not None

    row = (await session.execute(select(SearchQuery))).scalars().one()
    assert row.normalized_query == "machine learning"
    assert row.query_text == "Machine   LEARNING"
    assert row.has_results is True


async def test_a_zero_result_search_is_flagged(services, session):
    from sqlalchemy import select

    await services["analytics"].record_query(
        services["_sessionmaker"],
        query="nonexistent",
        filters={},
        sort=None,
        result_count=0,
        took_ms=3,
    )
    row = (await session.execute(select(SearchQuery))).scalars().one()
    assert row.has_results is False


async def test_an_empty_query_is_not_recorded(services, session):
    """A blank search is a browse, and it would swamp the top-queries report."""
    from sqlalchemy import func, select

    query_id = await services["analytics"].record_query(
        services["_sessionmaker"], query="   ", filters={}, sort=None, result_count=0, took_ms=0
    )
    assert query_id is None
    count = (await session.execute(select(func.count(SearchQuery.id)))).scalar_one()
    assert count == 0


async def test_recording_never_raises(services, monkeypatch):
    """A search that returned good results must not 500 because analytics failed."""

    def _explode(*args, **kwargs):
        raise RuntimeError("database on fire")

    query_id = await services["analytics"].record_query(
        _explode, query="anything", filters={}, sort=None, result_count=1, took_ms=1
    )
    assert query_id is None


async def test_the_report_separates_zero_result_queries(services, session):
    for query, count in [("found", 5), ("found", 5), ("missing", 0)]:
        await services["analytics"].record_query(
            services["_sessionmaker"],
            query=query,
            filters={},
            sort=None,
            result_count=count,
            took_ms=10,
        )

    report = await services["analytics"].report(session, days=7)
    assert report.total_searches == 3
    assert report.unique_queries == 2
    assert report.zero_result_searches == 1
    assert round(report.zero_result_rate, 3) == round(1 / 3, 3)
    assert [stat.query for stat in report.zero_result_queries] == ["missing"]


async def test_the_report_on_an_empty_window_returns_zeroes(services, session):
    """AVG and SUM over an empty set are NULL, which would propagate as nulls."""
    report = await services["analytics"].report(session, days=7)
    assert report.total_searches == 0
    assert report.zero_result_rate == 0.0
    assert report.average_took_ms == 0.0


async def test_a_click_must_reference_a_real_search(services, session):
    from knowledgeos_core import NotFoundError

    with pytest.raises(NotFoundError):
        await services["analytics"].record_click(
            session, query_id=uuid.uuid4(), book_id="abc", position=1
        )

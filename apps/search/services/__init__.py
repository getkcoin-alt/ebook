"""Business logic for the search service.

The layering is worth stating, because it is what lets the query path stay fast and
the index path stay safe:

* ``meili`` is the only thing that speaks HTTP to Meilisearch.
* ``documents`` turns a catalogue record into an index document — pure functions, no
  I/O, so the mapping is testable without an engine.
* ``indexes`` holds the index configuration (searchable fields, ranking rules,
  synonyms, filterable attributes). Changing a filter here without adding it to the
  filterable list is the classic way to ship a search box that 400s.
* ``indexer`` applies changes idempotently, with a ledger so a redelivered event is
  a no-op.
* ``query`` builds and runs searches; ``trending`` and ``analytics`` observe them.
"""

from __future__ import annotations

from services.analytics import AnalyticsService
from services.catalogue import CatalogueGateway
from services.documents import (
    build_author_document,
    build_book_document,
    build_category_document,
    checksum,
    price_bucket,
)
from services.indexer import Indexer, IndexLedger, register_handlers
from services.indexes import PRIMARY_KEY, settings_for, with_embedder
from services.meili import MeiliClient
from services.query import (
    KeywordBackend,
    QueryService,
    SearchBackend,
    SearchParams,
    assert_filters_are_indexable,
    escape_filter_value,
    normalise_query,
)
from services.trending import TrendingService

__all__ = [
    "PRIMARY_KEY",
    "AnalyticsService",
    "CatalogueGateway",
    "IndexLedger",
    "Indexer",
    "KeywordBackend",
    "MeiliClient",
    "QueryService",
    "SearchBackend",
    "SearchParams",
    "TrendingService",
    "assert_filters_are_indexable",
    "build_author_document",
    "build_book_document",
    "build_category_document",
    "checksum",
    "escape_filter_value",
    "normalise_query",
    "price_bucket",
    "register_handlers",
    "settings_for",
    "with_embedder",
]

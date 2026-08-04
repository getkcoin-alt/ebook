"""Configuration for the search service.

Two ideas drive everything here.

**The index is a cache, not a source of truth.** Every document in Meilisearch can
be rebuilt from the books service, so the settings that matter most are the ones
governing how fast we can rebuild it (batch size, concurrency) and how quickly we
give up when it is unreachable (timeouts).

**A search outage must never become a platform outage.** Timeouts are deliberately
short and explicit: a hung request to Meilisearch would hold a worker slot open and
turn "search is slow" into "the gateway's connection pool is exhausted".
"""

from __future__ import annotations

from pydantic import Field, computed_field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    service_name: str = "search"
    database_schema: str = "search"
    port: int = 8003

    jwks_url: str | None = "http://localhost:8001/.well-known/jwks.json"

    # ---- meilisearch -----------------------------------------------------
    meilisearch_url: str = "http://localhost:7700"
    #: Master key, or an API key with document + settings write scope.
    meilisearch_master_key: str | None = None
    #: Read timeout for a query. Kept under a second because this budget sits on
    #: the user's critical path; a slow search is worse than no search.
    meilisearch_timeout: float = 3.0
    meilisearch_connect_timeout: float = 2.0
    #: Separate, far longer budget for indexing calls, which are batched and run
    #: off the request path.
    meilisearch_index_timeout: float = 30.0
    #: Prefix for index names, so staging and production can share one instance.
    index_prefix: str = "kos"

    # ---- indexing --------------------------------------------------------
    #: Documents per Meilisearch request. 50k documents in one body is a request
    #: that times out, retries, and times out again.
    index_batch_size: int = 500
    #: Batches in flight at once. Backpressure: Meilisearch queues tasks and a
    #: reindex that saturates the queue starves live updates.
    index_max_concurrent_batches: int = 2
    #: Pause between batches, giving the engine time to drain its task queue.
    index_batch_pause_seconds: float = 0.05
    #: Books pulled per page when reconciling against the books service.
    reconcile_page_size: int = 200
    #: Ceiling on documents pulled in one reconciliation run.
    reconcile_max_documents: int = 100_000
    #: How long the reindex lock is held, so two replicas cannot both rebuild.
    reindex_lock_seconds: int = 1800

    # ---- query -----------------------------------------------------------
    search_default_limit: int = 20
    search_max_limit: int = 100
    autocomplete_limit: int = 8
    #: Autocomplete runs on every keystroke, so it gets its own tighter budget.
    autocomplete_timeout: float = 1.0
    related_limit: int = 12
    highlight_pre_tag: str = "<mark>"
    highlight_post_tag: str = "</mark>"
    #: Facets returned with a query. Bounded on purpose — every facet is work.
    facet_attributes: CsvList = Field(
        default_factory=lambda: [
            "category_slugs",
            "author_slugs",
            "formats",
            "tags",
            "language",
            "price_bucket",
        ]
    )
    facet_max_values: int = 20

    # ---- trending --------------------------------------------------------
    trending_enabled: bool = True
    #: Hourly buckets retained. 24 buckets = "trending today".
    trending_window_hours: int = 24
    #: Weight multiplier applied per hour of age. 0.9 ** 24 ~= 0.08, so a query
    #: from a day ago counts for a twelfth of one from this hour.
    trending_decay_per_hour: float = 0.9
    trending_max_terms: int = 20
    #: Queries shorter than this are ignored: single letters are keystrokes, not
    #: intent, and they would dominate the ranking.
    trending_min_query_length: int = 3

    # ---- analytics -------------------------------------------------------
    analytics_enabled: bool = True
    #: Queries longer than this are truncated before storage.
    analytics_max_query_length: int = 200

    # ---- semantic search (opt-in; see README) ---------------------------
    #: Master switch. Off by default: the service is fully functional without a
    #: single embedding, and turning this on requires an embedder to be reachable.
    semantic_enabled: bool = False
    #: Name of the embedder registered with Meilisearch (its `embedders` setting).
    semantic_embedder: str = "default"
    #: 0.0 = pure keyword, 1.0 = pure vector. 0.5 is a balanced hybrid.
    semantic_ratio: float = 0.5
    #: Backend that produces vectors. "none" | "ai-service".
    embedding_backend: str = "none"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    #: Texts per embedding request when backfilling vectors.
    embedding_batch_size: int = 64

    # ---- events ----------------------------------------------------------
    events_enabled: bool = True
    event_consumer_group: str = "search"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def books_index(self) -> str:
        return f"{self.index_prefix}_books"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def authors_index(self) -> str:
        return f"{self.index_prefix}_authors"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def categories_index(self) -> str:
        return f"{self.index_prefix}_categories"

    @property
    def index_names(self) -> tuple[str, str, str]:
        return (self.books_index, self.authors_index, self.categories_index)


settings = Settings()

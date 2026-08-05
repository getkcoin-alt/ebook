"""Configuration for the automation service.

Three settings decide whether an ingestion run is safe.

``max_source_bytes`` is the outer bound on everything downstream. Every later stage
loads the file into memory to inspect or transform it, so an unbounded source is an
unbounded worker RSS — and the worker that OOMs takes every other job on it down too.

``max_decompressed_bytes`` bounds an EPUB *after* unzipping. A 2MB archive that
expands to 40GB is a zip bomb, and the size check on the upload does not see it.

``ai_stages_optional`` is the difference between a pipeline that finishes and one
that stalls. AI stages are enrichment: a book with a human-written description and no
AI tags is publishable. Blocking publication on a provider outage means one API's bad
afternoon becomes a backlog nobody can clear.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    service_name: str = "automation"
    database_schema: str = "automation"
    port: int = 8007

    jwks_url: str | None = "http://localhost:8001/.well-known/jwks.json"

    # ---- source limits ---------------------------------------------------
    #: Hard ceiling on a source file. Checked against the object's real size before
    #: it is read, not against what the uploader claimed.
    max_source_bytes: int = 500 * 1024 * 1024
    #: An EPUB is a zip. This bounds what it may expand to, because the archive's own
    #: size says nothing about that.
    max_decompressed_bytes: int = 1024 * 1024 * 1024
    #: Refuse absurd archives before unpacking them at all.
    max_archive_entries: int = 10_000
    #: Formats the pipeline knows how to process. Anything else is rejected at
    #: validation rather than discovered halfway through conversion.
    accepted_formats: CsvList = Field(default_factory=lambda: ["pdf", "epub"])

    # ---- text extraction -------------------------------------------------
    #: How much text is pulled out for the AI stages. Enough to characterise a book;
    #: far less than the whole thing, which nothing downstream needs and every
    #: provider charges for.
    excerpt_chars: int = 12_000
    #: Pages read when building the excerpt. A 900-page reference book does not need
    #: 900 pages parsed to produce a description.
    excerpt_max_pages: int = 40

    # ---- derived artefacts ----------------------------------------------
    thumbnail_width: int = 400
    thumbnail_height: int = 600
    cover_width: int = 1000
    cover_height: int = 1500
    #: JPEG quality for derived images. 82 is the usual knee — visually clean, and
    #: roughly half the bytes of 95.
    image_quality: int = 82
    #: Skip compression when it would save less than this fraction. Rewriting a
    #: 200MB PDF to save 1% burns CPU and risks a worse file than the original.
    min_compression_gain: float = 0.05
    watermark_enabled: bool = True
    watermark_text: str = "KnowledgeOS"

    # ---- pipeline behaviour ---------------------------------------------
    #: Enrichment stages do not block publication.
    ai_stages_optional: bool = True
    #: Attempts per job before dead-lettering, counted across resumes.
    max_attempts: int = 5
    #: Backoff base in seconds; the runner applies exponential growth with jitter.
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3_600
    #: A job with no heartbeat for this long is presumed abandoned (worker killed
    #: between the ack and the checkpoint) and is requeued by the sweep.
    stale_job_seconds: int = 3_600
    #: How long finished jobs are kept. They are the audit trail for "why does this
    #: book have this description", which outlives any individual question about it.
    job_retention_days: int = 90

    #: Run the pipeline in-process instead of dispatching to Celery. True in local
    #: development and in tests; false wherever a worker fleet exists.
    inline_execution: bool = False

    # ---- sibling services ------------------------------------------------
    ai_enabled: bool = True
    search_indexing_enabled: bool = True
    #: Publish automatically when every required stage succeeds. Off means the book
    #: lands in `pending_review` for a human, which is what a store that cares about
    #: its catalogue actually wants.
    auto_publish: bool = False

    # ---- events ----------------------------------------------------------
    events_enabled: bool = True
    event_consumer_group: str = "automation"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accepted_format_set(self) -> list[str]:
        return [fmt.lower().lstrip(".") for fmt in self.accepted_formats]


settings = Settings()

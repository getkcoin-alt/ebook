"""Request and response schemas for the automation service."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from knowledgeos_core import BaseSchema, JobStatus


class Stage(StrEnum):
    """The pipeline, in order.

    The order is the contract. `PIPELINE` below is the single source of truth for
    it — a second ordered list somewhere else is how a stage ends up running before
    the one that produces its input.
    """

    COLLECT = "collect"
    VALIDATE = "validate"
    EXTRACT_METADATA = "extract_metadata"
    GENERATE_DESCRIPTION = "generate_description"
    SEO = "seo"
    TAGS = "tags"
    THUMBNAIL = "thumbnail"
    CONVERT = "convert"
    COMPRESS = "compress"
    WATERMARK = "watermark"
    UPLOAD = "upload"
    INDEX = "index"
    PUBLISH = "publish"


#: The canonical order. Everything that iterates stages iterates this.
PIPELINE: tuple[Stage, ...] = (
    Stage.COLLECT,
    Stage.VALIDATE,
    Stage.EXTRACT_METADATA,
    Stage.GENERATE_DESCRIPTION,
    Stage.SEO,
    Stage.TAGS,
    Stage.THUMBNAIL,
    Stage.CONVERT,
    Stage.COMPRESS,
    Stage.WATERMARK,
    Stage.UPLOAD,
    Stage.INDEX,
    Stage.PUBLISH,
)

#: Stages whose failure stops the job.
#:
#: Everything not in here is *enrichment*: it improves the listing and its absence is
#: visible to nobody but an editor. A book with no AI tags is publishable; a book
#: whose file was never validated is not, and one that was never uploaded does not
#: exist. Blocking publication on an enrichment stage means one provider's bad
#: afternoon becomes a backlog nobody can clear.
REQUIRED_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.COLLECT,
        Stage.VALIDATE,
        Stage.EXTRACT_METADATA,
        Stage.UPLOAD,
        # INDEX is required because it is the stage that writes the pipeline's
        # output back to the catalogue. The search push inside it is already
        # best-effort and swallows its own failures, so the only way this stage
        # fails is the catalogue write failing — and a job that reports success
        # having written nothing back has produced nothing at all.
        Stage.INDEX,
        Stage.PUBLISH,
    }
)

#: Stages that call a model. Skipped wholesale when no provider is configured, rather
#: than attempted thirteen times to discover the same 503.
AI_STAGES: frozenset[Stage] = frozenset({Stage.GENERATE_DESCRIPTION, Stage.SEO, Stage.TAGS})


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Deliberately not run — no provider configured, no cover page, disabled by
    #: settings. Distinct from FAILED because "we chose not to" and "we tried and
    #: could not" lead to different operator actions.
    SKIPPED = "skipped"


class SourceKind(StrEnum):
    """How a job entered the pipeline. Kept because the answer to "why is this book
    wrong" is usually "look at where it came from"."""

    UPLOAD = "upload"
    IMPORT = "import"
    EVENT = "event"
    MANUAL = "manual"
    REPROCESS = "reprocess"


class ImportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    #: Some rows landed, some did not. Its own status because reporting a partial
    #: import as "completed" hides the rows a human still has to fix.
    PARTIAL = "partial"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobOptions(BaseSchema):
    """Per-job overrides. Absent means the service default."""

    #: Run these stages only. Everything else is marked SKIPPED. Used to re-run one
    #: stage after fixing it, without redoing the expensive ones around it.
    only_stages: list[Stage] | None = None
    skip_stages: list[Stage] = Field(default_factory=list)
    #: Ignore cached AI output and call the model again. Costs money; audited.
    force_ai_refresh: bool = False
    #: Publish on success. Overrides `AUTO_PUBLISH` for this job.
    auto_publish: bool | None = None
    watermark: bool | None = None
    locale: str = Field(default="en", max_length=10)


class JobCreate(BaseSchema):
    """Start a pipeline run for one already-uploaded file."""

    book_id: uuid.UUID
    #: The storage key of the uploaded source. The pipeline never accepts bytes over
    #: HTTP: a 400MB body through the gateway is a request that cannot be retried,
    #: and the file is already in object storage by the time anyone asks for this.
    source_key: str = Field(min_length=1, max_length=500)
    original_filename: str | None = Field(default=None, max_length=255)
    source: SourceKind = SourceKind.UPLOAD
    priority: int = Field(default=0, ge=-10, le=10)
    options: JobOptions = Field(default_factory=JobOptions)

    @field_validator("source_key")
    @classmethod
    def _no_traversal(cls, value: str) -> str:
        # The key names an object this service will read and derive names from. A
        # caller-supplied `..` is how a derived-artefact key escapes its prefix.
        if ".." in value or value.startswith("/"):
            raise ValueError("source_key must be a plain storage key.")
        return value


class StageOut(BaseSchema):
    stage: Stage
    status: StageStatus
    attempts: int = 0
    duration_ms: int | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobOut(BaseSchema):
    id: uuid.UUID
    book_id: uuid.UUID
    status: JobStatus
    source: SourceKind
    source_key: str
    original_filename: str | None = None
    current_stage: Stage | None = None
    completed_stages: list[Stage] = Field(default_factory=list)
    attempts: int = 0
    priority: int = 0
    error: str | None = None
    #: Present when the job was dead-lettered. The stage history explains why.
    dead_lettered_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class JobDetail(JobOut):
    stages: list[StageOut] = Field(default_factory=list)


class JobPage(BaseSchema):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class JobActionResponse(BaseSchema):
    id: uuid.UUID
    status: JobStatus
    message: str


class RetryRequest(BaseSchema):
    #: Resume from here rather than from the last completed stage. Used when a stage
    #: succeeded but produced something wrong.
    from_stage: Stage | None = None
    #: Clear the attempt counter. Without it a dead-lettered job retried once more
    #: immediately dead-letters again.
    reset_attempts: bool = True


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


class ImportRow(BaseSchema):
    """One row of a bulk import.

    Deliberately close to the books service's create payload: an import that invents
    its own field names becomes a mapping layer that drifts from the catalogue.
    """

    title: str = Field(min_length=1, max_length=500)
    subtitle: str | None = Field(default=None, max_length=500)
    authors: list[str] = Field(default_factory=list, max_length=20)
    categories: list[str] = Field(default_factory=list, max_length=20)
    isbn13: str | None = Field(default=None, max_length=17)
    language: str = Field(default="en", max_length=10)
    price_minor: int = Field(default=0, ge=0)
    currency: str = Field(default="INR", max_length=3)
    description: str | None = Field(default=None, max_length=50_000)
    source_key: str | None = Field(default=None, max_length=500)
    #: Row number in the uploaded file, so an error report points at a line the
    #: operator can find rather than at an index into a list they never saw.
    line: int | None = None


class ImportCreate(BaseSchema):
    filename: str = Field(default="import.csv", max_length=255)
    rows: list[ImportRow] = Field(min_length=1, max_length=1_000)
    #: Validate and report without writing anything. The default, because an import
    #: is the one operation here that creates hundreds of rows at once.
    dry_run: bool = True
    options: JobOptions = Field(default_factory=JobOptions)


class ImportError_(BaseSchema):
    line: int | None = None
    title: str | None = None
    error: str


class ImportOut(BaseSchema):
    id: uuid.UUID
    filename: str
    status: ImportStatus
    dry_run: bool
    total: int
    succeeded: int
    failed: int
    errors: list[ImportError_] = Field(default_factory=list)
    job_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime
    finished_at: datetime | None = None


class ImportPage(BaseSchema):
    items: list[ImportOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Inspection / reporting
# ---------------------------------------------------------------------------


class DocumentInfo(BaseSchema):
    """What inspection learned about a file. The output of the metadata stage, and
    the input to everything that follows."""

    format: str
    size_bytes: int
    checksum: str
    page_count: int | None = None
    title: str | None = None
    author: str | None = None
    language: str | None = None
    isbn13: str | None = None
    publication_date: str | None = None
    encrypted: bool = False
    #: Extracted text, truncated. Never the whole book — nothing downstream needs it
    #: and every provider charges for it.
    excerpt: str = ""
    #: True when the file parsed but yielded no text at all: a scanned book. Worth
    #: knowing, because every AI stage after this will be working from the title
    #: alone and the output will read like it.
    text_empty: bool = False


class PipelineStats(BaseSchema):
    window_days: int
    total: int
    succeeded: int
    failed: int
    dead_lettered: int
    running: int
    queued: int
    #: Per-stage failure counts. The one number that says which stage to fix first.
    failures_by_stage: dict[str, int] = Field(default_factory=dict)
    median_duration_ms: int | None = None


class SweepResult(BaseSchema):
    requeued_stale: int = 0
    pruned_jobs: int = 0
    retried: int = 0

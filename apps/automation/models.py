"""SQLAlchemy models for the automation service (schema ``automation``).

The table that carries the weight here is **``job_stages``**. It is what makes a
retry cheap: each stage records its own outcome and its own output, so a job that
died at stage 11 resumes at stage 11 instead of redoing ten minutes of CPU and three
dollars of tokens. Without it, `acks_late=True` — a worker killed mid-deploy gets its
task redelivered — would mean re-running the whole pipeline, including the side
effects that already landed.

**``automation_jobs.result``** accumulates each stage's output into one document. A
stage reads what earlier stages produced from there rather than from a parameter
chain, which is what lets the runner start from the middle without reconstructing the
arguments the skipped stages would have passed.

**``processed_events``** makes event consumption idempotent. Redis Streams deliver at
least once, and a redelivered ``book.created`` must not queue a second pipeline run
against the same file.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from knowledgeos_core import (
    Base,
    JobStatus,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    UUIDType,
)
from schemas import ImportStatus, SourceKind, Stage, StageStatus

SCHEMA = "automation"

JSONType = JSON().with_variant(JSONB, "postgresql")


def _enum(enum_cls: type, name: str) -> SAEnum:
    """VARCHAR + CHECK rather than a native PostgreSQL ENUM.

    Adding a stage is then an ordinary reversible migration instead of an
    ``ALTER TYPE``, which cannot run inside a transaction on older servers and
    cannot be undone at all.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        values_callable=lambda enum: [member.value for member in enum],
        length=48,
    )


class AutomationJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One end-to-end run of the pipeline for one book."""

    __tablename__ = "automation_jobs"
    __table_args__ = (
        # One live job per source file. A double-click on "process" must not run the
        # pipeline twice against the same object — two runs would both write derived
        # artefacts and both try to publish, and the second would overwrite the
        # first's output halfway through.
        Index(
            "uq_automation_jobs_active_source",
            "source_key",
            unique=True,
            postgresql_where=text("status IN ('queued','running','retrying')"),
            sqlite_where=text("status IN ('queued','running','retrying')"),
        ),
        Index("ix_automation_jobs_status_created", "status", "created_at"),
        Index("ix_automation_jobs_book", "book_id", "created_at"),
        # The claim query for the worker sweep: highest priority, oldest first.
        Index("ix_automation_jobs_queue", "status", "priority", "created_at"),
        CheckConstraint("attempts >= 0", name="ck_automation_jobs_attempts"),
        {"schema": SCHEMA},
    )

    book_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    source: Mapped[SourceKind] = mapped_column(
        _enum(SourceKind, "automation_source_kind"), nullable=False, default=SourceKind.UPLOAD
    )
    source_key: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "automation_job_status"),
        nullable=False,
        default=JobStatus.QUEUED,
        index=True,
    )
    #: The stage in flight. Null once the job settles.
    current_stage: Mapped[Stage | None] = mapped_column(
        _enum(Stage, "automation_stage"), nullable=True
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    options: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    #: Accumulated stage output. What a resumed run reads instead of re-deriving.
    result: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Refreshed as each stage completes. A job whose heartbeat has gone stale was
    #: on a worker that died between the broker ack and the checkpoint; the sweep
    #: requeues those rather than leaving them RUNNING forever.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When a retry becomes eligible. Backoff lives in the row, not in a sleeping
    #: worker — a worker holding a task through a 10-minute backoff is a worker
    #: doing nothing while the queue grows.
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    #: The request id that started this. Joins a job's logs to the HTTP call that
    #: queued it, across every service the pipeline touches.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    import_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.imports.id", ondelete="SET NULL"), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stages: Mapped[list[JobStage]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobStage.position",
        lazy="selectin",
    )

    @property
    def completed_stages(self) -> list[Stage]:
        """Which stages have succeeded, from the loaded stage rows.

        Returns an empty list when the relationship has not been loaded, rather than
        triggering a lazy load. This property is read during response serialisation,
        and a lazy load there happens outside SQLAlchemy's greenlet context under
        asyncio — which does not degrade to a slow query, it raises `MissingGreenlet`
        and turns every job response into a 500.
        """
        if "stages" in inspect(self).unloaded:
            return []
        return [row.stage for row in self.stages if row.status is StageStatus.SUCCEEDED]

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
            JobStatus.CANCELLED,
        }


class JobStage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One stage's outcome within a job. The checkpoint."""

    __tablename__ = "job_stages"
    __table_args__ = (
        # One row per stage per job. A resumed run updates its row rather than
        # appending a second one, so the history stays readable and "has this stage
        # completed?" is a lookup rather than a scan-and-max.
        UniqueConstraint("job_id", "stage", name="uq_job_stages_job_stage"),
        Index("ix_job_stages_status", "status", "stage"),
        {"schema": SCHEMA},
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey(f"{SCHEMA}.automation_jobs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[Stage] = mapped_column(_enum(Stage, "automation_stage"), nullable=False)
    #: Index into the canonical pipeline order, denormalised so the history sorts
    #: correctly without the reader knowing the order.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[StageStatus] = mapped_column(
        _enum(StageStatus, "automation_stage_status"),
        nullable=False,
        default=StageStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[AutomationJob] = relationship(back_populates="stages")


class ImportBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A bulk catalogue import.

    Kept separate from the jobs it spawns because the interesting question about an
    import is "which rows failed and why", and that answer must survive the jobs
    being pruned.
    """

    __tablename__ = "imports"
    __table_args__ = (
        Index("ix_imports_status_created", "status", "created_at"),
        {"schema": SCHEMA},
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        _enum(ImportStatus, "automation_import_status"),
        nullable=False,
        default=ImportStatus.PENDING,
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Row-level errors, with the line number from the uploaded file so an operator
    #: can find the offending row rather than an index into a list they never saw.
    errors: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessedEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Idempotency ledger for consumed events.

    Written in the same transaction as the effect it guards. Writing it afterwards
    drops the effect when the process dies in between; committing it separately runs
    the pipeline twice on a redelivery.
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_automation_processed_events_event_id"),
        {"schema": SCHEMA},
    )

    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

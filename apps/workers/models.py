"""SQLAlchemy models for the workers service (schema ``workers``).

One table. It exists to answer one question an operator actually asks: **did the
nightly reconcile run, and did it work?**

That question could be answered from logs, and the answer would be "grep several
hundred megabytes across however many replicas" — which is why nobody asks it until
something has already gone wrong for a week. A row per run makes it a `SELECT`.

It also answers the question logs are *worst* at: **has beat stopped firing?** A job
that is not running produces no log lines at all, so the absence is invisible in a
log search and obvious in `last_run_at`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from knowledgeos_core import Base, TimestampMixin, UUIDPrimaryKeyMixin, UUIDType
from schemas import RunOutcome

SCHEMA = "workers"

JSONType = JSON().with_variant(JSONB, "postgresql")


class TaskRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of one scheduled job."""

    __tablename__ = "task_runs"
    __table_args__ = (
        # The history query: "show me this job's last N runs".
        Index("ix_task_runs_job_started", "job_name", "started_at"),
        # The dashboard query: "what failed today?".
        Index("ix_task_runs_outcome_started", "outcome", "started_at"),
        {"schema": SCHEMA},
    )

    job_name: Mapped[str] = mapped_column(String(120), nullable=False)
    outcome: Mapped[RunOutcome] = mapped_column(
        SAEnum(
            RunOutcome,
            name="worker_run_outcome",
            native_enum=False,
            values_callable=lambda enum: [member.value for member in enum],
            length=32,
        ),
        nullable=False,
    )
    #: Null when the run never reached the sibling service — a lock it did not win, or
    #: a job switched off. Distinct from a 500, which is a status.
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: What the sibling service reported. Truncated at the service boundary — this is
    #: a record that a sweep ran, not a copy of its output.
    detail: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Which replica ran it. The first thing to check when one host's runs all fail.
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Null for a scheduled run; set when an operator triggered it by hand.
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)

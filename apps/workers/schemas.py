"""Request and response schemas for the workers service."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from knowledgeos_core import BaseSchema


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Another replica held the lock. Not a failure — it means the schedule is
    #: working and single-flight is doing its job. Recorded distinctly so a
    #: three-replica deployment does not look like it is failing two runs in three.
    SKIPPED_LOCKED = "skipped_locked"
    #: Switched off for this deployment.
    DISABLED = "disabled"
    #: The sibling service did not answer inside the job's budget. Distinct from
    #: FAILED because the sweep may well have completed — we simply stopped waiting,
    #: and treating it as a failure invites a retry that duplicates the work.
    TIMED_OUT = "timed_out"


class JobOut(BaseSchema):
    """One schedule entry, with its recent health."""

    name: str
    service: str
    path: str
    cron: str
    enabled: bool
    critical: bool
    timeout: float
    description: str = ""

    last_run_at: datetime | None = None
    last_outcome: RunOutcome | None = None
    last_duration_ms: int | None = None
    #: Consecutive failures. One is noise — a deploy, a restart, a blip. Three in a
    #: row is a signal.
    consecutive_failures: int = 0
    healthy: bool = True


class RunOut(BaseSchema):
    id: uuid.UUID
    job_name: str
    outcome: RunOutcome
    status_code: int | None = None
    duration_ms: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    worker_id: str | None = None
    triggered_by: uuid.UUID | None = None


class RunPage(BaseSchema):
    items: list[RunOut]
    total: int
    limit: int
    offset: int


class TriggerResponse(BaseSchema):
    job_name: str
    outcome: RunOutcome
    duration_ms: int
    status_code: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class SchedulerHealth(BaseSchema):
    """Whether the scheduler itself is alive.

    The failure this is really watching for is **beat having stopped**. A job that is
    not running produces no logs and no failures, so it is invisible in every signal
    except the absence of recent runs — which is why `stale_jobs` is here and is the
    field worth alerting on.
    """

    total_jobs: int
    enabled_jobs: int
    unhealthy_jobs: list[str] = Field(default_factory=list)
    #: Enabled jobs with no run recorded inside several of their own intervals.
    stale_jobs: list[str] = Field(default_factory=list)
    runs_last_hour: int = 0
    failures_last_hour: int = 0
    last_run_at: datetime | None = None


class PruneResponse(BaseSchema):
    pruned: int

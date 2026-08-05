"""Configuration for the workers service.

This service owns no domain data and makes no business decisions. It calls
`/internal/maintenance/*` on the services that do, on a schedule.

The setting that matters most is ``disabled_jobs``. A deployment without a
Meilisearch instance or an AI provider should not run those sweeps at all — a job
that fails every hour by design is a job that teaches everyone to ignore the failure
count, and then the one that matters goes unnoticed too.
"""

from __future__ import annotations

from pydantic import Field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    service_name: str = "workers"
    database_schema: str = "workers"
    port: int = 8009

    jwks_url: str | None = "http://localhost:8001/.well-known/jwks.json"

    #: Jobs switched off for this deployment, by name.
    disabled_jobs: CsvList = Field(default_factory=list)

    #: How long a run record is kept. Long enough to answer "has the nightly reconcile
    #: been failing all week?", which is the question this history exists for.
    run_retention_days: int = 30

    #: Attempts per scheduled call. Deliberately low: every job here runs again on its
    #: own schedule, so a failed run is at worst a delay. Retrying hard would turn a
    #: struggling sibling service into a service under attack from its own platform.
    max_attempts: int = 2
    retry_delay_seconds: int = 30

    #: Consecutive failures before a job is reported unhealthy. One failed run is
    #: noise — a deploy, a restart, a network blip. Three in a row is a signal.
    unhealthy_after_failures: int = 3

    #: Beat writes its schedule state here. On a container with an ephemeral
    #: filesystem this is fine: losing it means at worst one duplicate run, and the
    #: Redis lock makes that a no-op.
    beat_schedule_path: str = "/tmp/celerybeat-schedule"  # noqa: S108 - ephemeral by design

    events_enabled: bool = False

    @property
    def disabled_job_set(self) -> set[str]:
        return {name.strip() for name in self.disabled_jobs if name.strip()}


settings = Settings()

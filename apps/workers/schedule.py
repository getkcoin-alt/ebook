"""The schedule. One table, and it is the whole configuration of this service.

Every entry names a **sibling service and an HTTP path**, never a table. That is the
single most important decision here.

A scheduler with direct database access to every schema is how a microservice
platform quietly re-couples: the sweep that expires orders ends up encoding the
payment service's order state machine, and now two deployables must change together
whenever that machine does. Worse, the constraint that makes schema-per-service worth
anything — that only one service writes to a schema (ADR 0002) — is broken by the one
process nobody thinks of as a service.

So this service knows how to call an HTTP endpoint on a schedule, and nothing else.
The endpoints it calls are the same `/internal/maintenance/*` routes each owning
service already exposes and already tests. Adding a sweep is a row here plus a route
there; it is never a migration in someone else's schema.

## Choosing a cadence

Two rules:

**Nothing expensive runs on the hour.** Every entry that could be slow is offset —
`:07`, `:23`, `:41`. A platform where six services all wake at `:00` produces a
six-way load spike once an hour and a flat sixty minutes either side, and the spike is
what sizes the database.

**Frequency follows the cost of being late, not the cost of running.** Expiring unpaid
orders is cheap and being late means holding stock that is not sold, so it runs every
five minutes. Pruning last year's per-user AI budget rows is also cheap, but nobody
notices if it is a day late, so it runs nightly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One recurring call to a sibling service."""

    #: Stable identifier. Used as the lock key, the metric label and the run-history
    #: key, so renaming one loses its history — treat it as an identifier, not a
    #: description.
    name: str
    #: The service that owns the work. Resolved through service discovery.
    service: str
    path: str
    #: Celery beat cron fields. Minute offsets are deliberate; see the module docstring.
    minute: str = "0"
    hour: str = "*"
    day_of_week: str = "*"
    method: str = "POST"
    params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    #: Read budget. A reconcile walks the whole catalogue; an order sweep does not.
    timeout: float = 60.0
    #: Held for the run so two replicas cannot both fire. Must exceed the job's
    #: realistic worst case, or a slow run releases its own lock and the next tick
    #: starts a second copy on top of it.
    lock_ttl: int = 300
    #: A failure here is worth waking someone for. Most sweeps are not: missing one
    #: run of a nightly prune costs nothing, and paging on it trains people to ignore
    #: the pager.
    critical: bool = False
    enabled: bool = True
    description: str = ""

    @property
    def cron(self) -> dict[str, str]:
        return {
            "minute": self.minute,
            "hour": self.hour,
            "day_of_week": self.day_of_week,
        }


SCHEDULE: tuple[ScheduledJob, ...] = (
    # ---- payment ---------------------------------------------------------
    ScheduledJob(
        name="payment.expire-orders",
        service="payment",
        path="/internal/maintenance/expire-orders",
        minute="*/5",
        timeout=30.0,
        lock_ttl=120,
        # The one genuinely urgent sweep on the platform. An unpaid order holds a
        # reservation and, for a limited title, holds stock that is not being sold.
        critical=True,
        description="Release orders whose payment window has closed.",
    ),
    ScheduledJob(
        name="payment.expire-subscriptions",
        service="payment",
        path="/internal/maintenance/expire-subscriptions",
        minute="17",
        timeout=60.0,
        description=("Safety net for a missed cancellation webhook, not the primary mechanism."),
    ),
    ScheduledJob(
        name="payment.approve-conversions",
        service="payment",
        path="/internal/maintenance/approve-conversions",
        minute="23",
        hour="2",
        timeout=120.0,
        description="Mature affiliate conversions past their clawback window.",
    ),
    # ---- notifications ---------------------------------------------------
    ScheduledJob(
        name="notifications.retry",
        service="notification",
        path="/internal/maintenance/retry",
        minute="*/10",
        timeout=120.0,
        lock_ttl=300,
        # A receipt that never arrives is a support ticket, so this is more urgent
        # than most — but not urgent enough to wake anyone at 3am, because the next
        # run in ten minutes will catch it.
        description="Re-attempt deliveries that failed transiently.",
    ),
    ScheduledJob(
        name="notifications.prune",
        service="notification",
        path="/internal/maintenance/prune",
        minute="41",
        hour="3",
        timeout=300.0,
        lock_ttl=600,
        description="Drop delivery records past their retention window.",
    ),
    # ---- automation ------------------------------------------------------
    ScheduledJob(
        name="automation.drain",
        service="automation",
        path="/internal/maintenance/drain",
        minute="*/2",
        params={"limit": 5},
        # Longer than the tick: a job that takes twelve minutes must not have a
        # second copy started on top of it at minute two.
        timeout=900.0,
        lock_ttl=1_800,
        critical=True,
        description="Run pipeline jobs whose backoff has expired.",
    ),
    ScheduledJob(
        name="automation.requeue-stale",
        service="automation",
        path="/internal/maintenance/requeue-stale",
        minute="*/15",
        timeout=60.0,
        description=(
            "Rescue jobs whose worker was killed after acking — `acks_late` does not cover those."
        ),
    ),
    ScheduledJob(
        name="automation.prune",
        service="automation",
        path="/internal/maintenance/prune",
        minute="47",
        hour="4",
        timeout=300.0,
        description="Drop old finished jobs. Dead-lettered ones are kept.",
    ),
    # ---- search ----------------------------------------------------------
    ScheduledJob(
        name="search.reconcile",
        service="search",
        path="/internal/reindex",
        minute="13",
        body={"mode": "reconcile"},
        # A reconcile walks the whole catalogue. The search service takes its own
        # distributed lock as well, so this is belt and braces — but the timeout has
        # to accommodate a real catalogue or every run is recorded as a failure.
        timeout=900.0,
        lock_ttl=1_800,
        description="Repair index drift against the catalogue.",
    ),
    ScheduledJob(
        name="search.prune-analytics",
        service="search",
        path="/internal/maintenance/prune-analytics",
        minute="29",
        hour="3",
        timeout=300.0,
        description="Drop query analytics past their useful life.",
    ),
    # ---- ai --------------------------------------------------------------
    ScheduledJob(
        name="ai.prune",
        service="ai",
        path="/internal/maintenance/prune",
        minute="53",
        hour="4",
        timeout=300.0,
        description="Drop expired generation cache and old per-user budget rows.",
    ),
    # ---- workers (this service) -----------------------------------------
    ScheduledJob(
        name="workers.prune-runs",
        service="workers",
        path="/internal/maintenance/prune",
        minute="59",
        hour="5",
        timeout=60.0,
        description=(
            "Drop this service's own run history past retention. A history table that "
            "grows forever is the problem this service exists to solve for everyone "
            "else."
        ),
    ),
    # ---- admin -----------------------------------------------------------
    ScheduledJob(
        name="admin.prune-flag-audits",
        service="admin",
        path="/internal/maintenance/prune-audits",
        minute="43",
        hour="5",
        timeout=120.0,
        description="Drop flag audit rows past retention.",
    ),
    # ---- auth ------------------------------------------------------------
    ScheduledJob(
        name="auth.prune",
        service="auth",
        path="/internal/maintenance/prune",
        minute="37",
        hour="5",
        timeout=300.0,
        description=(
            "Drop expired tokens and sessions past their grace window, and audit rows "
            "past retention. Security-relevant audit actions are exempt."
        ),
    ),
)

#: Name -> job. Built once; a duplicate name is a startup failure rather than a
#: silently-shadowed schedule entry.
BY_NAME: dict[str, ScheduledJob] = {}
for _job in SCHEDULE:
    if _job.name in BY_NAME:
        raise RuntimeError(f"Duplicate scheduled job name: {_job.name}")
    BY_NAME[_job.name] = _job
del _job


def enabled_jobs(disabled: set[str] | None = None) -> list[ScheduledJob]:
    """The schedule minus anything switched off for this deployment.

    A deployment without an AI provider or a Meilisearch instance should not run
    those sweeps at all — a job that fails every hour by design is a job that teaches
    everyone to ignore the failure count.
    """
    excluded = disabled or set()
    return [job for job in SCHEDULE if job.enabled and job.name not in excluded]

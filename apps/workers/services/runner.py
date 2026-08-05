"""Executing one scheduled job.

Three properties, in the order they matter.

**Single-flight.** Every deployment runs more than one replica, and beat fires on all
of them. Without a lock, `payment.expire-orders` runs three times a minute against a
service that is idempotent but not free, and `search.reconcile` runs three
simultaneous catalogue walks. The lock is taken in Redis, held for the job's
`lock_ttl`, and a caller that loses it records `skipped_locked` and stops. Losing the
lock is a **normal outcome, not a failure** — a three-replica deployment must not look
like it is failing two runs in three.

**Bounded.** Every call has an explicit read timeout, and a timeout is recorded as
`timed_out` rather than `failed`. The distinction is not cosmetic: a sweep that
exceeded our patience has very likely completed on the other side, and treating that
as a failure invites a retry that duplicates the work.

**Non-escalating.** Retries are deliberately shallow — every job runs again on its own
schedule, so a failed run is a delay, not data loss. A worker that retries hard turns
a struggling sibling into a service under sustained attack from its own platform, at
exactly the moment it can least take it.

Nothing here interprets what a sweep did. The response body is recorded and that is
all. The moment this service starts reasoning about order states, it has taken on the
payment service's domain and two deployables have to change together.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from schedule import ScheduledJob

from knowledgeos_core import get_logger
from knowledgeos_core.http import ServiceRegistry
from knowledgeos_core.redis import RedisClient
from models import TaskRun
from schemas import RunOutcome
from settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class RunResult:
    job_name: str
    outcome: RunOutcome
    duration_ms: int = 0
    status_code: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        # `skipped_locked` counts as fine: it means single-flight worked.
        return self.outcome in (RunOutcome.SUCCEEDED, RunOutcome.SKIPPED_LOCKED)


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        services: ServiceRegistry | None,
        redis: RedisClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._services = services
        self._redis = redis
        self._worker_id = worker_id

    async def run(
        self,
        job: ScheduledJob,
        *,
        triggered_by: uuid.UUID | None = None,
        ignore_lock: bool = False,
    ) -> RunResult:
        """Execute one job. Never raises — the outcome is the return value.

        A scheduled task that raises gets retried by Celery on top of whatever the
        retry policy says, which is a second, invisible retry layer stacked on the one
        configured here. Returning the outcome keeps the policy in one place.
        """
        if job.name in self._settings.disabled_job_set:
            return RunResult(job_name=job.name, outcome=RunOutcome.DISABLED)

        # An operator pressing "run now" wants it to run now, not to be told another
        # replica might. Scheduled firings always take the lock.
        if self._redis is None or ignore_lock:
            return await self._execute(job)

        async with self._redis.lock(f"workers:{job.name}", ttl=job.lock_ttl) as acquired:
            if not acquired:
                logger.debug("workers.job_locked", job=job.name)
                return RunResult(job_name=job.name, outcome=RunOutcome.SKIPPED_LOCKED)
            return await self._execute(job)

    async def _execute(self, job: ScheduledJob) -> RunResult:
        if self._services is None:
            return RunResult(
                job_name=job.name,
                outcome=RunOutcome.FAILED,
                error="Service discovery is not configured.",
            )

        client = self._services.get(job.service)
        started = time.perf_counter()

        try:
            response = await client.request(
                job.method,
                job.path,
                params=job.params or None,
                json=job.body,
                timeout=job.timeout,
            )
        except httpx.TimeoutException:
            elapsed = int((time.perf_counter() - started) * 1000)
            # Not a failure. The sweep has very likely finished on the other side; we
            # stopped waiting. Retrying would duplicate whatever it did.
            logger.warning("workers.job_timed_out", job=job.name, timeout=job.timeout)
            return RunResult(
                job_name=job.name,
                outcome=RunOutcome.TIMED_OUT,
                duration_ms=elapsed,
                error=f"No response within {job.timeout:.0f}s.",
            )
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning("workers.job_failed", job=job.name, service=job.service, error=str(exc))
            return RunResult(
                job_name=job.name,
                outcome=RunOutcome.FAILED,
                duration_ms=elapsed,
                error=str(exc)[:2_000],
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        detail = _summarise(response)

        # A response is not a success. The shared client only raises for transport
        # failures — a 500 from the sweep comes back as an ordinary response object,
        # and recording that as a successful run is how a sweep that has been broken
        # for a week shows up green on the dashboard.
        if response.status_code >= 400:
            logger.warning(
                "workers.job_rejected",
                job=job.name,
                service=job.service,
                status=response.status_code,
            )
            return RunResult(
                job_name=job.name,
                outcome=RunOutcome.FAILED,
                duration_ms=elapsed,
                status_code=response.status_code,
                detail=detail,
                error=f"{job.service} returned {response.status_code}.",
            )

        logger.info(
            "workers.job_ran",
            job=job.name,
            service=job.service,
            status=response.status_code,
            duration_ms=elapsed,
        )
        return RunResult(
            job_name=job.name,
            outcome=RunOutcome.SUCCEEDED,
            duration_ms=elapsed,
            status_code=response.status_code,
            detail=detail,
        )

    def to_row(self, result: RunResult, *, triggered_by: uuid.UUID | None = None) -> TaskRun:
        """The history row for a run.

        `started_at` is reconstructed from the duration rather than captured before
        the call, so a row's start and end always agree with the duration beside them.
        """
        finished = datetime.now(UTC)
        return TaskRun(
            job_name=result.job_name,
            outcome=result.outcome,
            status_code=result.status_code,
            duration_ms=result.duration_ms,
            detail=result.detail,
            error=result.error,
            started_at=finished - _millis(result.duration_ms),
            finished_at=finished,
            worker_id=self._worker_id,
            triggered_by=triggered_by,
        )


def _summarise(response: httpx.Response) -> dict[str, Any]:
    """What the sibling service reported, bounded.

    A record that a sweep ran, not a copy of its output: a reconcile's response could
    be large, and a history table that grows with the size of other services'
    responses is a history table nobody keeps.
    """
    try:
        payload = response.json()
    except Exception:
        return {"body": response.text[:500]}

    if isinstance(payload, dict):
        return {
            key: value
            for key, value in payload.items()
            if isinstance(value, (int, float, bool, str)) and len(str(value)) <= 500
        }
    if isinstance(payload, list):
        return {"items": len(payload)}
    return {"value": str(payload)[:500]}


def _millis(value: int):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(milliseconds=max(0, value))

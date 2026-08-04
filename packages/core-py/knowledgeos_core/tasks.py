"""Celery foundation shared by the automation, worker and notification services.

**Reliability posture.** Book processing is long, expensive and user-visible, so the
defaults here favour not losing work over raw throughput:

* ``acks_late=True`` — a task is acknowledged only after it finishes. If a worker is
  killed mid-job (deploy, OOM, spot reclaim) the broker redelivers it. The tradeoff
  is that tasks must be idempotent, which the pipeline's stage checkpointing gives us.
* ``prefetch_multiplier=1`` — a worker holds one task at a time. With long tasks,
  prefetching means a restarting worker drops a queue's worth of jobs and fast
  workers starve while one holds a backlog.
* ``reject_on_worker_lost=True`` — requeue rather than silently drop on SIGKILL.

**Queue routing.** Separate queues by cost profile so a 10-minute PDF conversion
never blocks a 200ms email send.
"""

from __future__ import annotations

import time
from typing import Any

from celery import Celery, Task
from celery.signals import (
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
)
from kombu import Exchange, Queue

from .config import ServiceSettings
from .logging import configure_logging, get_logger, request_id_ctx
from .metrics import dead_letter_total, task_duration_seconds, task_executions_total

logger = get_logger(__name__)

# Cost-tiered queues. Concurrency is tuned per queue in the worker command.
QUEUE_DEFAULT = "default"
QUEUE_AUTOMATION = "automation"  # CPU/IO heavy: conversion, compression, watermark
QUEUE_AI = "ai"  # network bound, rate limited by provider quotas
QUEUE_NOTIFICATIONS = "notifications"  # short, latency sensitive
QUEUE_SEARCH = "search"  # indexing
QUEUE_MAINTENANCE = "maintenance"  # cleanup, backups, scheduled reports

_exchange = Exchange("knowledgeos", type="direct", durable=True)

QUEUES = tuple(
    Queue(name, exchange=_exchange, routing_key=name, durable=True)
    for name in (
        QUEUE_DEFAULT,
        QUEUE_AUTOMATION,
        QUEUE_AI,
        QUEUE_NOTIFICATIONS,
        QUEUE_SEARCH,
        QUEUE_MAINTENANCE,
    )
)


class BaseTask(Task):
    """Default task behaviour: retry transient failures with jittered backoff."""

    autoretry_for = (Exception,)
    max_retries = 5
    retry_backoff = True
    retry_backoff_max = 600
    # Jitter prevents a thundering herd: without it, 500 tasks that failed together
    # because a provider went down all retry at the same instant and knock it over
    # again the moment it recovers.
    retry_jitter = True
    acks_late = True
    reject_on_worker_lost = True
    track_started = True

    def on_failure(self, exc: Exception, task_id: str, args: Any, kwargs: Any, einfo: Any) -> None:
        """Terminal failure: every retry is exhausted."""
        dead_letter_total.labels(
            service=self.request.hostname or "worker",
            task=self.name,
            reason=type(exc).__name__,
        ).inc()
        logger.error(
            "task.dead_lettered",
            task=self.name,
            task_id=task_id,
            error=str(exc),
            attempts=self.request.retries + 1,
        )


def create_celery(settings: ServiceSettings, *, name: str | None = None) -> Celery:
    """Build the shared Celery application."""
    app = Celery(name or settings.service_name)

    app.conf.update(
        broker_url=settings.redis_url,
        result_backend=settings.redis_url,
        task_cls=f"{BaseTask.__module__}:{BaseTask.__qualname__}",
        # --- serialization: JSON only. Pickle would let anyone who can write to
        # Redis achieve remote code execution on every worker.
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # --- reliability
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        task_send_sent_event=True,
        worker_send_task_events=True,
        # --- results: keep long enough for the UI to poll a job to completion,
        # short enough that Redis memory stays bounded.
        result_expires=86_400,
        result_extended=True,
        # --- timeouts. Hard kill 60s after the soft warning so a task gets a chance
        # to clean up temp files and mark its DB row failed before being killed.
        task_soft_time_limit=1_800,
        task_time_limit=1_860,
        # --- recycle workers periodically; image/PDF libraries leak native memory
        # and an unbounded worker eventually gets OOM-killed mid-job.
        worker_max_tasks_per_child=100,
        worker_max_memory_per_child=512_000,  # KiB
        # --- queues
        task_queues=QUEUES,
        task_default_queue=QUEUE_DEFAULT,
        task_default_exchange="knowledgeos",
        task_default_routing_key=QUEUE_DEFAULT,
        task_routes={
            "automation.*": {"queue": QUEUE_AUTOMATION},
            "ai.*": {"queue": QUEUE_AI},
            "notifications.*": {"queue": QUEUE_NOTIFICATIONS},
            "search.*": {"queue": QUEUE_SEARCH},
            "maintenance.*": {"queue": QUEUE_MAINTENANCE},
        },
        # --- broker resilience
        broker_connection_retry_on_startup=True,
        broker_connection_max_retries=None,  # retry forever; Redis may boot after us
        broker_pool_limit=20,
        broker_transport_options={
            # Must exceed the longest task, or Redis redelivers a task that is still
            # running and it executes twice.
            "visibility_timeout": 3_600,
            "fanout_prefix": True,
            "fanout_patterns": True,
        },
        redis_backend_health_check_interval=30,
        timezone="UTC",
        enable_utc=True,
    )
    return app


# ---- signal wiring: metrics + structured logs for every task -------------

_task_started_at: dict[str, float] = {}


@setup_logging.connect
def _configure_task_logging(**_: Any) -> None:
    """Stop Celery replacing our structlog configuration with its own."""


def install_task_observability(settings: ServiceSettings) -> None:
    """Attach metric/log signal handlers. Call once from the worker entrypoint."""
    configure_logging(
        service_name=settings.service_name,
        level=settings.log_level,
        fmt=settings.log_format,
        version=settings.service_version,
        environment=settings.environment,
    )
    service = settings.service_name

    @task_prerun.connect
    def _prerun(task_id: str = "", task: Task | None = None, **kwargs: Any) -> None:
        _task_started_at[task_id] = time.perf_counter()
        # Reuse the request id the publisher captured, so a Celery job's logs join
        # the HTTP request that queued it.
        headers = getattr(getattr(task, "request", None), "headers", None) or {}
        if correlation := headers.get("correlation_id"):
            request_id_ctx.set(correlation)
        logger.info("task.started", task=task.name if task else "unknown", task_id=task_id)

    @task_postrun.connect
    def _postrun(
        task_id: str = "", task: Task | None = None, state: str = "", **kwargs: Any
    ) -> None:
        started = _task_started_at.pop(task_id, None)
        name = task.name if task else "unknown"
        outcome = (state or "UNKNOWN").lower()
        task_executions_total.labels(service=service, task=name, outcome=outcome).inc()
        if started is not None:
            elapsed = time.perf_counter() - started
            task_duration_seconds.labels(service=service, task=name).observe(elapsed)
            logger.info(
                "task.finished",
                task=name,
                task_id=task_id,
                state=state,
                duration_ms=round(elapsed * 1000, 2),
            )

    @task_retry.connect
    def _retry(request: Any = None, reason: Any = None, **kwargs: Any) -> None:
        logger.warning(
            "task.retrying",
            task=getattr(request, "task", "unknown"),
            task_id=getattr(request, "id", None),
            reason=str(reason)[:500],
            retries=getattr(request, "retries", 0),
        )

    @task_failure.connect
    def _failure(
        task_id: str = "",
        exception: Exception | None = None,
        sender: Task | None = None,
        **kwargs: Any,
    ) -> None:
        logger.error(
            "task.failed",
            task=sender.name if sender else "unknown",
            task_id=task_id,
            error=str(exception),
            error_type=type(exception).__name__ if exception else None,
        )

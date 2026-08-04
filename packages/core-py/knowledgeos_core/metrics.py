"""Prometheus instrumentation.

Metric names follow the Prometheus conventions Grafana dashboards expect:
``<namespace>_<subsystem>_<name>_<unit>``. Every series carries a ``service`` label
so one Grafana datasource can chart the whole platform.

Cardinality rule: labels only ever hold bounded values. Paths are recorded as the
*route template* (``/v1/books/{book_id}``), never the raw URL, or a crawler hitting
random ids would blow up the series count.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    multiprocess,
)
from prometheus_client import (
    generate_latest as _generate_latest,
)
from prometheus_client.core import REGISTRY as DEFAULT_REGISTRY

NAMESPACE = "knowledgeos"

# Buckets tuned for web APIs: dense below 1s, with a long tail for uploads.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests processed.",
    ["service", "method", "path", "status"],
    namespace=NAMESPACE,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency.",
    ["service", "method", "path"],
    namespace=NAMESPACE,
    buckets=LATENCY_BUCKETS,
)

http_requests_in_flight = Gauge(
    "http_requests_in_flight",
    "HTTP requests currently being served.",
    ["service"],
    namespace=NAMESPACE,
)

http_request_body_bytes = Histogram(
    "http_request_body_bytes",
    "Size of accepted request bodies.",
    ["service", "method"],
    namespace=NAMESPACE,
    buckets=(1e3, 1e4, 1e5, 1e6, 5e6, 1e7, 5e7, 1e8),
)

# ---- cross-service calls -----------------------------------------------

upstream_requests_total = Counter(
    "upstream_requests_total",
    "Calls made to another internal service or third-party API.",
    ["service", "upstream", "status"],
    namespace=NAMESPACE,
)

upstream_request_duration_seconds = Histogram(
    "upstream_request_duration_seconds",
    "Latency of calls to another service.",
    ["service", "upstream"],
    namespace=NAMESPACE,
    buckets=LATENCY_BUCKETS,
)

circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state per upstream (0=closed, 1=half-open, 2=open).",
    ["service", "upstream"],
    namespace=NAMESPACE,
)

# ---- background work ----------------------------------------------------

task_executions_total = Counter(
    "task_executions_total",
    "Celery task executions by terminal outcome.",
    ["service", "task", "outcome"],
    namespace=NAMESPACE,
)

task_duration_seconds = Histogram(
    "task_duration_seconds",
    "Celery task execution time.",
    ["service", "task"],
    namespace=NAMESPACE,
    buckets=(0.05, 0.25, 1, 5, 15, 60, 300, 900, 3600),
)

queue_depth = Gauge(
    "queue_depth",
    "Messages waiting in a Celery queue.",
    ["service", "queue"],
    namespace=NAMESPACE,
)

dead_letter_total = Counter(
    "dead_letter_total",
    "Jobs moved to the dead letter queue after exhausting retries.",
    ["service", "task", "reason"],
    namespace=NAMESPACE,
)

# ---- domain -------------------------------------------------------------

cache_operations_total = Counter(
    "cache_operations_total",
    "Cache lookups by result.",
    ["service", "cache", "result"],  # result: hit | miss | error
    namespace=NAMESPACE,
)

rate_limit_rejections_total = Counter(
    "rate_limit_rejections_total",
    "Requests rejected by the rate limiter.",
    ["service", "scope"],
    namespace=NAMESPACE,
)

auth_events_total = Counter(
    "auth_events_total",
    "Authentication outcomes.",
    ["service", "event", "outcome"],
    namespace=NAMESPACE,
)

ai_tokens_total = Counter(
    "ai_tokens_total",
    "LLM tokens consumed.",
    ["service", "provider", "model", "kind"],  # kind: input | output
    namespace=NAMESPACE,
)

ai_cost_usd_total = Counter(
    "ai_cost_usd_total",
    "Estimated LLM spend in USD.",
    ["service", "provider", "model"],
    namespace=NAMESPACE,
)

payment_events_total = Counter(
    "payment_events_total",
    "Payment lifecycle events.",
    ["service", "provider", "event", "outcome"],
    namespace=NAMESPACE,
)

service_info = Gauge(
    "service_info",
    "Static build information; value is always 1.",
    ["service", "version", "environment"],
    namespace=NAMESPACE,
)

build_timestamp_seconds = Gauge(
    "build_timestamp_seconds",
    "Unix timestamp of process start.",
    ["service"],
    namespace=NAMESPACE,
)


def get_registry() -> CollectorRegistry:
    """Return the registry to scrape.

    With multiple Gunicorn/Uvicorn workers per container, ``PROMETHEUS_MULTIPROC_DIR``
    must be set so counters aggregate across processes instead of each worker
    reporting only its own slice.
    """
    import os

    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    return DEFAULT_REGISTRY  # type: ignore[return-value]


def render_metrics() -> bytes:
    return _generate_latest(get_registry())


__all__ = [
    "ai_cost_usd_total",
    "ai_tokens_total",
    "auth_events_total",
    "build_timestamp_seconds",
    "cache_operations_total",
    "circuit_breaker_state",
    "dead_letter_total",
    "get_registry",
    "http_request_body_bytes",
    "http_request_duration_seconds",
    "http_requests_in_flight",
    "http_requests_total",
    "payment_events_total",
    "queue_depth",
    "rate_limit_rejections_total",
    "render_metrics",
    "service_info",
    "task_duration_seconds",
    "task_executions_total",
    "upstream_request_duration_seconds",
    "upstream_requests_total",
]

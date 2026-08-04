"""Gateway configuration.

Everything the edge needs on top of :class:`ServiceSettings`: upstream transport
tuning, the response cache, the ban denylist and the aggregated OpenAPI document.

The service-discovery URLs themselves (``auth_service_url`` and friends) come from
``ServiceSettings`` — the gateway does not redeclare them, so one env var name works
everywhere on the platform.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    """Configuration for the public API gateway."""

    service_name: str = "gateway"
    port: int = 8000
    #: The gateway owns no tables. It is declared for completeness only; no
    #: ``Components(database=True)`` is requested, so nothing ever connects.
    database_schema: str = "public"

    # ---- upstream transport --------------------------------------------
    #: Per-upstream connection budget. Sized well above a single replica's
    #: concurrency so one slow service cannot starve the pool for the others —
    #: each upstream has its own pool precisely so they fail independently.
    upstream_max_connections: int = 200
    upstream_max_keepalive_connections: int = 40
    upstream_keepalive_expiry: float = 30.0
    upstream_connect_timeout: float = 5.0
    #: Default read timeout. Slow upstreams (ai, automation) override it in
    #: ``upstreams.py``; a per-route override wins over both.
    upstream_read_timeout: float = 30.0
    upstream_write_timeout: float = 30.0
    upstream_pool_timeout: float = 5.0

    # ---- circuit breaker -------------------------------------------------
    breaker_failure_threshold: int = 5
    breaker_recovery_seconds: float = 30.0

    # ---- readiness -------------------------------------------------------
    #: Timeout for one upstream ``/health`` probe during a readiness check.
    upstream_health_timeout: float = 1.5
    #: Readiness probes reuse a snapshot for this long. ``/health/ready`` is polled
    #: every few seconds; without this every poll would fan out to nine services.
    upstream_health_cache_seconds: float = 5.0

    # ---- response cache --------------------------------------------------
    cache_enabled: bool = True
    #: Fallback TTL when a route does not declare one.
    cache_default_ttl: int = 60
    #: Responses larger than this are streamed but never stored — the cache exists
    #: for catalogue JSON, not for book downloads.
    cache_max_body_bytes: int = 1_048_576

    # ---- ban denylist ----------------------------------------------------
    denylist_enabled: bool = True
    #: Existence of the key means "banned". Owned by the auth service; see README.
    denylist_key_template: str = "kos:denylist:user:{user_id}"

    # ---- rate limiting ---------------------------------------------------
    rate_limit_enabled: bool = True

    # ---- aggregated OpenAPI ---------------------------------------------
    openapi_enabled: bool = True
    openapi_cache_ttl: int = 300
    openapi_fetch_timeout: float = 5.0
    #: Swagger UI assets. Pinned by version so the CSP below stays meaningful.
    swagger_ui_js_url: str = (
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui-bundle.js"
    )
    swagger_ui_css_url: str = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui.css"

    # ---- browser-facing security ----------------------------------------
    #: CSP for the HTML documentation page. The JSON API keeps core-py's
    #: ``default-src 'none'`` — only this one HTML page needs to load scripts.
    docs_csp: str = (
        "default-src 'none'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "font-src 'self' data: https://cdn.jsdelivr.net; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )
    #: Response headers browser JavaScript is allowed to read cross-origin. The
    #: core CORS middleware exposes the platform set; these are gateway-specific.
    cors_expose_headers: CsvList = Field(
        default_factory=lambda: [
            "X-Request-ID",
            "X-Trace-ID",
            "X-Cache",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Retry-After",
        ]
    )

    # ---- event-driven cache invalidation --------------------------------
    events_enabled: bool = True
    event_consumer_group: str = "gateway"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def docs_enabled(self) -> bool:
        """Always ``False`` for the gateway.

        This switches off FastAPI's *built-in* ``/docs`` and ``/openapi.json``
        routes. The gateway serves those paths itself with the aggregated
        cross-service specification — its own schema (one catch-all proxy route)
        would be useless to a client.
        """
        return False


settings = Settings()

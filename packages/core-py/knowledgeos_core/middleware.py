"""ASGI middleware: correlation ids, access logs, metrics, body limits, security headers.

Order matters. The stack is installed so that the outermost layer is the one that
must see *every* request including failures:

    RequestContext (ids)  ->  AccessLog+Metrics  ->  BodyLimit  ->  SecurityHeaders  ->  app

Starlette applies ``add_middleware`` in reverse, which :func:`install_middleware`
accounts for.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from .config import ServiceSettings
from .errors import PayloadTooLargeError
from .logging import get_logger, request_id_ctx, trace_id_ctx, user_id_ctx
from .metrics import (
    http_request_body_bytes,
    http_request_duration_seconds,
    http_requests_in_flight,
    http_requests_total,
)

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"

#: Endpoints excluded from access logs and metrics — probes fire every few seconds
#: and would otherwise dominate both the log volume and the latency histogram.
_SILENT_PATHS = frozenset(
    {"/health", "/health/ready", "/health/startup", "/metrics", "/favicon.ico"}
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Establish the correlation id for the request and echo it back.

    The gateway generates the id at the edge and forwards it; internal services
    reuse it. That single value ties a frontend error, a gateway log line, a book
    service query and a Celery task together.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        # Never trust an unbounded client-supplied id into our log index.
        request_id = incoming[:64] if incoming else uuid.uuid4().hex
        trace_id = request.headers.get(TRACE_ID_HEADER, request_id)

        rid_token = request_id_ctx.set(request_id)
        tid_token = trace_id_ctx.set(trace_id)
        uid_token = user_id_ctx.set(None)
        structlog.contextvars.bind_contextvars(request_id=request_id)

        request.state.request_id = request_id
        request.state.trace_id = trace_id
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers[TRACE_ID_HEADER] = trace_id
            return response
        finally:
            request_id_ctx.reset(rid_token)
            trace_id_ctx.reset(tid_token)
            user_id_ctx.reset(uid_token)
            structlog.contextvars.unbind_contextvars("request_id")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """One structured access log line and one metrics observation per request."""

    def __init__(self, app: ASGIApp, *, service_name: str) -> None:
        super().__init__(app)
        self._service = service_name

    @staticmethod
    def _route_template(request: Request) -> str:
        """Collapse ``/v1/books/<uuid>`` to ``/v1/books/{book_id}``.

        Starlette resolves the matching route during dispatch, so by the time we
        record the metric the template is available on ``request.scope``. Unmatched
        requests (404s) are bucketed together to bound cardinality.
        """
        route = request.scope.get("route")
        path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
        return path_format or "<unmatched>"

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path in _SILENT_PATHS:
            return await call_next(request)

        method = request.method
        started = time.perf_counter()
        http_requests_in_flight.labels(service=self._service).inc()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            http_requests_in_flight.labels(service=self._service).dec()
            template = self._route_template(request)
            http_requests_total.labels(
                service=self._service, method=method, path=template, status=str(status_code)
            ).inc()
            http_request_duration_seconds.labels(
                service=self._service, method=method, path=template
            ).observe(elapsed)

            log = logger.info
            if status_code >= 500:
                log = logger.error
            elif status_code >= 400:
                log = logger.warning
            log(
                "http.request",
                method=method,
                path=path,
                route=template,
                status=status_code,
                duration_ms=round(elapsed * 1000, 2),
                client=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent", "")[:200],
            )


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are buffered into memory."""

    def __init__(self, app: ASGIApp, *, max_bytes: int, service_name: str) -> None:
        super().__init__(app)
        self._max = max_bytes
        self._service = service_name

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                size = int(content_length)
            except ValueError:
                size = 0
            if size > self._max:
                err = PayloadTooLargeError(
                    f"Request body exceeds the {self._max} byte limit.",
                    details={"max_bytes": self._max, "received_bytes": size},
                )
                return JSONResponse(status_code=err.status_code, content=err.to_dict())
            if size:
                http_request_body_bytes.labels(
                    service=self._service, method=request.method
                ).observe(size)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply the Helmet-equivalent response headers for API responses.

    The frontend sets its own richer CSP in ``next.config.ts``; these defaults are
    tuned for JSON APIs, where the main risks are MIME sniffing, clickjacking of
    error pages and referrer leakage of signed URLs.
    """

    def __init__(self, app: ASGIApp, *, is_production: bool) -> None:
        super().__init__(app)
        self._is_production = is_production

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = MutableHeaders(scope=None, raw=response.raw_headers)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=(self)"
        )
        # An API serves no markup, so the strictest possible CSP is correct here.
        headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        if self._is_production:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
            )
        # Auth responses must never be cached by an intermediary.
        if request.url.path.startswith(("/v1/auth", "/auth")):
            headers["Cache-Control"] = "no-store, private"
        return response


def install_middleware(app: FastAPI, settings: ServiceSettings) -> None:
    """Install the platform middleware stack in the correct order."""
    # Added last => runs innermost.
    app.add_middleware(SecurityHeadersMiddleware, is_production=settings.is_production)
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=settings.max_request_body_bytes,
        service_name=settings.service_name,
    )
    # Compress JSON payloads above ~1KB. Below that the CPU cost outweighs the win.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_middleware(ObservabilityMiddleware, service_name=settings.service_name)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Request-ID",
                "X-Trace-ID",
                "X-CSRF-Token",
                "Idempotency-Key",
            ],
            expose_headers=[
                REQUEST_ID_HEADER,
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "Retry-After",
            ],
            max_age=600,
        )

    if settings.trusted_hosts and settings.trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

    # Added first => runs outermost, so even rejected requests get a correlation id.
    app.add_middleware(RequestContextMiddleware)

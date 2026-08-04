"""Health, readiness and metrics endpoints.

Three distinct probes, because they answer three different questions:

``GET /health``   — is the process alive? Never touches a dependency. Wire this to
                    the Railway healthcheck and the Kubernetes liveness probe; if it
                    checks Postgres, a database blip restarts every container you own.
``GET /health/ready`` — can this instance serve traffic right now? Checks dependencies
                    and flips to 503 the moment shutdown begins, so load balancers
                    drain the instance before it stops accepting connections.
``GET /health/startup`` — has first-time initialisation finished? Lets slow boots
                    (migrations, model warmup) avoid tripping the liveness probe.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse

from .logging import get_logger
from .metrics import render_metrics

logger = get_logger(__name__)

#: A dependency probe: returns ``{"status": "up"|"down", ...}`` and never raises.
DependencyProbe = Callable[[], Awaitable[dict[str, Any]]]


class HealthState:
    """Mutable liveness/readiness state owned by the app lifespan."""

    def __init__(self) -> None:
        self.started_at: float = time.time()
        self.startup_complete: bool = False
        self.shutting_down: bool = False
        self._probes: dict[str, DependencyProbe] = {}
        #: Dependencies whose failure must NOT remove the instance from the pool.
        self._optional: set[str] = set()

    def register(self, name: str, probe: DependencyProbe, *, required: bool = True) -> None:
        self._probes[name] = probe
        if not required:
            self._optional.add(name)

    def mark_starting(self) -> None:
        """Reset lifecycle state at the beginning of a startup.

        A process normally starts once, but the app object can be started again in
        the same interpreter — most obviously in a test suite that reuses a
        module-level ``app``. Without clearing ``shutting_down`` here, the first
        shutdown latches it and every later readiness probe reports "draining".
        """
        self.shutting_down = False
        self.startup_complete = False
        self.started_at = time.time()

    def mark_started(self) -> None:
        self.startup_complete = True
        logger.info("service.startup_complete", boot_seconds=round(self.uptime, 3))

    def begin_shutdown(self) -> None:
        self.shutting_down = True
        logger.info("service.draining")

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at

    async def check(self) -> tuple[bool, dict[str, Any]]:
        """Run every probe. Returns ``(ready, details)``."""
        results: dict[str, Any] = {}
        ready = True
        for name, probe in self._probes.items():
            try:
                result = await probe()
            except Exception as exc:
                result = {"status": "down", "error": str(exc)}
            results[name] = result
            if result.get("status") != "up" and name not in self._optional:
                ready = False
        return ready, results


def build_health_router(
    *,
    state: HealthState,
    service_name: str,
    version: str,
    environment: str,
    metrics_enabled: bool = True,
) -> APIRouter:
    router = APIRouter(tags=["system"], include_in_schema=False)

    @router.get("/health", summary="Liveness probe")
    async def health() -> JSONResponse:
        # Deliberately dependency-free: this answers "is the process wedged?".
        return JSONResponse(
            {
                "status": "ok",
                "service": service_name,
                "version": version,
                "environment": environment,
                "uptime_seconds": round(state.uptime, 3),
            }
        )

    @router.get("/health/startup", summary="Startup probe")
    async def startup() -> JSONResponse:
        code = status.HTTP_200_OK if state.startup_complete else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(
            {"status": "ok" if state.startup_complete else "starting"}, status_code=code
        )

    @router.get("/health/ready", summary="Readiness probe")
    async def ready() -> JSONResponse:
        if state.shutting_down:
            return JSONResponse(
                {"status": "draining", "service": service_name},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        healthy, details = await state.check()
        healthy = healthy and state.startup_complete
        return JSONResponse(
            {
                "status": "ready" if healthy else "not_ready",
                "service": service_name,
                "version": version,
                "dependencies": details,
            },
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if metrics_enabled:

        @router.get("/metrics", summary="Prometheus metrics")
        async def metrics() -> Response:
            return PlainTextResponse(
                render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8"
            )

    return router

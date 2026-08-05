"""Fanning out to every service and assembling one page.

Three rules, and each one exists because of a specific way admin dashboards fail.

**Every panel fails on its own.** A dashboard that returns a single status for the
whole page goes red because one optional service is restarting, and then nobody can
see the seven panels that are fine. Each panel carries its own status, and a failure
is rendered as an unavailable card next to seven working ones.

**Panels are fetched concurrently, and each is bounded.** Serially, the page takes the
sum of eight services' latencies; concurrently but unbounded, it takes the maximum,
which is the slowest service on its worst day. Both are bounded here — per panel and
for the page as a whole.

**The operator's own token is forwarded.** This is the important one. The alternative
— admin calling siblings under its own HMAC identity — turns this service into a
confused deputy: anyone who can reach an admin endpoint gets data from every service
regardless of what they are allowed to see *in those services*. Forwarding the bearer
token means each service applies its own `require_permission`, and admin grants no
privilege it was not given.

Nothing here computes anything. Revenue is the payment service's number; this module
puts it on a page. The moment it starts deriving a figure, that figure disagrees with
the one the owning service reports, and nobody can say which is right.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from knowledgeos_core import get_logger
from knowledgeos_core.http import ServiceRegistry
from knowledgeos_core.redis import RedisClient
from schemas import Dashboard, HealthBoard, Panel, PanelStatus, ServiceHealth
from settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PanelSpec:
    """Where one card's numbers come from."""

    service: str
    path: str
    params: dict[str, Any] | None = None
    #: Include the window in the request. Not every source is time-windowed — a
    #: catalogue count is a count.
    windowed: bool = True


#: Service -> the endpoint that produces its card. These are the same admin routes an
#: operator could call directly, which is what keeps the numbers here identical to the
#: numbers on the owning service's own screens.
PANEL_SOURCES: dict[str, PanelSpec] = {
    "payment": PanelSpec(service="payment", path="/v1/admin/revenue"),
    "search": PanelSpec(service="search", path="/v1/admin/search/analytics"),
    "notification": PanelSpec(service="notification", path="/v1/admin/notifications/stats"),
    "automation": PanelSpec(service="automation", path="/v1/automation/stats"),
    "workers": PanelSpec(service="workers", path="/v1/admin/workers/health", windowed=False),
    "books": PanelSpec(service="books", path="/v1/admin/books", params={"limit": 1}),
    # The AI panel comes from an internal route rather than an admin one: spend is
    # not something the AI service exposes on a bearer-token endpoint.
    "ai": PanelSpec(service="ai", path="/internal/usage"),
}

#: Panels whose source is HMAC-only. The operator's token is not forwarded to these,
#: so the admin route that serves them carries the permission check instead.
_INTERNAL_PANELS = {"ai"}


class Aggregator:
    def __init__(
        self,
        settings: Settings,
        services: ServiceRegistry | None,
        redis: RedisClient | None = None,
    ) -> None:
        self._settings = settings
        self._services = services
        self._redis = redis

    # ---- dashboard -------------------------------------------------------

    async def dashboard(
        self, *, days: int = 7, token: str | None = None, refresh: bool = False
    ) -> Dashboard:
        """Every panel, concurrently, bounded, cached.

        The cache key includes the window but **not the caller**. Every panel here is
        aggregate platform data with no per-user component, so caching per operator
        would multiply the fan-out by the number of people watching a deploy — which
        is exactly when the load matters.
        """
        cache_key = f"dashboard:{days}"
        if not refresh and self._redis is not None:
            try:
                hit = await self._redis.get_json(cache_key)
                if hit:
                    return Dashboard.model_validate({**hit, "cached": True})
            except Exception as exc:
                logger.warning("admin.dashboard_cache_read_failed", error=str(exc))

        wanted = [name for name in self._settings.panel_services if name in PANEL_SOURCES]
        try:
            panels = await asyncio.wait_for(
                asyncio.gather(*(self._panel(name, days=days, token=token) for name in wanted)),
                timeout=self._settings.dashboard_timeout,
            )
        except TimeoutError:
            # The whole-page ceiling. Reached only when several panels are slow at
            # once; each already has its own budget. Reported as an incomplete
            # dashboard rather than a 504, because a page with no panels is strictly
            # worse than a page with some.
            logger.warning("admin.dashboard_timed_out", services=wanted)
            panels = [
                Panel(service=name, status=PanelStatus.TIMEOUT, error="Dashboard budget exceeded.")
                for name in wanted
            ]

        board = Dashboard(
            generated_at=datetime.now(UTC),
            window_days=days,
            panels=list(panels),
            # `==`, not `is`. `BaseSchema` sets `use_enum_values`, so a validated
            # model's enum field is a plain string — an identity check here is never
            # true and the dashboard reports itself incomplete even when every panel
            # answered. (SQLAlchemy columns are the opposite: they hand back enum
            # members, which is why `is` is correct against a model and wrong here.)
            complete=all(panel.status == PanelStatus.OK for panel in panels),
        )

        if self._redis is not None:
            try:
                await self._redis.set_json(
                    cache_key,
                    board.model_dump(mode="json"),
                    ttl=self._settings.dashboard_cache_ttl,
                )
            except Exception as exc:
                logger.warning("admin.dashboard_cache_write_failed", error=str(exc))
        return board

    async def _panel(self, name: str, *, days: int, token: str | None) -> Panel:
        spec = PANEL_SOURCES[name]
        if self._services is None:
            return Panel(
                service=name,
                status=PanelStatus.UNAVAILABLE,
                error="Service discovery is not configured.",
            )

        params = dict(spec.params or {})
        if spec.windowed:
            params["days"] = days

        # Forwarded so the owning service applies its own permission check. Without
        # it this service is a confused deputy: reachable by anyone admin lets in,
        # and holding an HMAC key that opens everything.
        headers = (
            {"Authorization": f"Bearer {token}"} if token and name not in _INTERNAL_PANELS else None
        )

        started = time.perf_counter()
        try:
            response = await self._services.get(spec.service).request(
                "GET",
                spec.path,
                params=params or None,
                headers=headers,
                timeout=self._settings.panel_timeout,
            )
        except httpx.TimeoutException:
            return Panel(
                service=name,
                status=PanelStatus.TIMEOUT,
                error=f"No response within {self._settings.panel_timeout:.0f}s.",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            logger.info("admin.panel_failed", service=name, error=str(exc))
            return Panel(
                service=name,
                status=PanelStatus.UNAVAILABLE,
                # Never the upstream's message verbatim: this reaches a browser, and
                # an upstream error can carry internal hostnames and query fragments.
                error=f"The {name} service is not reachable.",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code == 403:
            # A real answer: this operator may not see this panel. Distinguished from
            # a broken service so nobody goes looking for an outage.
            return Panel(
                service=name,
                status=PanelStatus.ERROR,
                error="You do not have permission to view this panel.",
                latency_ms=latency,
            )
        if response.status_code >= 400:
            return Panel(
                service=name,
                status=PanelStatus.ERROR,
                error=f"The {name} service returned {response.status_code}.",
                latency_ms=latency,
            )

        return Panel(
            service=name,
            status=PanelStatus.OK,
            data=_flatten(response),
            latency_ms=latency,
        )

    # ---- health board ----------------------------------------------------

    async def health(self, *, refresh: bool = False) -> HealthBoard:
        """Liveness across the platform, from each service's own probe.

        `/health`, not `/health/ready`. Readiness goes red when a dependency is
        briefly slow, which is correct for a load balancer and useless on a status
        board — an operator wants to know which processes are alive, not which ones
        would currently decline traffic.
        """
        if not refresh and self._redis is not None:
            try:
                hit = await self._redis.get_json("health-board")
                if hit:
                    return HealthBoard.model_validate({**hit, "cached": True})
            except Exception as exc:
                logger.warning("admin.health_cache_read_failed", error=str(exc))

        names = self._settings.panel_services
        results = await asyncio.gather(*(self._service_health(name) for name in names))

        board = HealthBoard(
            generated_at=datetime.now(UTC),
            services=list(results),
            healthy=sum(1 for row in results if row.status == "up"),
            degraded=sum(1 for row in results if row.status != "up"),
        )

        if self._redis is not None:
            try:
                await self._redis.set_json(
                    "health-board",
                    board.model_dump(mode="json"),
                    ttl=self._settings.health_cache_ttl,
                )
            except Exception as exc:
                logger.warning("admin.health_cache_write_failed", error=str(exc))
        return board

    async def _service_health(self, name: str) -> ServiceHealth:
        if self._services is None:
            return ServiceHealth(service=name, status="unknown", detail="No discovery.")

        started = time.perf_counter()
        try:
            response = await self._services.get(name).request(
                "GET", "/health", timeout=self._settings.panel_timeout
            )
        except httpx.TimeoutException:
            return ServiceHealth(service=name, status="timeout", detail="No response.")
        except Exception:
            return ServiceHealth(service=name, status="down", detail="Not reachable.")

        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return ServiceHealth(
                service=name,
                status="down",
                latency_ms=latency,
                detail=f"Returned {response.status_code}.",
            )

        try:
            payload = response.json()
        except Exception:
            payload = {}
        return ServiceHealth(
            service=name,
            status=str(payload.get("status") or "up"),
            version=payload.get("version"),
            latency_ms=latency,
        )


def _flatten(response: httpx.Response) -> dict[str, Any]:
    """The panel's data, bounded.

    A dashboard card shows numbers. Passing an upstream's whole response through would
    put an arbitrary payload — including anything that service adds later — into a
    browser, and would make this service's response size a function of somebody else's
    schema.
    """
    try:
        payload = response.json()
    except Exception:
        return {}

    if isinstance(payload, list):
        return {"count": len(payload)}
    if not isinstance(payload, dict):
        return {}

    data: dict[str, Any] = {}
    for key, value in payload.items():
        if (
            isinstance(value, (int, float, bool))
            or value is None
            or (isinstance(value, str) and len(value) <= 200)
        ):
            data[key] = value
        elif isinstance(value, list):
            # Lists become their length. A card does not render a thousand rows, and
            # `total` is what it was going to show anyway.
            data[f"{key}_count"] = len(value)
        elif isinstance(value, dict) and len(value) <= 20:
            data[key] = {
                inner_key: inner
                for inner_key, inner in value.items()
                if isinstance(inner, (int, float, bool, str))
            }
    return data

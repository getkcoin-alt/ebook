"""Configuration for the admin service.

This service is a **reader**. It owns two small tables and otherwise assembles views
out of what other services already expose. Almost every setting here is therefore
about the cost and the failure behaviour of fanning out.

``panel_timeout`` is the one that decides whether the dashboard is usable. Eight
services on one page load means the slowest one sets the page's latency unless each
panel is bounded independently — so it is short, and a panel that misses it is
rendered as unavailable rather than holding the whole page.

``dashboard_cache_ttl`` decides whether the dashboard is a self-inflicted load test.
An admin page that fans out to eight services on every render, with a few operators
leaving it open, produces more internal traffic than the storefront.
"""

from __future__ import annotations

from pydantic import Field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    service_name: str = "admin"
    database_schema: str = "admin"
    port: int = 8008

    jwks_url: str | None = "http://localhost:8001/.well-known/jwks.json"

    # ---- fan-out ---------------------------------------------------------
    #: Per-panel read budget. Short on purpose: the dashboard's job is to load, and a
    #: card that says "unavailable" is far better than a page that hangs.
    panel_timeout: float = 5.0
    #: Whole-dashboard ceiling, in case several panels are slow at once.
    dashboard_timeout: float = 12.0
    #: Services whose panels appear on the dashboard, in display order.
    panels: CsvList = Field(
        default_factory=lambda: [
            "books",
            "payment",
            "search",
            "notification",
            "ai",
            "automation",
            "workers",
        ]
    )

    # ---- caching ---------------------------------------------------------
    #: Short. A dashboard is read far more often than the numbers on it change, but an
    #: operator watching a deploy needs to see movement within a minute.
    dashboard_cache_ttl: int = 60
    #: The health board is cheaper and more urgent than the metrics.
    health_cache_ttl: int = 15

    # ---- feature flags ---------------------------------------------------
    #: How long a sibling service may cache a flag before re-reading it. The lag
    #: between flipping a flag and it taking effect everywhere.
    flag_cache_ttl: int = 30
    #: Flags are audited. Turning payments off is a change somebody has to be able to
    #: attribute later.
    flag_audit_retention_days: int = 365

    events_enabled: bool = True
    event_consumer_group: str = "admin"

    @property
    def panel_services(self) -> list[str]:
        return [name.strip() for name in self.panels if name.strip()]


settings = Settings()

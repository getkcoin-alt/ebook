"""The operator dashboard: aggregated metrics and a service health board."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from deps import Aggregate, CallerToken, WindowDays
from knowledgeos_core import get_logger
from knowledgeos_core.deps import require_permission
from schemas import Dashboard, HealthBoard

logger = get_logger(__name__)

ANALYTICS_READ = Depends(require_permission("analytics:read"))

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get(
    "/dashboard",
    response_model=Dashboard,
    summary="Platform metrics, assembled from every service",
    description=(
        "Each panel comes from the service that owns the number, and **carries its "
        "own status**. A dashboard with one status for the whole page goes red "
        "because one optional service is restarting, and then nobody can see the "
        "seven panels that are fine.\n\n"
        "Your own token is forwarded to each service, so each applies its own "
        "permission check — a panel you may not see comes back as an error on that "
        "card rather than being silently included.\n\n"
        "Cached briefly. An admin page that fans out to eight services on every "
        "render, with a few operators watching a deploy, produces more internal "
        "traffic than the storefront."
    ),
    dependencies=[ANALYTICS_READ],
)
async def dashboard(
    aggregator: Aggregate,
    days: WindowDays,
    token: CallerToken,
    refresh: Annotated[bool, Query(description="Bypass the cache.")] = False,
) -> Dashboard:
    return await aggregator.dashboard(days=days, token=token, refresh=refresh)


@router.get(
    "/health-board",
    response_model=HealthBoard,
    summary="Liveness across every service",
    description=(
        "Reads each service's `/health`, not `/health/ready`. Readiness goes red "
        "when a dependency is briefly slow, which is right for a load balancer and "
        "useless on a status board — an operator wants to know which processes are "
        "alive, not which would currently decline traffic."
    ),
    dependencies=[ANALYTICS_READ],
)
async def health_board(
    aggregator: Aggregate,
    refresh: Annotated[bool, Query(description="Bypass the cache.")] = False,
) -> HealthBoard:
    return await aggregator.health(refresh=refresh)

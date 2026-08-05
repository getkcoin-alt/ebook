"""HTTP surface for the admin service."""

from routers.dashboard import router as dashboard_router
from routers.flags import internal_router
from routers.flags import router as flags_router

__all__ = ["dashboard_router", "flags_router", "internal_router"]

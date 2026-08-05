"""HTTP surface for the workers service."""

from routers.admin import internal_router
from routers.admin import router as admin_router

__all__ = ["admin_router", "internal_router"]

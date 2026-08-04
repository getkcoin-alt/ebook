"""HTTP surface of the notification service."""

from __future__ import annotations

from routers.admin import internal_router
from routers.admin import router as admin_router
from routers.inbox import router as inbox_router

__all__ = ["admin_router", "inbox_router", "internal_router"]

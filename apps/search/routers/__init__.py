"""HTTP surface of the search service."""

from __future__ import annotations

from routers.admin import internal_router
from routers.admin import router as admin_router
from routers.search import router as search_router

__all__ = ["admin_router", "internal_router", "search_router"]

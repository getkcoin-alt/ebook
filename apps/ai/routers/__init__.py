"""HTTP surface of the AI service."""

from __future__ import annotations

from routers.admin import router as admin_router
from routers.assistant import router as assistant_router
from routers.internal import router as internal_router

__all__ = ["admin_router", "assistant_router", "internal_router"]

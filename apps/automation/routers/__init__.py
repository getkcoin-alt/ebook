"""HTTP surface for the automation service."""

from routers.internal import router as internal_router
from routers.jobs import router as jobs_router

__all__ = ["internal_router", "jobs_router"]

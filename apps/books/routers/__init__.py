"""HTTP routers for the book service."""

from routers.admin import entitlements_router, internal_router, moderation_router
from routers.admin import router as admin_router
from routers.catalogue import library_router
from routers.catalogue import router as catalogue_router
from routers.engagement import lists_router, reading_router, reviews_router
from routers.taxonomy import authors_router, categories_router, publishers_router

__all__ = [
    "admin_router",
    "authors_router",
    "catalogue_router",
    "categories_router",
    "entitlements_router",
    "internal_router",
    "library_router",
    "lists_router",
    "moderation_router",
    "publishers_router",
    "reading_router",
    "reviews_router",
]

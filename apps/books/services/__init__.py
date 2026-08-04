"""Domain services for the book service. Routers stay thin; decisions live here."""

from services.cache import CatalogueCache
from services.catalogue import BookFilters, CatalogueService
from services.entitlements import EntitlementService
from services.events import EntitlementEventHandler, register_consumers
from services.lists import ListService
from services.reading import ReadingService
from services.reviews import ReviewService
from services.taxonomy import TaxonomyService

__all__ = [
    "BookFilters",
    "CatalogueCache",
    "CatalogueService",
    "EntitlementEventHandler",
    "EntitlementService",
    "ListService",
    "ReadingService",
    "ReviewService",
    "TaxonomyService",
    "register_consumers",
]

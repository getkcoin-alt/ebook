"""Domain services for the books service.

Routers stay thin: they parse input, call one of these, and shape the response.
Everything that decides something — what a user may download, what a review does to
a book's rating, which slice of the catalogue a cursor points at — lives here, where
it can be tested without an HTTP client.

None of these methods commit. The request-scoped session dependency commits on
success and rolls back on any exception, so a handler cannot leave a half-written
aggregate behind.
"""

from services.cache import CatalogueCache
from services.catalogue import BookFilters, CatalogueService
from services.entitlements import EntitlementService
from services.events import (
    ENTITLEMENT_EVENTS,
    EntitlementEventHandler,
    GrantRequest,
    parse_grant,
    register_consumers,
)
from services.lists import ListService
from services.reading import ReadingService
from services.reviews import ReviewService
from services.taxonomy import TaxonomyService

__all__ = [
    "ENTITLEMENT_EVENTS",
    "BookFilters",
    "CatalogueCache",
    "CatalogueService",
    "EntitlementEventHandler",
    "EntitlementService",
    "GrantRequest",
    "ListService",
    "ReadingService",
    "ReviewService",
    "TaxonomyService",
    "parse_grant",
    "register_consumers",
]

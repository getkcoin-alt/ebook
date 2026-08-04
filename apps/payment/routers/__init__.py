"""HTTP surface of the payment service."""

from __future__ import annotations

from routers.admin import internal_router
from routers.admin import router as admin_router
from routers.billing import affiliates_router, invoices_router, subscriptions_router
from routers.checkout import router as checkout_router
from routers.webhooks import router as webhooks_router

__all__ = [
    "admin_router",
    "affiliates_router",
    "checkout_router",
    "internal_router",
    "invoices_router",
    "subscriptions_router",
    "webhooks_router",
]

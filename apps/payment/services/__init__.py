"""Business logic for the payment service.

Routers translate HTTP; these classes decide what happens to money. Keeping the two
apart is what lets the settlement path be driven from three different entrypoints —
a webhook, a client-side verification, an operator's replay — without any of them
reimplementing it.
"""

from __future__ import annotations

from services.affiliates import AffiliateService
from services.catalogue import CatalogueBook, CatalogueClient
from services.coupons import CouponEvaluation, CouponService
from services.invoices import InvoiceService
from services.orders import OrderService, generate_order_number
from services.payments import PaymentService, SettlementResult
from services.pricing import PricedCart, PricingService
from services.providers import GatewayRegistry
from services.refunds import RefundService
from services.reports import ReportService
from services.subscriptions import SubscriptionService
from services.tax import TaxResult, compute_tax
from services.webhooks import WebhookOutcome, WebhookService

__all__ = [
    "AffiliateService",
    "CatalogueBook",
    "CatalogueClient",
    "CouponEvaluation",
    "CouponService",
    "GatewayRegistry",
    "InvoiceService",
    "OrderService",
    "PaymentService",
    "PricedCart",
    "PricingService",
    "RefundService",
    "ReportService",
    "SettlementResult",
    "SubscriptionService",
    "TaxResult",
    "WebhookOutcome",
    "WebhookService",
    "compute_tax",
    "generate_order_number",
]

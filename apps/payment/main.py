"""Payment service entrypoint.

Orders, gateways, refunds, coupons, invoices, subscriptions and affiliate
commission. The only service allowed to decide that money moved.

It does not grant access to anything. When an order settles it publishes
``payment.succeeded`` and ``order.paid``; the books service consumes those and writes
the entitlement. That split is deliberate — a payment service that also handed out
files would have to know about formats, storage keys and reader permissions, and a
bug in either half would compromise both.
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import (
    admin_router,
    affiliates_router,
    checkout_router,
    internal_router,
    invoices_router,
    subscriptions_router,
    webhooks_router,
)
from services import (
    AffiliateService,
    CatalogueClient,
    CouponService,
    GatewayRegistry,
    InvoiceService,
    OrderService,
    PaymentService,
    PricingService,
    RefundService,
    ReportService,
    SubscriptionService,
    WebhookService,
)
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    """Wire the services once, at startup, and hang them off the app context.

    Constructing them per request would rebuild every httpx client and every circuit
    breaker on every call, which defeats both connection pooling and the breaker's
    memory of recent failures.
    """
    gateways = GatewayRegistry(settings)
    catalogue = CatalogueClient(ctx.services, ctx.redis)
    coupons = CouponService(settings)
    invoices = InvoiceService(settings)
    affiliates = AffiliateService(settings)
    orders = OrderService(settings, coupons, gateways)
    payments = PaymentService(settings, orders, invoices, affiliates)
    refunds = RefundService(settings, orders, gateways, affiliates)
    subscriptions = SubscriptionService(settings)

    ctx.extras.update(
        {
            "gateways": gateways,
            "catalogue": catalogue,
            "coupons": coupons,
            "pricing": PricingService(settings, catalogue, coupons),
            "orders": orders,
            "payments": payments,
            "refunds": refunds,
            "invoices": invoices,
            "subscriptions": subscriptions,
            "affiliates": affiliates,
            "reports": ReportService(settings.default_currency),
            "webhooks": WebhookService(
                settings, gateways, orders, payments, refunds, subscriptions, affiliates
            ),
        }
    )

    if not settings.enabled_providers:
        # Not fatal: free orders still work through the manual gateway, and a
        # deployment mid-setup should boot so its health check passes.
        logger.warning(
            "payment.no_gateway_configured",
            hint="Set RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET or STRIPE_SECRET_KEY.",
        )
    if settings.razorpay_enabled and not settings.razorpay_webhook_secret:
        logger.warning("payment.razorpay_webhook_secret_missing")
    if settings.stripe_enabled and not settings.stripe_webhook_secret:
        logger.warning("payment.stripe_webhook_secret_missing")

    logger.info(
        "payment.ready",
        providers=settings.enabled_providers,
        currency=settings.default_currency,
        gst_percent=settings.gst_percent,
        prices_include_tax=settings.prices_include_tax,
    )


async def _shutdown(ctx: AppContext) -> None:
    gateways = ctx.extras.get("gateways")
    if gateways is not None:
        await gateways.aclose()


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        auth=True,
        events=True,
        # Prices come from the books service over a signed internal call, never from
        # the client and never from the books schema directly.
        service_clients=True,
    ),
    routers=[
        checkout_router,
        invoices_router,
        subscriptions_router,
        affiliates_router,
        webhooks_router,
        admin_router,
        internal_router,
    ],
    on_startup=[_bootstrap],
    on_shutdown=[_shutdown],
    description=(
        "Orders, checkout, Razorpay and Stripe gateways, refunds, coupons, GST "
        "invoicing, subscriptions and affiliate commission."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

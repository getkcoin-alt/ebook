"""Configuration for the payment service.

Provider credentials and webhook secrets live here. A missing webhook secret is not
a soft failure: without it the service cannot prove a callback came from the
provider, so signature verification refuses rather than trusting the payload.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    service_name: str = "payment"
    database_schema: str = "payment"
    port: int = 8005

    # ---- currency and tax ----------------------------------------------
    default_currency: str = "INR"
    #: GST on digital goods in India. A legal figure — verify it before invoicing.
    gst_percent: int = 18
    #: Seller's state code. Intra-state sales split into CGST+SGST; inter-state is
    #: a single IGST line. Getting this wrong misfiles the tax, not just the label.
    seller_state_code: str = "GJ"
    #: True when catalogue prices already include GST — the Indian retail norm, and
    #: what a customer expects when the page says ₹499. The tax is then *extracted*
    #: from the price rather than added on top, so the total the customer agreed to
    #: is the total they are charged.
    prices_include_tax: bool = True
    invoice_prefix: str = "KOS"
    seller_legal_name: str = "KnowledgeOS"
    seller_gstin: str | None = None

    # ---- Razorpay -------------------------------------------------------
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    #: Verifies webhook authenticity. Without it, anyone who learns the URL can
    #: mark an order paid.
    razorpay_webhook_secret: str | None = None

    # ---- Stripe ---------------------------------------------------------
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None
    #: Stripe signatures older than this are rejected as replays.
    stripe_signature_tolerance: int = 300

    # ---- behaviour ------------------------------------------------------
    #: How long a created order may sit unpaid before it is expired by cleanup.
    order_ttl_seconds: int = 3600
    #: Idempotency-Key retention. Matches common provider replay windows.
    idempotency_ttl: int = 86_400
    #: Cap on line items, so a crafted cart cannot make one order unbounded.
    max_order_items: int = 50
    #: Affiliate commission in basis points (500 = 5%).
    affiliate_commission_bps: int = 500
    #: How long after payment a conversion stays ``pending``. Paying commission
    #: before the refund window closes means clawing it back later.
    affiliate_hold_days: int = 14

    #: Return URLs are echoed to the browser by the gateway, so an open redirect
    #: here is a phishing vector. Only these origins are accepted.
    allowed_return_origins: CsvList = Field(default_factory=lambda: ["http://localhost:3000"])

    provider_http_timeout: float = 20.0
    max_webhook_body_bytes: int = 1024 * 1024

    # ---- events ---------------------------------------------------------
    events_enabled: bool = True
    event_consumer_group: str = "payment"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def razorpay_enabled(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def enabled_providers(self) -> list[str]:
        providers = []
        if self.razorpay_enabled:
            providers.append("razorpay")
        if self.stripe_enabled:
            providers.append("stripe")
        return providers

    @property
    def default_provider(self) -> str | None:
        """First configured gateway. Razorpay wins when both are present, because
        the platform prices in INR and Razorpay settles it without conversion."""
        providers = self.enabled_providers
        return providers[0] if providers else None


settings = Settings()

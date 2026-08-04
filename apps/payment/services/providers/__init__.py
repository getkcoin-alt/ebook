"""Provider registry.

One place decides which gateway handles a given order, so no router ever writes
``if provider == "razorpay"``. A deployment with no credentials configured still
boots and still serves free orders through the manual gateway — a missing Stripe key
should degrade checkout, not prevent the service from starting.
"""

from __future__ import annotations

from knowledgeos_core import BadRequestError, PaymentProvider, ServiceUnavailableError, get_logger
from services.providers.base import (
    Gateway,
    NormalisedEvent,
    ProviderOrder,
    ProviderRefund,
    constant_time_equals,
    hmac_sha256_hex,
)
from services.providers.manual import ManualGateway
from services.providers.razorpay import RazorpayGateway
from services.providers.stripe import StripeGateway
from settings import Settings

logger = get_logger(__name__)

__all__ = [
    "Gateway",
    "GatewayRegistry",
    "ManualGateway",
    "NormalisedEvent",
    "ProviderOrder",
    "ProviderRefund",
    "RazorpayGateway",
    "StripeGateway",
    "constant_time_equals",
    "hmac_sha256_hex",
]


class GatewayRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._gateways: dict[PaymentProvider, Gateway] = {
            PaymentProvider.MANUAL: ManualGateway(settings),
        }
        # Clients are built eagerly for configured providers so the first checkout
        # of the day does not pay for connection setup.
        if settings.razorpay_enabled:
            self._gateways[PaymentProvider.RAZORPAY] = RazorpayGateway(settings)
        if settings.stripe_enabled:
            self._gateways[PaymentProvider.STRIPE] = StripeGateway(settings)
        logger.info(
            "payment.gateways_ready",
            providers=[p.value for p in self._gateways],
            default=settings.default_provider,
        )

    def __contains__(self, provider: object) -> bool:
        return provider in self._gateways

    def get(self, provider: PaymentProvider | str) -> Gateway:
        try:
            key = PaymentProvider(provider)
        except ValueError as exc:
            raise BadRequestError(
                f"Unknown payment provider '{provider}'.", code="unknown_provider"
            ) from exc
        gateway = self._gateways.get(key)
        if gateway is None:
            raise ServiceUnavailableError(
                f"The {key.value} gateway is not configured on this deployment.",
                details={"provider": key.value, "available": self.available},
            )
        return gateway

    def resolve(self, requested: PaymentProvider | str | None, *, amount_minor: int) -> Gateway:
        """Pick the gateway for an order.

        A zero-value order goes to the manual gateway regardless of what the client
        asked for: no card network will accept a ₹0 charge, and the order still has
        to complete so entitlements are granted through the normal path.
        """
        if amount_minor <= 0:
            return self._gateways[PaymentProvider.MANUAL]
        if requested is not None:
            return self.get(requested)
        default = self._settings.default_provider
        if default is None:
            raise ServiceUnavailableError(
                "No payment gateway is configured on this deployment.",
                details={"hint": "Set RAZORPAY_KEY_ID/SECRET or STRIPE_SECRET_KEY."},
            )
        return self.get(default)

    @property
    def available(self) -> list[str]:
        return [provider.value for provider in self._gateways]

    @property
    def public_providers(self) -> list[PaymentProvider]:
        """Gateways a customer may actually choose. Manual is staff-only."""
        return [p for p in self._gateways if p is not PaymentProvider.MANUAL]

    async def aclose(self) -> None:
        for gateway in self._gateways.values():
            await gateway.aclose()

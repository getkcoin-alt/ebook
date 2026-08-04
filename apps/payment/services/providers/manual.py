"""The no-gateway gateway.

Used for two real cases, not as a stub:

* **Zero-value orders.** A 100%-off coupon or an all-free cart still has to become a
  paid order so entitlements are granted through exactly the same path as a real
  purchase. Routing it through a card network for ₹0 would fail, and special-casing
  free orders elsewhere in the flow would create a second, less-tested way to grant
  access.
* **Offline settlement.** Bank transfers and institutional invoices, marked paid by
  an administrator. The audit trail is the admin's user id on the payment row.

It never contacts anything, and it cannot verify a webhook — there is no counterparty
to verify. ``verify_webhook`` returns False, so a request that somehow reaches a
manual webhook route is rejected rather than trusted.
"""

from __future__ import annotations

import uuid
from typing import Any

from knowledgeos_core import PaymentProvider
from schemas import PaymentStatus
from services.providers.base import NormalisedEvent, ProviderOrder, ProviderRefund
from settings import Settings


class ManualGateway:
    name = PaymentProvider.MANUAL

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return True

    async def create_order(
        self,
        *,
        order_id: uuid.UUID,
        order_number: str,
        amount_minor: int,
        currency: str,
        customer_email: str | None,
        return_url: str | None,
        description: str,
    ) -> ProviderOrder:
        # The provider reference is derived from our own order id so the unique
        # constraint on (provider, provider_payment_id) still does its job.
        return ProviderOrder(provider_order_id=f"manual_{order_id}")

    def verify_webhook(self, *, body: bytes, headers: dict[str, str]) -> bool:
        return False

    def parse_webhook(self, payload: dict[str, Any], headers: dict[str, str]) -> NormalisedEvent:
        return NormalisedEvent(
            event_id=str(payload.get("id", "")),
            event_type="manual.unknown",
            status=PaymentStatus.PENDING,
            raw=payload,
        )

    async def refund(
        self,
        *,
        provider_payment_id: str,
        amount_minor: int,
        currency: str,
        reason: str | None,
        idempotency_key: str,
    ) -> ProviderRefund:
        """Records the intent. Moving the money is a manual bank operation."""
        return ProviderRefund(
            provider_refund_id=f"manual_rfnd_{idempotency_key}", status="succeeded"
        )

    async def aclose(self) -> None:
        return None

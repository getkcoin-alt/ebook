"""The gateway abstraction.

Every provider implements the same four operations — create an order, verify a
callback signature, normalise a webhook, issue a refund — so the rest of the service
never branches on which gateway is in use. Adding a third provider is a new file
here plus one registry entry, not a change to any router.

**Signature verification is not optional and never soft-fails.** A webhook endpoint
is a public URL that changes order state and grants access to paid files. If the
secret is missing, verification returns False; it does not fall through to trusting
the payload. That distinction is the difference between a payment system and a
free-books API for anyone who can guess the URL.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from knowledgeos_core import PaymentProvider
from schemas import PaymentStatus


@dataclass(slots=True)
class ProviderOrder:
    """What a gateway hands back when an order is opened on its side."""

    provider_order_id: str
    checkout_url: str | None = None
    client_secret: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderRefund:
    provider_refund_id: str | None
    status: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalisedEvent:
    """A webhook flattened into the shape this service reasons about.

    Providers disagree on almost everything — event naming, nesting, whether the
    amount is in minor units. Normalising once here keeps that mess out of the
    handlers, which then only see: what happened, to which order, for how much.
    """

    event_id: str
    event_type: str
    #: One of ``payment``, ``refund``, ``subscription`` or ``unknown``.
    category: str = "unknown"
    provider_order_id: str | None = None
    provider_payment_id: str | None = None
    provider_refund_id: str | None = None
    provider_subscription_id: str | None = None
    #: Our own order id, when the gateway echoed it back in its metadata.
    order_id: uuid.UUID | None = None
    status: PaymentStatus | None = None
    amount_minor: int | None = None
    currency: str | None = None
    method: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def constant_time_equals(left: str, right: str) -> bool:
    """Compare signatures without leaking their contents through timing.

    A naive ``==`` returns as soon as two bytes differ, which over enough requests
    lets an attacker recover a valid signature one byte at a time.
    """
    return hmac.compare_digest(left.encode(), right.encode())


def hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class Gateway(Protocol):
    """The contract every provider implements."""

    name: PaymentProvider

    @property
    def configured(self) -> bool:
        """False when credentials are absent, so routes can 503 rather than 500."""
        ...

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
    ) -> ProviderOrder: ...

    def verify_webhook(self, *, body: bytes, headers: dict[str, str]) -> bool: ...

    def parse_webhook(
        self, payload: dict[str, Any], headers: dict[str, str]
    ) -> NormalisedEvent: ...

    async def refund(
        self,
        *,
        provider_payment_id: str,
        amount_minor: int,
        currency: str,
        reason: str | None,
        idempotency_key: str,
    ) -> ProviderRefund: ...

    async def aclose(self) -> None: ...

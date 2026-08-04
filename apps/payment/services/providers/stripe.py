"""Stripe gateway.

Stripe's API is form-encoded, not JSON, and it nests parameters with bracket syntax
(``metadata[order_id]=...``). That is why this file flattens payloads by hand rather
than posting ``json=``; sending JSON to Stripe silently produces an empty request.

Stripe's webhook signature is a scheme worth understanding rather than copying:

    Stripe-Signature: t=1710000000,v1=<hex>,v1=<hex>

The signed message is ``"<t>.<raw_body>"``. The timestamp is *inside* the signed
payload, which is what makes a replay detectable — an attacker who captures a valid
webhook cannot change ``t`` without invalidating the signature, so rejecting old
timestamps rejects replays. Several ``v1`` values may appear during a secret
rotation, and any one matching is a pass.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from knowledgeos_core import PaymentProvider, UpstreamError, get_logger
from schemas import PaymentStatus
from services.providers.base import (
    NormalisedEvent,
    ProviderOrder,
    ProviderRefund,
    constant_time_equals,
    hmac_sha256_hex,
)
from settings import Settings

logger = get_logger(__name__)

API_BASE = "https://api.stripe.com/v1"

_PAYMENT_STATUS = {
    "requires_payment_method": PaymentStatus.PENDING,
    "requires_confirmation": PaymentStatus.PENDING,
    "requires_action": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
    "requires_capture": PaymentStatus.AUTHORIZED,
    "succeeded": PaymentStatus.CAPTURED,
    "canceled": PaymentStatus.FAILED,
}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Turn a nested dict into Stripe's bracketed form encoding."""
    flat: dict[str, str] = {}
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    flat.update(_flatten(item, f"{name}[{index}]"))
                else:
                    flat[f"{name}[{index}]"] = str(item)
        elif isinstance(value, bool):
            flat[name] = "true" if value else "false"
        elif value is not None:
            flat[name] = str(value)
    return flat


class StripeGateway:
    name = PaymentProvider.STRIPE

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(settings.provider_http_timeout, connect=5.0),
            headers={
                "Authorization": f"Bearer {settings.stripe_secret_key or ''}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "knowledgeos-payment/1.0",
                # Pinning the version means a Stripe-side upgrade cannot change our
                # webhook shapes underneath a running deployment.
                "Stripe-Version": "2024-06-20",
            },
        )

    @property
    def configured(self) -> bool:
        return self._settings.stripe_enabled

    async def _post(
        self, path: str, data: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = await self._client.post(path, data=_flatten(data), headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError("Could not reach Stripe.", details={"upstream": "stripe"}) from exc
        if response.is_success:
            return response.json()  # type: ignore[no-any-return]

        try:
            error = response.json().get("error", {})
            message = error.get("message") or "Stripe rejected the request."
            code = error.get("code") or "stripe_error"
        except Exception:
            message, code = "Stripe rejected the request.", "stripe_error"
        logger.warning(
            "stripe.request_failed", path=path, status=response.status_code, message=message
        )
        raise UpstreamError(
            message, code=str(code).lower(), status_code=502, details={"upstream": "stripe"}
        )

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
        """Open a Checkout Session.

        Checkout rather than a bare PaymentIntent, because it brings Strong Customer
        Authentication, local payment methods and a hosted page that stays PCI-compliant
        without this service ever seeing a card number.
        """
        base = str(return_url or self._settings.frontend_url).rstrip("/")
        payload: dict[str, Any] = {
            "mode": "payment",
            "client_reference_id": str(order_id),
            "success_url": f"{base}/checkout/success?order={order_number}",
            "cancel_url": f"{base}/checkout/cancelled?order={order_number}",
            "metadata": {"order_id": str(order_id), "order_number": order_number},
            "payment_intent_data": {"metadata": {"order_id": str(order_id)}},
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency.lower(),
                        # Stripe wants the currency's minor unit, which is exactly how
                        # every amount is stored on this platform. No conversion.
                        "unit_amount": amount_minor,
                        "product_data": {"name": description[:250] or order_number},
                    },
                }
            ],
        }
        if customer_email:
            payload["customer_email"] = customer_email

        data = await self._post("/checkout/sessions", payload, idempotency_key=f"order-{order_id}")
        return ProviderOrder(
            provider_order_id=str(data["id"]),
            checkout_url=data.get("url"),
            client_secret=data.get("client_secret"),
            raw=data,
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
        payload: dict[str, Any] = {
            "payment_intent": provider_payment_id,
            "amount": amount_minor,
            "metadata": {"reason": (reason or "")[:250]},
        }
        data = await self._post("/refunds", payload, idempotency_key=idempotency_key)
        return ProviderRefund(
            provider_refund_id=str(data.get("id")) if data.get("id") else None,
            status=str(data.get("status", "pending")),
            raw=data,
        )

    # ---- signatures -----------------------------------------------------

    def verify_webhook(self, *, body: bytes, headers: dict[str, str]) -> bool:
        secret = self._settings.stripe_webhook_secret
        header = headers.get("stripe-signature", "")
        if not secret or not header:
            logger.warning("stripe.webhook_unverifiable", has_secret=bool(secret))
            return False

        timestamp: str | None = None
        signatures: list[str] = []
        for part in header.split(","):
            key, _, value = part.strip().partition("=")
            if key == "t":
                timestamp = value
            elif key == "v1":
                signatures.append(value)
        if not timestamp or not signatures:
            return False

        try:
            age = abs(time.time() - int(timestamp))
        except ValueError:
            return False
        if age > self._settings.stripe_signature_tolerance:
            # An old-but-validly-signed webhook is a captured request being replayed.
            logger.warning("stripe.webhook_stale", age_seconds=int(age))
            return False

        expected = hmac_sha256_hex(secret, f"{timestamp}.".encode() + body)
        return any(constant_time_equals(candidate, expected) for candidate in signatures)

    # ---- webhook parsing ------------------------------------------------

    def parse_webhook(self, payload: dict[str, Any], headers: dict[str, str]) -> NormalisedEvent:
        event_type = str(payload.get("type", "unknown"))
        event_id = str(payload.get("id", ""))
        obj = (payload.get("data") or {}).get("object") or {}
        metadata = obj.get("metadata") or {}
        order_id = _maybe_uuid(metadata.get("order_id") or obj.get("client_reference_id"))
        currency = (obj.get("currency") or "").upper() or None

        if event_type.startswith("charge.refund") or event_type.startswith("refund."):
            return NormalisedEvent(
                event_id=event_id,
                event_type=event_type,
                category="refund",
                provider_refund_id=str(obj.get("id")) if obj.get("id") else None,
                provider_payment_id=str(obj.get("payment_intent"))
                if obj.get("payment_intent")
                else None,
                amount_minor=_int_or_none(obj.get("amount")),
                currency=currency,
                order_id=order_id,
                raw=payload,
            )

        if event_type.startswith("customer.subscription"):
            return NormalisedEvent(
                event_id=event_id,
                event_type=event_type,
                category="subscription",
                provider_subscription_id=str(obj.get("id")) if obj.get("id") else None,
                raw=payload,
            )

        if event_type.startswith(("checkout.session", "payment_intent.")):
            # A completed Checkout Session carries the session id, but the money is
            # attached to the PaymentIntent — that is the id a refund needs later.
            payment_id = obj.get("payment_intent") or obj.get("id")
            amount = obj.get("amount_total") or obj.get("amount_received") or obj.get("amount")
            status = _PAYMENT_STATUS.get(str(obj.get("status", "")))
            if event_type == "checkout.session.completed":
                status = (
                    PaymentStatus.CAPTURED
                    if obj.get("payment_status") == "paid"
                    else PaymentStatus.PENDING
                )
            error = obj.get("last_payment_error") or {}
            return NormalisedEvent(
                event_id=event_id,
                event_type=event_type,
                category="payment",
                provider_order_id=str(obj.get("id")) if obj.get("id") else None,
                provider_payment_id=str(payment_id) if payment_id else None,
                order_id=order_id,
                status=status,
                amount_minor=_int_or_none(amount),
                currency=currency,
                method=(obj.get("payment_method_types") or [None])[0],
                failure_code=error.get("code"),
                failure_message=error.get("message"),
                raw=payload,
            )

        return NormalisedEvent(event_id=event_id, event_type=event_type, raw=payload)

    async def aclose(self) -> None:
        await self._client.aclose()


def _maybe_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

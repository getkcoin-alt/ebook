"""Razorpay gateway.

Talks to Razorpay's REST API over httpx with HTTP basic auth (key id as username,
key secret as password). No SDK: the surface we use is four endpoints, and a vendor
SDK would pull in its own HTTP stack, its own retry policy and its own opinions
about logging, none of which match the platform's.

Razorpay works in the currency's minor unit natively, so no conversion is needed —
which is one fewer place to lose a factor of a hundred.

Two signature schemes, and they are not interchangeable:

* **Checkout callback**: ``HMAC-SHA256(key_secret, "<order_id>|<payment_id>")``.
  Signed with the *API secret*.
* **Webhook**: ``HMAC-SHA256(webhook_secret, raw_body)`` in ``X-Razorpay-Signature``.
  Signed with a *separate webhook secret* configured in the dashboard.

Using one secret for the other check is a real and easy mistake; it fails closed
here, which is the right direction to fail.
"""

from __future__ import annotations

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

API_BASE = "https://api.razorpay.com/v1"

#: Razorpay's payment states mapped onto ours.
_PAYMENT_STATUS = {
    "created": PaymentStatus.PENDING,
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "refunded": PaymentStatus.REFUNDED,
    "failed": PaymentStatus.FAILED,
}


class RazorpayGateway:
    name = PaymentProvider.RAZORPAY

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(settings.provider_http_timeout, connect=5.0),
            auth=(
                httpx.BasicAuth(settings.razorpay_key_id or "", settings.razorpay_key_secret or "")
                if settings.razorpay_key_id
                else None
            ),
            headers={"User-Agent": "knowledgeos-payment/1.0"},
        )

    @property
    def configured(self) -> bool:
        return self._settings.razorpay_enabled

    # ---- API ------------------------------------------------------------

    async def _post(self, path: str, json: dict[str, Any], *, idempotency_key: str | None = None):
        # Razorpay honours this header on refunds: a retried call with the same key
        # returns the original refund instead of issuing a second one.
        headers = {"X-Razorpay-Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = await self._client.post(path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "Could not reach Razorpay.", details={"upstream": "razorpay"}
            ) from exc
        if response.is_success:
            return response.json()

        # Surface Razorpay's own message: "amount must be at least 100" is far more
        # useful to a developer than a generic gateway failure.
        try:
            error = response.json().get("error", {})
            message = error.get("description") or "Razorpay rejected the request."
            code = error.get("code", "razorpay_error")
        except Exception:
            message, code = "Razorpay rejected the request.", "razorpay_error"
        logger.warning(
            "razorpay.request_failed", path=path, status=response.status_code, message=message
        )
        raise UpstreamError(
            message,
            code=str(code).lower(),
            status_code=502,
            details={"upstream": "razorpay"},
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
        payload = {
            "amount": amount_minor,
            "currency": currency.upper(),
            # Razorpay enforces uniqueness on receipt within a short window, so our
            # order number doubles as the idempotency handle for a retried create.
            "receipt": order_number[:40],
            "notes": {
                "order_id": str(order_id),
                "order_number": order_number,
                "description": description[:250],
            },
        }
        data = await self._post("/orders", payload)
        return ProviderOrder(provider_order_id=str(data["id"]), raw=data)

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
            "amount": amount_minor,
            # "optimum" lets Razorpay choose instant refund when the customer's bank
            # supports it and fall back to the normal rail when it does not.
            "speed": "optimum",
            "notes": {"reason": (reason or "")[:250], "idempotency_key": idempotency_key},
        }
        data = await self._post(
            f"/payments/{provider_payment_id}/refund", payload, idempotency_key=idempotency_key
        )
        return ProviderRefund(
            provider_refund_id=str(data.get("id")) if data.get("id") else None,
            status=str(data.get("status", "pending")),
            raw=data,
        )

    # ---- signatures -----------------------------------------------------

    def verify_webhook(self, *, body: bytes, headers: dict[str, str]) -> bool:
        secret = self._settings.razorpay_webhook_secret
        signature = headers.get("x-razorpay-signature", "")
        if not secret or not signature:
            # No secret configured means we cannot prove anything about this
            # request. Refuse rather than trust it.
            logger.warning("razorpay.webhook_unverifiable", has_secret=bool(secret))
            return False
        return constant_time_equals(signature, hmac_sha256_hex(secret, body))

    def verify_checkout(
        self, *, provider_order_id: str, provider_payment_id: str, signature: str
    ) -> bool:
        """Verify the payload the Razorpay Checkout modal hands to the browser.

        Signed with the API secret over ``order_id|payment_id`` — a different secret
        and a different message from the webhook check above.
        """
        secret = self._settings.razorpay_key_secret
        if not secret or not signature:
            return False
        message = f"{provider_order_id}|{provider_payment_id}".encode()
        return constant_time_equals(signature, hmac_sha256_hex(secret, message))

    # ---- webhook parsing ------------------------------------------------

    def parse_webhook(self, payload: dict[str, Any], headers: dict[str, str]) -> NormalisedEvent:
        event_type = str(payload.get("event", "unknown"))
        entities = payload.get("payload", {}) or {}
        payment = (entities.get("payment", {}) or {}).get("entity", {}) or {}
        refund = (entities.get("refund", {}) or {}).get("entity", {}) or {}
        subscription = (entities.get("subscription", {}) or {}).get("entity", {}) or {}

        # Razorpay does not send an event id in the body; the delivery id header is
        # the only stable handle for deduplication. Falling back to a composite key
        # keeps the unique constraint meaningful when the header is absent.
        event_id = headers.get("x-razorpay-event-id") or (
            f"{event_type}:{payment.get('id') or refund.get('id') or subscription.get('id') or ''}"
        )

        notes = payment.get("notes") or refund.get("notes") or {}
        order_id = _maybe_uuid(notes.get("order_id"))

        if event_type.startswith("refund."):
            return NormalisedEvent(
                event_id=event_id,
                event_type=event_type,
                category="refund",
                provider_refund_id=str(refund.get("id")) if refund.get("id") else None,
                provider_payment_id=str(refund.get("payment_id"))
                if refund.get("payment_id")
                else None,
                amount_minor=_int_or_none(refund.get("amount")),
                currency=refund.get("currency"),
                order_id=order_id,
                raw=payload,
            )

        if event_type.startswith("subscription."):
            return NormalisedEvent(
                event_id=event_id,
                event_type=event_type,
                category="subscription",
                provider_subscription_id=str(subscription.get("id"))
                if subscription.get("id")
                else None,
                raw=payload,
            )

        if event_type.startswith(("payment.", "order.")):
            return NormalisedEvent(
                event_id=event_id,
                event_type=event_type,
                category="payment",
                provider_order_id=str(payment.get("order_id")) if payment.get("order_id") else None,
                provider_payment_id=str(payment.get("id")) if payment.get("id") else None,
                order_id=order_id,
                status=_PAYMENT_STATUS.get(str(payment.get("status", ""))),
                amount_minor=_int_or_none(payment.get("amount")),
                currency=payment.get("currency"),
                method=payment.get("method"),
                failure_code=payment.get("error_code"),
                failure_message=payment.get("error_description"),
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

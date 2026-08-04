"""Webhook and callback signature verification.

These run against the **real** gateway implementations, not stubs. Stubbing
signature verification would leave the one control that stands between a public URL
and free access to every paid file completely untested.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import orjson
import pytest

from services.providers.base import constant_time_equals, hmac_sha256_hex
from services.providers.razorpay import RazorpayGateway
from services.providers.stripe import StripeGateway
from tests.conftest import RAZORPAY_SECRET, RAZORPAY_WEBHOOK_SECRET, STRIPE_WEBHOOK_SECRET


def _sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


@pytest.fixture
def razorpay(settings) -> RazorpayGateway:
    return RazorpayGateway(settings)


@pytest.fixture
def stripe(settings) -> StripeGateway:
    return StripeGateway(settings)


# ---------------------------------------------------------------------------
# Razorpay
# ---------------------------------------------------------------------------


def test_razorpay_accepts_a_correctly_signed_webhook(razorpay):
    body = orjson.dumps({"event": "payment.captured"})
    headers = {"x-razorpay-signature": _sign(RAZORPAY_WEBHOOK_SECRET, body)}
    assert razorpay.verify_webhook(body=body, headers=headers) is True


def test_razorpay_rejects_a_tampered_body(razorpay):
    body = orjson.dumps({"event": "payment.captured", "amount": 100})
    headers = {"x-razorpay-signature": _sign(RAZORPAY_WEBHOOK_SECRET, body)}
    tampered = orjson.dumps({"event": "payment.captured", "amount": 999_999})
    assert razorpay.verify_webhook(body=tampered, headers=headers) is False


def test_razorpay_rejects_a_signature_made_with_the_api_secret(razorpay):
    """The webhook secret and the API secret are different values.

    Signing a webhook with the API secret is an easy mistake to make, and it has to
    fail closed rather than be quietly accepted.
    """
    body = orjson.dumps({"event": "payment.captured"})
    headers = {"x-razorpay-signature": _sign(RAZORPAY_SECRET, body)}
    assert razorpay.verify_webhook(body=body, headers=headers) is False


def test_razorpay_refuses_when_no_webhook_secret_is_configured(settings, monkeypatch):
    """Without a secret there is nothing to verify against, so nothing is trusted."""
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None)
    gateway = RazorpayGateway(settings)
    body = orjson.dumps({"event": "payment.captured"})
    assert gateway.verify_webhook(body=body, headers={"x-razorpay-signature": "anything"}) is False


def test_razorpay_refuses_a_missing_signature_header(razorpay):
    assert razorpay.verify_webhook(body=b"{}", headers={}) is False


def test_razorpay_checkout_signature_is_over_order_and_payment_ids(razorpay):
    message = b"order_ABC|pay_XYZ"
    assert (
        razorpay.verify_checkout(
            provider_order_id="order_ABC",
            provider_payment_id="pay_XYZ",
            signature=_sign(RAZORPAY_SECRET, message),
        )
        is True
    )


def test_razorpay_checkout_signature_does_not_transfer_between_payments(razorpay):
    """A signature valid for one payment must not validate a different one."""
    signature = _sign(RAZORPAY_SECRET, b"order_ABC|pay_XYZ")
    assert (
        razorpay.verify_checkout(
            provider_order_id="order_ABC",
            provider_payment_id="pay_SOMEONE_ELSE",
            signature=signature,
        )
        is False
    )


def test_razorpay_parses_a_captured_payment(razorpay):
    payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_123",
                    "order_id": "order_123",
                    "status": "captured",
                    "amount": 49900,
                    "currency": "INR",
                    "method": "upi",
                    "notes": {"order_id": "0f9d4a3e-0000-4000-8000-000000000001"},
                }
            }
        },
    }
    event = razorpay.parse_webhook(payload, {"x-razorpay-event-id": "evt_1"})
    assert event.category == "payment"
    assert event.provider_payment_id == "pay_123"
    assert event.provider_order_id == "order_123"
    assert event.amount_minor == 49900
    assert str(event.order_id) == "0f9d4a3e-0000-4000-8000-000000000001"


def test_razorpay_parses_a_refund(razorpay):
    payload = {
        "event": "refund.processed",
        "payload": {
            "refund": {
                "entity": {"id": "rfnd_1", "payment_id": "pay_1", "amount": 1000, "currency": "INR"}
            }
        },
    }
    event = razorpay.parse_webhook(payload, {})
    assert event.category == "refund"
    assert event.provider_refund_id == "rfnd_1"


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------


def _stripe_header(body: bytes, secret: str, timestamp: int | None = None) -> str:
    timestamp = timestamp or int(time.time())
    signature = _sign(secret, f"{timestamp}.".encode() + body)
    return f"t={timestamp},v1={signature}"


def test_stripe_accepts_a_correctly_signed_webhook(stripe):
    body = orjson.dumps({"id": "evt_1", "type": "checkout.session.completed"})
    headers = {"stripe-signature": _stripe_header(body, STRIPE_WEBHOOK_SECRET)}
    assert stripe.verify_webhook(body=body, headers=headers) is True


def test_stripe_rejects_a_replayed_delivery(stripe):
    """The timestamp is inside the signed message, so an old signature is a replay.

    An attacker who captures a valid webhook cannot move it forward in time without
    invalidating the signature — which is exactly what makes the age check work.
    """
    body = orjson.dumps({"id": "evt_1", "type": "checkout.session.completed"})
    stale = int(time.time()) - 4000
    headers = {"stripe-signature": _stripe_header(body, STRIPE_WEBHOOK_SECRET, stale)}
    assert stripe.verify_webhook(body=body, headers=headers) is False


def test_stripe_accepts_any_matching_v1_during_a_secret_rotation(stripe):
    """Stripe sends several v1 values while a secret is being rotated."""
    body = orjson.dumps({"id": "evt_1"})
    timestamp = int(time.time())
    good = _sign(STRIPE_WEBHOOK_SECRET, f"{timestamp}.".encode() + body)
    headers = {"stripe-signature": f"t={timestamp},v1=deadbeef,v1={good}"}
    assert stripe.verify_webhook(body=body, headers=headers) is True


def test_stripe_rejects_a_tampered_body(stripe):
    body = orjson.dumps({"id": "evt_1", "amount": 100})
    headers = {"stripe-signature": _stripe_header(body, STRIPE_WEBHOOK_SECRET)}
    assert (
        stripe.verify_webhook(body=orjson.dumps({"id": "evt_1", "amount": 1}), headers=headers)
        is False
    )


def test_stripe_rejects_a_malformed_signature_header(stripe):
    body = orjson.dumps({"id": "evt_1"})
    assert stripe.verify_webhook(body=body, headers={"stripe-signature": "garbage"}) is False


def test_stripe_refuses_when_no_webhook_secret_is_configured(settings, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", None)
    gateway = StripeGateway(settings)
    body = orjson.dumps({"id": "evt_1"})
    assert gateway.verify_webhook(body=body, headers={"stripe-signature": "t=1,v1=x"}) is False


def test_stripe_completed_checkout_maps_to_the_payment_intent(stripe):
    """The session id is not the id a refund needs — the PaymentIntent is."""
    payload = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "payment_intent": "pi_test_1",
                "payment_status": "paid",
                "amount_total": 49900,
                "currency": "inr",
                "metadata": {"order_id": "0f9d4a3e-0000-4000-8000-000000000002"},
            }
        },
    }
    event = stripe.parse_webhook(payload, {})
    assert event.category == "payment"
    assert event.provider_payment_id == "pi_test_1"
    assert event.provider_order_id == "cs_test_1"
    assert event.amount_minor == 49900
    assert str(event.order_id) == "0f9d4a3e-0000-4000-8000-000000000002"


def test_stripe_form_encoding_flattens_nested_payloads():
    """Stripe's API is form-encoded with bracket syntax; JSON silently posts nothing."""
    from services.providers.stripe import _flatten

    flat = _flatten(
        {
            "mode": "payment",
            "metadata": {"order_id": "abc"},
            "line_items": [{"quantity": 1, "price_data": {"currency": "inr"}}],
            "expand": False,
        }
    )
    assert flat["mode"] == "payment"
    assert flat["metadata[order_id]"] == "abc"
    assert flat["line_items[0][quantity]"] == "1"
    assert flat["line_items[0][price_data][currency]"] == "inr"
    assert flat["expand"] == "false"


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def test_constant_time_comparison_matches_ordinary_equality():
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
    assert constant_time_equals("abc", "") is False


def test_hmac_helper_matches_the_standard_library():
    assert hmac_sha256_hex("secret", b"message") == _sign("secret", b"message")

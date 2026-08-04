"""HTTP surface: authentication, authorisation, idempotency and webhook responses.

These exercise the wiring the service tests bypass — dependency injection, the
permission dependencies, the error envelope, and the status codes a provider or a
browser actually sees.
"""

from __future__ import annotations

import uuid

import orjson
import pytest

from tests.conftest import BOOK_A, BOOK_B, BOOK_DRAFT, BOOK_FREE, OTHER_ID, READER_ID

pytestmark = pytest.mark.asyncio


def cart(*book_ids) -> list[dict]:
    return [{"book_id": str(b), "quantity": 1} for b in (book_ids or [BOOK_A])]


# ---------------------------------------------------------------------------
# Configuration and quoting
# ---------------------------------------------------------------------------


async def test_providers_endpoint_exposes_only_public_keys(client):
    """The checkout page needs the publishable key; the secret never leaves here."""
    response = await client.get("/v1/payments/providers")
    assert response.status_code == 200
    body = response.json()
    assert "manual" not in body["providers"]  # staff-only
    assert body["razorpay_key_id"] == "rzp_test_key"
    assert body["stripe_publishable_key"] == "pk_test_key"
    assert "secret" not in orjson.dumps(body).decode().lower()


async def test_quote_is_public_and_writes_nothing(client, session, services):
    from sqlalchemy import func, select

    from models import Order

    response = await client.post("/v1/checkout/quote", json={"items": cart(BOOK_A)})
    assert response.status_code == 200
    assert response.json()["total_minor"] == 49900

    count = (await session.execute(select(func.count(Order.id)))).scalar_one()
    assert count == 0


async def test_quote_rejects_a_duplicate_book_id(client):
    """Use quantity instead — two lines for one book would double-charge silently."""
    response = await client.post(
        "/v1/checkout/quote",
        json={"items": [{"book_id": str(BOOK_A)}, {"book_id": str(BOOK_A)}]},
    )
    assert response.status_code == 422


async def test_quote_rejects_an_empty_cart(client):
    response = await client.post("/v1/checkout/quote", json={"items": []})
    assert response.status_code == 422


async def test_quote_rejects_an_unknown_field(client):
    """`extra="forbid"` — a typo'd field is a 422, not a silently ignored one."""
    response = await client.post("/v1/checkout/quote", json={"items": cart(), "total_minor": 1})
    assert response.status_code == 422


async def test_a_bad_coupon_is_a_200_with_an_explanation(client, coupon_factory):
    response = await client.post("/v1/coupons/validate", json={"code": "NOSUCH", "items": cart()})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["reason"] == "That code is not recognised."


async def test_a_good_coupon_validates(client, coupon_factory):
    await coupon_factory(code="TEN", value=10)
    response = await client.post("/v1/coupons/validate", json={"code": "TEN", "items": cart()})
    body = response.json()
    assert body["valid"] is True
    assert body["discount_minor"] == 4990


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


async def test_creating_an_order_requires_authentication(client):
    response = await client.post("/v1/orders", json={"items": cart()})
    assert response.status_code == 401


async def test_creating_an_order_returns_a_checkout_session(client, as_user):
    as_user()
    response = await client.post("/v1/orders", json={"items": cart(BOOK_A)})
    assert response.status_code == 201
    body = response.json()
    assert body["amount_minor"] == 49900
    assert body["provider"] == "razorpay"
    assert body["checkout_url"].startswith("https://gateway.test/")
    assert body["order"]["status"] == "awaiting_payment"
    assert len(body["order"]["items"]) == 1


async def test_the_order_is_owned_by_the_token_not_the_request(client, as_user):
    """There is no user_id field on OrderCreate, and this is why."""
    as_user(READER_ID)
    response = await client.post("/v1/orders", json={"items": cart()})
    assert response.json()["order"]["user_id"] == str(READER_ID)


async def test_an_idempotency_key_makes_a_retry_return_the_same_order(client, as_user):
    """A double-clicked Buy button must not produce two orders."""
    as_user()
    key = str(uuid.uuid4())
    payload = {"items": cart(BOOK_A)}

    first = await client.post("/v1/orders", json=payload, headers={"Idempotency-Key": key})
    second = await client.post("/v1/orders", json=payload, headers={"Idempotency-Key": key})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["order"]["id"] == second.json()["order"]["id"]


async def test_a_replayed_order_does_not_return_a_client_secret(client, as_user):
    """It is a single-use credential for one browser and is never persisted."""
    as_user()
    key = str(uuid.uuid4())
    payload = {"items": cart(BOOK_A)}
    first = await client.post("/v1/orders", json=payload, headers={"Idempotency-Key": key})
    second = await client.post("/v1/orders", json=payload, headers={"Idempotency-Key": key})

    assert first.json()["client_secret"] == "cs_test_secret"
    assert second.json()["client_secret"] is None
    assert second.json()["checkout_url"] is not None  # still resumable


async def test_two_different_keys_create_two_orders(client, as_user):
    as_user()
    payload = {"items": cart(BOOK_A)}
    first = await client.post(
        "/v1/orders", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    second = await client.post(
        "/v1/orders", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert first.json()["order"]["id"] != second.json()["order"]["id"]


async def test_a_draft_book_cannot_be_ordered(client, as_user):
    as_user()
    response = await client.post("/v1/orders", json={"items": cart(BOOK_DRAFT)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "book_not_purchasable"


async def test_a_cart_of_books_you_already_own_is_refused(client, as_user, catalogue):
    as_user(READER_ID)
    catalogue.owned[READER_ID] = {BOOK_A}
    response = await client.post("/v1/orders", json={"items": cart(BOOK_A)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "already_owned"


async def test_a_free_order_settles_without_a_gateway(client, as_user, session):
    """A free book grants access through exactly the same path as a paid one."""
    from sqlalchemy import select

    from models import Order

    as_user()
    response = await client.post("/v1/orders", json={"items": cart(BOOK_FREE)})
    assert response.status_code == 201
    assert response.json()["amount_minor"] == 0

    order = (await session.execute(select(Order))).scalars().one()
    assert order.status == "paid"
    assert order.paid_at is not None


async def test_a_return_url_outside_the_allowlist_is_refused(client, as_user):
    """An unvalidated return URL is an open redirect from a page the customer trusts."""
    as_user()
    response = await client.post(
        "/v1/orders",
        json={"items": cart(), "return_url": "https://evil.example/steal"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "return_url_not_allowed"


async def test_an_allowlisted_return_url_is_accepted(client, as_user):
    as_user()
    response = await client.post(
        "/v1/orders",
        json={"items": cart(), "return_url": "http://localhost:3000/checkout/done"},
    )
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# Order history
# ---------------------------------------------------------------------------


async def test_you_only_see_your_own_orders(client, as_user, order_factory):
    await order_factory(user_id=OTHER_ID)
    as_user(READER_ID)
    response = await client.get("/v1/orders")
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_another_users_order_is_a_404(client, as_user, order_factory):
    order = await order_factory(user_id=OTHER_ID)
    as_user(READER_ID)
    response = await client.get(f"/v1/orders/{order.id}")
    assert response.status_code == 404


async def test_you_can_cancel_your_own_unpaid_order(client, as_user, order_factory):
    order = await order_factory(user_id=READER_ID)
    as_user(READER_ID)
    response = await client.post(f"/v1/orders/{order.id}/cancel", json={"reason": "nope"})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Client-side verification
# ---------------------------------------------------------------------------


async def test_a_valid_signature_settles_the_order(client, as_user, order_factory):
    order = await order_factory(user_id=READER_ID)
    as_user(READER_ID)
    response = await client.post(
        "/v1/payments/verify",
        json={
            "order_id": str(order.id),
            "provider": "razorpay",
            "provider_order_id": order.provider_order_id,
            "provider_payment_id": "pay_1",
            "signature": "valid",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "paid"


async def test_an_invalid_signature_is_refused(client, as_user, order_factory, session):
    """Someone posted a payment confirmation they could not sign."""
    order = await order_factory(user_id=READER_ID)
    as_user(READER_ID)
    response = await client.post(
        "/v1/payments/verify",
        json={
            "order_id": str(order.id),
            "provider": "razorpay",
            "provider_order_id": order.provider_order_id,
            "provider_payment_id": "pay_1",
            "signature": "forged",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_payment_signature"

    await session.refresh(order)
    assert order.status == "awaiting_payment"


async def test_a_signature_for_a_different_gateway_order_is_refused(client, as_user, order_factory):
    """A valid signature over someone else's payment is still not payment for this."""
    order = await order_factory(user_id=READER_ID)
    as_user(READER_ID)
    response = await client.post(
        "/v1/payments/verify",
        json={
            "order_id": str(order.id),
            "provider": "razorpay",
            "provider_order_id": "order_for_someone_else",
            "provider_payment_id": "pay_1",
            "signature": "valid",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "order_reference_mismatch"


async def test_you_cannot_verify_a_payment_for_someone_elses_order(client, as_user, order_factory):
    order = await order_factory(user_id=OTHER_ID)
    as_user(READER_ID)
    response = await client.post(
        "/v1/payments/verify",
        json={
            "order_id": str(order.id),
            "provider": "razorpay",
            "provider_order_id": order.provider_order_id,
            "provider_payment_id": "pay_1",
            "signature": "valid",
        },
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


async def test_an_unsigned_webhook_is_a_401(client, order_factory):
    """A provider surfaces this in its dashboard, which is where a missing webhook
    secret should show up."""
    order = await order_factory()
    response = await client.post(
        "/v1/webhooks/razorpay",
        json={
            "id": "evt_1",
            "event": "payment.captured",
            "provider_order_id": order.provider_order_id,
            "provider_payment_id": "pay_1",
        },
    )
    assert response.status_code == 401
    assert response.json()["received"] is False


async def test_a_signed_webhook_settles_and_returns_200(client, order_factory, session):
    order = await order_factory()
    response = await client.post(
        "/v1/webhooks/razorpay",
        json={
            "id": "evt_1",
            "event": "payment.captured",
            "category": "payment",
            "provider_order_id": order.provider_order_id,
            "provider_payment_id": "pay_1",
            "amount_minor": order.total_minor,
        },
        headers={"x-test-signature": "valid"},
    )
    assert response.status_code == 200
    assert response.json()["received"] is True

    await session.refresh(order)
    assert order.status == "paid"


async def test_a_redelivered_webhook_reports_duplicate_not_an_error(client, order_factory):
    order = await order_factory()
    payload = {
        "id": "evt_dup",
        "event": "payment.captured",
        "category": "payment",
        "provider_order_id": order.provider_order_id,
        "provider_payment_id": "pay_1",
    }
    headers = {"x-test-signature": "valid"}
    await client.post("/v1/webhooks/razorpay", json=payload, headers=headers)
    second = await client.post("/v1/webhooks/razorpay", json=payload, headers=headers)

    assert second.status_code == 200
    assert second.json()["duplicate"] is True


async def test_malformed_webhook_json_is_a_400(client):
    response = await client.post(
        "/v1/webhooks/razorpay",
        content=b"{not json",
        headers={"content-type": "application/json", "x-test-signature": "valid"},
    )
    assert response.status_code == 400


async def test_an_oversized_webhook_body_is_refused_before_parsing(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_webhook_body_bytes", 10)
    response = await client.post(
        "/v1/webhooks/razorpay",
        content=orjson.dumps({"id": "x" * 500}),
        headers={"content-type": "application/json", "x-test-signature": "valid"},
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


async def test_admin_routes_reject_an_ordinary_user(client, as_user, order_factory):
    order = await order_factory()
    as_user()  # only books:read
    assert (await client.get("/v1/admin/orders")).status_code == 403
    assert (await client.post(f"/v1/admin/orders/{order.id}/refund", json={})).status_code == 403
    assert (await client.get("/v1/admin/revenue")).status_code == 403


async def test_an_admin_can_list_every_order(client, as_admin, order_factory):
    await order_factory(user_id=READER_ID)
    await order_factory(user_id=OTHER_ID)
    as_admin()
    response = await client.get("/v1/admin/orders")
    assert response.status_code == 200
    assert response.json()["total"] == 2


async def test_an_admin_can_refund_a_paid_order(client, as_admin, order_factory, services, session):
    from knowledgeos_core import PaymentProvider

    order = await order_factory()
    await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()

    as_admin()
    response = await client.post(
        f"/v1/admin/orders/{order.id}/refund", json={"amount_minor": 10000, "reason": "goodwill"}
    )
    assert response.status_code == 201
    assert response.json()["amount_minor"] == 10000

    await session.refresh(order)
    assert order.refunded_minor == 10000
    assert order.status == "partially_refunded"


async def test_an_admin_can_settle_an_offline_payment(client, as_admin, order_factory, session):
    order = await order_factory()
    as_admin()
    response = await client.post(f"/v1/admin/orders/{order.id}/mark-paid")
    assert response.status_code == 200
    assert response.json()["status"] == "paid"

    await session.refresh(order)
    assert order.provider is not None


async def test_an_admin_can_create_and_deactivate_a_coupon(client, as_admin):
    as_admin()
    created = await client.post(
        "/v1/admin/coupons",
        json={"code": "launch20", "coupon_type": "percent", "value": 20},
    )
    assert created.status_code == 201
    # Upper-cased on the way in, so WELCOME and welcome cannot become two coupons.
    assert created.json()["code"] == "LAUNCH20"

    coupon_id = created.json()["id"]
    removed = await client.delete(f"/v1/admin/coupons/{coupon_id}")
    assert removed.status_code == 200
    assert removed.json()["is_active"] is False


async def test_a_percentage_coupon_above_100_is_rejected(client, as_admin):
    as_admin()
    response = await client.post(
        "/v1/admin/coupons",
        json={"code": "TOOMUCH", "coupon_type": "percent", "value": 150},
    )
    assert response.status_code == 422


async def test_a_duplicate_coupon_code_is_a_409(client, as_admin):
    as_admin()
    body = {"code": "DUPE", "coupon_type": "percent", "value": 5}
    await client.post("/v1/admin/coupons", json=body)
    second = await client.post("/v1/admin/coupons", json=body)
    assert second.status_code == 409


async def test_revenue_reports_net_of_tax(client, as_admin, order_factory, services, session):
    from knowledgeos_core import PaymentProvider

    order = await order_factory()
    await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()

    as_admin()
    response = await client.get("/v1/admin/revenue")
    body = response.json()
    assert body["gross_minor"] == order.total_minor
    assert body["net_minor"] == order.total_minor - order.tax_minor


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------


async def test_an_invoice_is_visible_to_its_owner_only(
    client, as_user, order_factory, services, session
):
    from knowledgeos_core import PaymentProvider

    order = await order_factory(user_id=READER_ID)
    result = await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()
    invoice_id = result.invoice.id

    as_user(READER_ID)
    assert (await client.get(f"/v1/invoices/{invoice_id}")).status_code == 200

    as_user(OTHER_ID)
    assert (await client.get(f"/v1/invoices/{invoice_id}")).status_code == 404


async def test_becoming_an_affiliate_is_idempotent(client, as_user):
    as_user()
    first = await client.post("/v1/affiliate", json={"code": "myref"})
    second = await client.post("/v1/affiliate", json={"code": "myref"})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


async def test_affiliate_conversions_are_empty_without_an_account(client, as_user):
    as_user()
    response = await client.get("/v1/affiliate/conversions")
    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_plans_are_publicly_listable(client, as_admin, session):
    as_admin()
    created = await client.post(
        "/v1/admin/plans",
        json={"code": "pro", "name": "Pro", "price_minor": 99900, "interval": "month"},
    )
    assert created.status_code == 201

    response = await client.get("/v1/plans")
    assert response.status_code == 200
    assert response.json()["items"][0]["code"] == "pro"


async def test_a_subscription_can_be_started_and_cancelled(client, as_user, as_admin):
    as_admin()
    plan = (
        await client.post(
            "/v1/admin/plans",
            json={"code": "pro", "name": "Pro", "price_minor": 99900, "trial_days": 7},
        )
    ).json()

    as_user(READER_ID)
    created = await client.post("/v1/subscriptions", json={"plan_id": plan["id"]})
    assert created.status_code == 201
    assert created.json()["status"] == "trialing"  # a trial starts live

    subscription_id = created.json()["id"]
    cancelled = await client.post(
        f"/v1/subscriptions/{subscription_id}/cancel", json={"at_period_end": True}
    )
    assert cancelled.status_code == 200
    # The customer paid for the period and keeps it.
    assert cancelled.json()["cancel_at_period_end"] is True
    assert cancelled.json()["status"] == "trialing"


async def test_a_second_active_subscription_is_a_409(client, as_user, as_admin):
    as_admin()
    plan = (
        await client.post(
            "/v1/admin/plans",
            json={"code": "pro", "name": "Pro", "price_minor": 99900, "trial_days": 7},
        )
    ).json()

    as_user(READER_ID)
    await client.post("/v1/subscriptions", json={"plan_id": plan["id"]})
    second = await client.post("/v1/subscriptions", json={"plan_id": plan["id"]})
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


async def test_internal_routes_reject_an_unsigned_caller(client, order_factory):
    """Private-network reachability is not authorisation."""
    order = await order_factory()
    response = await client.get(f"/internal/orders/{order.id}")
    assert response.status_code == 401


async def test_internal_order_summary_carries_the_book_ids(client, as_internal, order_factory):
    order = await order_factory(book_ids=[BOOK_A, BOOK_B])
    as_internal()
    response = await client.get(f"/internal/orders/{order.id}")
    assert response.status_code == 200
    assert set(response.json()["book_ids"]) == {str(BOOK_A), str(BOOK_B)}


async def test_internal_purchased_books_lists_only_paid_orders(
    client, as_internal, order_factory, services, session
):
    from knowledgeos_core import PaymentProvider

    paid = await order_factory(user_id=READER_ID, book_ids=[BOOK_A])
    await order_factory(user_id=READER_ID, book_ids=[BOOK_B])  # left unpaid
    await services["payments"].settle(
        session, order=paid, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()

    as_internal()
    response = await client.get(f"/internal/users/{READER_ID}/purchased-books")
    assert response.status_code == 200
    assert response.json() == [str(BOOK_A)]


async def test_internal_maintenance_expires_stale_orders(
    client, as_internal, order_factory, session
):
    from datetime import UTC, datetime, timedelta

    order = await order_factory()
    order.expires_at = datetime.now(UTC) - timedelta(hours=2)
    await session.commit()

    as_internal()
    response = await client.post("/internal/maintenance/expire-orders")
    assert response.status_code == 200
    assert "1" in response.json()["message"]


async def test_health_is_public(client):
    response = await client.get("/health")
    assert response.status_code == 200

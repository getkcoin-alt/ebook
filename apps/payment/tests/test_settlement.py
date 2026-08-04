"""Orders, settlement, refunds and webhooks.

The theme of this file is **exactly-once under at-least-once delivery**. A payment
confirmation arrives from three directions — the provider's webhook, the browser's
verification call, and an operator's replay — and any of them can arrive twice. The
tests that matter most here are the ones that call the same thing twice and assert
that the second call changed nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import orjson
import pytest
from sqlalchemy import func, select

from knowledgeos_core import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    OrderStatus,
    PaymentProvider,
)
from models import AffiliateConversion, Invoice, Payment, WebhookEvent
from schemas import AffiliateConversionStatus, PaymentStatus, RefundStatus
from tests.conftest import BOOK_A, BOOK_B, BOOK_FREE, OTHER_ID, READER_ID

pytestmark = pytest.mark.asyncio


async def _count(session, model) -> int:
    return int((await session.execute(select(func.count(model.id)))).scalar_one())


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


async def test_an_order_snapshots_its_line_items(session, services, order_factory, catalogue):
    """A later price change must not rewrite what a past customer bought."""
    from services.catalogue import CatalogueBook

    order = await order_factory(book_ids=[BOOK_A])
    catalogue.books[BOOK_A] = CatalogueBook(
        id=BOOK_A,
        slug="book-a",
        title="Book A",
        price_minor=99900,
        currency="INR",
        status="published",
    )

    reloaded = await services["orders"].get(session, order.id)
    assert reloaded.items[0].unit_price_minor == 49900
    assert reloaded.items[0].title == "Book A"


async def test_order_numbers_are_unique_and_unguessable(session, services, order_factory):
    numbers = {(await order_factory()).order_number for _ in range(5)}
    assert len(numbers) == 5
    # A sequential counter would leak daily sales volume to anyone who buys twice.
    assert all(number.startswith("KOS-") for number in numbers)


async def test_attaching_a_gateway_moves_the_order_to_awaiting_payment(
    session, services, order_factory, gateways
):
    order = await order_factory()
    assert order.status == OrderStatus.AWAITING_PAYMENT
    assert order.provider_order_id == f"prov_{order.order_number}"
    assert gateways.stub.created[0]["amount_minor"] == order.total_minor


async def test_a_zero_value_order_routes_to_the_manual_gateway(session, services, order_factory):
    """No card network accepts a ₹0 charge, but the order still has to complete."""
    order = await order_factory(book_ids=[BOOK_FREE])
    assert order.total_minor == 0
    assert order.provider == PaymentProvider.MANUAL


async def test_a_coupon_is_consumed_when_the_order_is_created(
    session, services, order_factory, coupon_factory
):
    """Consuming it only on success would let one code fund unlimited checkouts."""
    coupon = await coupon_factory(code="TEN", value=10, usage_limit=1)
    await order_factory(coupon_code="TEN")

    await session.refresh(coupon)
    assert coupon.usage_count == 1


async def test_a_single_use_coupon_cannot_be_used_twice(
    session, services, order_factory, coupon_factory
):
    await coupon_factory(code="ONCE", value=10, usage_limit=1, per_user_limit=5)
    await order_factory(coupon_code="ONCE")

    # The second quote sees the exhausted counter and simply does not apply it.
    from schemas import OrderItemIn

    cart = await services["pricing"].quote(
        session,
        items=[OrderItemIn(book_id=BOOK_A)],
        user_id=OTHER_ID,
        currency="INR",
        coupon_code="ONCE",
    )
    assert cart.coupon_applied is False


async def test_a_per_user_limit_blocks_a_second_use_by_the_same_buyer(
    session, services, order_factory, coupon_factory
):
    await coupon_factory(code="ONEEACH", value=10, per_user_limit=1)
    await order_factory(coupon_code="ONEEACH")

    from schemas import OrderItemIn

    again = await services["pricing"].quote(
        session,
        items=[OrderItemIn(book_id=BOOK_A)],
        user_id=READER_ID,
        currency="INR",
        coupon_code="ONEEACH",
    )
    assert again.coupon_applied is False
    assert "already used" in again.coupon_message

    # ...but a different customer still may.
    other = await services["pricing"].quote(
        session,
        items=[OrderItemIn(book_id=BOOK_A)],
        user_id=OTHER_ID,
        currency="INR",
        coupon_code="ONEEACH",
    )
    assert other.coupon_applied is True


async def test_cancelling_an_order_returns_its_coupon(
    session, services, order_factory, coupon_factory
):
    """An abandoned checkout must not consume a single-use code forever."""
    coupon = await coupon_factory(code="BACK", value=10, usage_limit=1)
    order = await order_factory(coupon_code="BACK")
    await session.refresh(coupon)
    assert coupon.usage_count == 1

    await services["orders"].cancel(session, order, reason="Changed my mind")
    await session.commit()
    await session.refresh(coupon)
    assert coupon.usage_count == 0


async def test_expiring_a_stale_order_returns_its_coupon(
    session, services, order_factory, coupon_factory
):
    coupon = await coupon_factory(code="STALE", value=10, usage_limit=1)
    order = await order_factory(coupon_code="STALE")
    order.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await session.commit()

    expired = await services["orders"].expire_stale(session)
    assert expired == 1
    await session.refresh(order)
    await session.refresh(coupon)
    assert order.status == OrderStatus.CANCELLED
    assert coupon.usage_count == 0


async def test_another_users_order_is_a_404_not_a_403(session, services, order_factory):
    """A 403 would confirm the order exists, turning the endpoint into an oracle."""
    order = await order_factory(user_id=READER_ID)
    with pytest.raises(NotFoundError):
        await services["orders"].get_for_user(session, order.id, OTHER_ID)


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


async def test_settling_an_order_records_a_payment_and_issues_one_invoice(
    session, services, order_factory
):
    order = await order_factory()
    result = await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()

    assert result.newly_paid is True
    assert order.status == OrderStatus.PAID
    assert order.paid_at is not None
    assert result.invoice is not None
    assert await _count(session, Payment) == 1
    assert await _count(session, Invoice) == 1


async def test_settling_twice_is_a_no_op(session, services, order_factory):
    """The single most important property in this service.

    A redelivered webhook and a client-side verify call both reach here; only one of
    them may issue an invoice or grant a book.
    """
    order = await order_factory()
    first = await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    second = await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()

    assert first.newly_paid is True
    assert second.newly_paid is False
    assert await _count(session, Payment) == 1
    assert await _count(session, Invoice) == 1


async def test_a_second_payment_id_on_a_settled_order_does_not_re_settle_it(
    session, services, order_factory
):
    """A different payment id is a new row, but the order is already paid."""
    order = await order_factory()
    await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    second = await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_2"
    )
    await session.commit()

    assert second.newly_paid is False
    assert await _count(session, Payment) == 2  # both attempts recorded
    assert await _count(session, Invoice) == 1  # but only one invoice


async def test_an_authorised_payment_advancing_to_captured_updates_one_row(
    session, services, order_factory
):
    """Providers send the same payment id twice as it moves forward."""
    order = await order_factory()
    await services["payments"].record_payment(
        session,
        order=order,
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id="pay_1",
        status=PaymentStatus.AUTHORIZED,
        amount_minor=order.total_minor,
        currency="INR",
    )
    payment, created = await services["payments"].record_payment(
        session,
        order=order,
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id="pay_1",
        status=PaymentStatus.CAPTURED,
        amount_minor=order.total_minor,
        currency="INR",
    )
    await session.commit()

    assert created is False
    assert payment.status == PaymentStatus.CAPTURED
    assert payment.captured_at is not None
    assert await _count(session, Payment) == 1


async def test_an_amount_mismatch_is_recorded_rather_than_refused(session, services, order_factory):
    """The money already moved; refusing to record it would leave it nowhere."""
    order = await order_factory()
    result = await services["payments"].settle(
        session,
        order=order,
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id="pay_1",
        amount_minor=1,
    )
    await session.commit()

    assert result.newly_paid is True
    assert result.payment.amount_minor == 1


async def test_a_failed_payment_leaves_the_order_retryable(session, services, order_factory):
    order = await order_factory()
    await services["payments"].fail(
        session,
        order=order,
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id="pay_fail",
        reason="Card declined",
    )
    await session.commit()

    assert order.status == OrderStatus.FAILED
    assert order.failure_reason == "Card declined"
    assert await _count(session, Payment) == 1


async def test_a_late_failure_never_un_pays_a_settled_order(session, services, order_factory):
    """Webhooks arrive out of order; a stale failure must not undo a real payment."""
    order = await order_factory()
    await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_ok"
    )
    await services["payments"].fail(
        session,
        order=order,
        provider=PaymentProvider.RAZORPAY,
        provider_payment_id="pay_old",
        reason="Timed out",
    )
    await session.commit()
    assert order.status == OrderStatus.PAID


async def test_the_paid_event_payload_carries_the_books_to_grant(session, services, order_factory):
    """A cross-service contract: books grants access from `book_ids`."""
    order = await order_factory(book_ids=[BOOK_A, BOOK_B])
    await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    payload = services["payments"].order_event_payload(order)
    assert set(payload["book_ids"]) == {str(BOOK_A), str(BOOK_B)}
    assert payload["order_id"] == str(order.id)
    assert payload["user_id"] == str(READER_ID)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


async def test_invoice_numbers_are_consecutive_within_a_financial_year(
    session, services, order_factory
):
    """GST rules require a consecutive serial, which is why it is not a UUID."""
    numbers = []
    for _ in range(3):
        order = await order_factory()
        result = await services["payments"].settle(
            session,
            order=order,
            provider=PaymentProvider.RAZORPAY,
            provider_payment_id=f"pay_{uuid.uuid4()}",
        )
        await session.commit()
        numbers.append(result.invoice.invoice_number)

    serials = [int(number.rsplit("/", 1)[-1]) for number in numbers]
    assert serials == [1, 2, 3]


async def test_an_invoice_snapshots_the_billing_details(session, services, order_factory):
    order = await order_factory()
    result = await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()

    invoice = result.invoice
    assert invoice.line_items[0]["title"] == "Book A"
    assert invoice.billing_snapshot["email"] == "buyer@knowledgeos.dev"
    assert invoice.billing_snapshot["seller_state_code"] == "GJ"
    assert invoice.total_minor == order.total_minor


async def test_an_invoice_is_never_issued_twice_for_one_order(session, services, order_factory):
    order = await order_factory()
    first = await services["invoices"].for_order(session, order)
    second = await services["invoices"].for_order(session, order)
    await session.commit()
    assert first.id == second.id
    assert await _count(session, Invoice) == 1


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


async def _paid_order(session, services, order_factory, **kwargs):
    order = await order_factory(**kwargs)
    await services["payments"].settle(
        session, order=order, provider=PaymentProvider.RAZORPAY, provider_payment_id="pay_1"
    )
    await session.commit()
    return order


async def test_a_full_refund_marks_the_order_refunded(session, services, order_factory):
    order = await _paid_order(session, services, order_factory)
    refund = await services["refunds"].create(
        session, order=order, amount_minor=None, reason="Requested", actor_id=OTHER_ID
    )
    await session.commit()

    assert refund.amount_minor == order.total_minor
    assert order.status == OrderStatus.REFUNDED
    assert order.refunded_minor == order.total_minor
    assert refund.actor_id == OTHER_ID  # attributable, always


async def test_a_partial_refund_leaves_the_order_partially_refunded(
    session, services, order_factory
):
    order = await _paid_order(session, services, order_factory)
    await services["refunds"].create(
        session, order=order, amount_minor=10000, reason=None, actor_id=OTHER_ID
    )
    await session.commit()

    assert order.status == OrderStatus.PARTIALLY_REFUNDED
    assert order.refunded_minor == 10000
    assert order.refundable_minor == order.total_minor - 10000


async def test_refunds_cannot_exceed_the_order_total_across_several_calls(
    session, services, order_factory
):
    order = await _paid_order(session, services, order_factory)
    await services["refunds"].create(
        session, order=order, amount_minor=40000, reason=None, actor_id=OTHER_ID
    )
    await session.commit()

    with pytest.raises(BadRequestError) as exc:
        await services["refunds"].create(
            session, order=order, amount_minor=40000, reason=None, actor_id=OTHER_ID
        )
    assert exc.value.code == "refund_exceeds_remaining"


async def test_an_unpaid_order_cannot_be_refunded(session, services, order_factory):
    order = await order_factory()
    with pytest.raises(ConflictError) as exc:
        await services["refunds"].create(
            session, order=order, amount_minor=None, reason=None, actor_id=OTHER_ID
        )
    assert exc.value.code == "order_not_refundable"


async def test_a_paid_order_cannot_be_cancelled(session, services, order_factory):
    order = await _paid_order(session, services, order_factory)
    with pytest.raises(ConflictError) as exc:
        await services["orders"].cancel(session, order)
    assert exc.value.code == "order_already_paid"


async def test_the_refund_reaches_the_gateway_with_an_idempotency_key(
    session, services, order_factory, gateways
):
    """A timeout-and-retry against the gateway must not refund twice."""
    order = await _paid_order(session, services, order_factory)
    refund = await services["refunds"].create(
        session, order=order, amount_minor=10000, reason="oops", actor_id=OTHER_ID
    )
    await session.commit()

    call = gateways.stub.refunds[0]
    assert call["amount_minor"] == 10000
    assert call["idempotency_key"] == str(refund.id)


async def test_a_refund_webhook_settles_the_pending_refund(
    session, services, order_factory, gateways
):
    gateways.stub.refund_status = "pending"
    order = await _paid_order(session, services, order_factory)
    refund = await services["refunds"].create(
        session, order=order, amount_minor=10000, reason=None, actor_id=OTHER_ID
    )
    await session.commit()
    assert refund.status == RefundStatus.PROCESSING

    await services["refunds"].mark_settled(
        session,
        provider=PaymentProvider.RAZORPAY,
        provider_refund_id=refund.provider_refund_id,
        succeeded=True,
    )
    await session.commit()
    assert refund.status == RefundStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Affiliates
# ---------------------------------------------------------------------------


async def test_commission_is_calculated_on_the_net_of_tax(session, services, order_factory):
    """GST is collected for the government; paying commission on it pays out money
    that was never revenue."""
    account = await services["affiliates"].register(session, user_id=OTHER_ID, code="ref1")
    order = await _paid_order(session, services, order_factory, affiliate_code="ref1")

    conversion = (
        (
            await session.execute(
                select(AffiliateConversion).where(AffiliateConversion.order_id == order.id)
            )
        )
        .scalars()
        .one()
    )
    net = order.total_minor - order.tax_minor
    assert conversion.commission_minor == net * account.commission_bps // 10_000
    assert conversion.status == AffiliateConversionStatus.PENDING.value


async def test_a_self_referral_earns_nothing(session, services, order_factory):
    await services["affiliates"].register(session, user_id=READER_ID, code="self")
    order = await _paid_order(session, services, order_factory, affiliate_code="self")
    assert await _count(session, AffiliateConversion) == 0
    assert order.status == OrderStatus.PAID  # the sale still completes


async def test_an_unknown_affiliate_code_does_not_block_the_sale(session, services, order_factory):
    """A stale referral link must never stop someone buying something."""
    order = await _paid_order(session, services, order_factory, affiliate_code="ghost")
    assert order.status == OrderStatus.PAID
    assert await _count(session, AffiliateConversion) == 0


async def test_a_conversion_is_recorded_once_per_order(session, services, order_factory):
    await services["affiliates"].register(session, user_id=OTHER_ID, code="ref1")
    order = await _paid_order(session, services, order_factory, affiliate_code="ref1")
    await services["affiliates"].record(session, order)
    await session.commit()
    assert await _count(session, AffiliateConversion) == 1


async def test_a_full_refund_reverses_the_commission(session, services, order_factory):
    account = await services["affiliates"].register(session, user_id=OTHER_ID, code="ref1")
    order = await _paid_order(session, services, order_factory, affiliate_code="ref1")
    earned = account.total_earned_minor
    assert earned > 0

    await services["refunds"].create(
        session, order=order, amount_minor=None, reason="refund", actor_id=OTHER_ID
    )
    await session.commit()

    conversion = (
        (
            await session.execute(
                select(AffiliateConversion).where(AffiliateConversion.order_id == order.id)
            )
        )
        .scalars()
        .one()
    )
    await session.refresh(account)
    assert conversion.status == AffiliateConversionStatus.REVERSED.value
    assert account.total_earned_minor == 0


async def test_conversions_only_mature_after_the_hold_period(
    session, services, order_factory, settings, monkeypatch
):
    await services["affiliates"].register(session, user_id=OTHER_ID, code="ref1")
    await _paid_order(session, services, order_factory, affiliate_code="ref1")

    assert await services["affiliates"].approve_matured(session) == 0  # too new

    monkeypatch.setattr(settings, "affiliate_hold_days", 0)
    assert await services["affiliates"].approve_matured(session) == 1


async def test_registering_as_an_affiliate_twice_returns_the_same_account(session, services):
    first = await services["affiliates"].register(session, user_id=READER_ID)
    second = await services["affiliates"].register(session, user_id=READER_ID)
    assert first.id == second.id


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def _webhook_payload(order, **overrides):
    payload = {
        "id": f"evt_{uuid.uuid4()}",
        "event": "payment.captured",
        "category": "payment",
        "provider_order_id": order.provider_order_id,
        "provider_payment_id": f"pay_{uuid.uuid4()}",
        "amount_minor": order.total_minor,
        "currency": "INR",
    }
    payload.update(overrides)
    return payload


async def test_a_signed_webhook_settles_the_order(session, services, order_factory):
    order = await order_factory()
    payload = _webhook_payload(order)
    outcome = await services["webhooks"].ingest(
        session,
        provider=PaymentProvider.RAZORPAY,
        body=orjson.dumps(payload),
        headers={"x-test-signature": "valid"},
        payload=payload,
    )
    assert outcome.accepted is True
    assert outcome.settled_order_id == order.id

    await session.refresh(order)
    assert order.status == OrderStatus.PAID


async def test_an_unsigned_webhook_changes_nothing_but_is_still_recorded(
    session, services, order_factory
):
    """A burst of these is an attack signal worth being able to see."""
    order = await order_factory()
    payload = _webhook_payload(order)
    outcome = await services["webhooks"].ingest(
        session,
        provider=PaymentProvider.RAZORPAY,
        body=orjson.dumps(payload),
        headers={"x-test-signature": "forged"},
        payload=payload,
    )
    assert outcome.accepted is False

    await session.refresh(order)
    assert order.status == OrderStatus.AWAITING_PAYMENT

    event = (await session.execute(select(WebhookEvent))).scalars().one()
    assert event.signature_valid is False
    assert event.processed is False


async def test_a_redelivered_webhook_is_recognised_as_a_duplicate(session, services, order_factory):
    """Providers redeliver aggressively; the unique (provider, event_id) is what
    makes processing exactly-once."""
    order = await order_factory()
    payload = _webhook_payload(order)
    headers = {"x-test-signature": "valid"}

    first = await services["webhooks"].ingest(
        session,
        provider=PaymentProvider.RAZORPAY,
        body=orjson.dumps(payload),
        headers=headers,
        payload=payload,
    )
    second = await services["webhooks"].ingest(
        session,
        provider=PaymentProvider.RAZORPAY,
        body=orjson.dumps(payload),
        headers=headers,
        payload=payload,
    )

    assert first.settled_order_id == order.id
    assert second.duplicate is True
    assert second.settled_order_id is None
    assert await _count(session, WebhookEvent) == 1
    assert await _count(session, Invoice) == 1


async def test_a_webhook_for_an_unknown_order_is_recorded_and_ignored(session, services):
    """Provider dashboard test events land here; they are not errors."""
    payload = {
        "id": "evt_test",
        "event": "payment.captured",
        "category": "payment",
        "provider_order_id": "order_not_ours",
        "provider_payment_id": "pay_x",
    }
    outcome = await services["webhooks"].ingest(
        session,
        provider=PaymentProvider.RAZORPAY,
        body=orjson.dumps(payload),
        headers={"x-test-signature": "valid"},
        payload=payload,
    )
    assert outcome.accepted is True
    assert outcome.settled_order_id is None
    assert await _count(session, WebhookEvent) == 1


async def test_a_failure_webhook_marks_the_order_failed(session, services, order_factory):
    order = await order_factory()
    payload = _webhook_payload(order, event="payment.failed")
    await services["webhooks"].ingest(
        session,
        provider=PaymentProvider.RAZORPAY,
        body=orjson.dumps(payload),
        headers={"x-test-signature": "valid"},
        payload=payload,
    )
    await session.refresh(order)
    assert order.status == OrderStatus.FAILED


async def test_a_replay_only_runs_for_a_verified_event(session, services, order_factory):
    order = await order_factory()
    payload = _webhook_payload(order)
    await services["webhooks"].ingest(
        session,
        provider=PaymentProvider.RAZORPAY,
        body=orjson.dumps(payload),
        headers={"x-test-signature": "forged"},
        payload=payload,
    )
    event = (await session.execute(select(WebhookEvent))).scalars().one()

    outcome = await services["webhooks"].replay(session, event.id)
    assert outcome.accepted is False

    await session.refresh(order)
    assert order.status == OrderStatus.AWAITING_PAYMENT


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


async def test_revenue_excludes_tax_and_refunds_from_net(session, services, order_factory):
    order = await _paid_order(session, services, order_factory)
    await services["refunds"].create(
        session, order=order, amount_minor=10000, reason=None, actor_id=OTHER_ID
    )
    await session.commit()

    summary = await services["reports"].revenue(session)
    assert summary.gross_minor == order.total_minor
    assert summary.refunded_minor == 10000
    assert summary.tax_minor == order.tax_minor
    assert summary.net_minor == order.total_minor - 10000 - order.tax_minor
    assert summary.paid_order_count == 1


async def test_revenue_on_an_empty_period_returns_zeroes_not_nulls(session, services):
    """SUM over an empty set is NULL, which would propagate through the response."""
    summary = await services["reports"].revenue(session)
    assert summary.gross_minor == 0
    assert summary.net_minor == 0
    assert summary.order_count == 0

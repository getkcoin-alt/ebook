"""Pricing and coupons.

The invariant every test in this file is really checking:

    subtotal - discount == taxable + tax == total

:meth:`PricedCart.assert_consistent` enforces it in production, so a quote that
returns at all has already satisfied it. These tests pin down the *values*, and the
rules that decide them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from knowledgeos_core import BadRequestError, NotFoundError
from schemas import BillingAddress, CouponType, OrderItemIn
from tests.conftest import BOOK_A, BOOK_B, BOOK_DRAFT, BOOK_FREE, CATEGORY_FICTION, READER_ID

pytestmark = pytest.mark.asyncio


def items(*book_ids, quantity: int = 1) -> list[OrderItemIn]:
    return [OrderItemIn(book_id=b, quantity=quantity) for b in book_ids]


# ---------------------------------------------------------------------------
# Basic pricing
# ---------------------------------------------------------------------------


async def test_price_comes_from_the_catalogue_not_the_request(session, services):
    """The client sends book ids; the amount is resolved server-side."""
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR"
    )
    assert cart.subtotal_minor == 49900
    assert cart.total_minor == 49900
    assert cart.lines[0].unit_price_minor == 49900


async def test_quantity_multiplies_the_line(session, services):
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A, quantity=3), user_id=READER_ID, currency="INR"
    )
    assert cart.lines[0].line_total_minor == 149700
    assert cart.subtotal_minor == 149700


async def test_multiple_books_sum(session, services):
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A, BOOK_B), user_id=READER_ID, currency="INR"
    )
    assert cart.subtotal_minor == 79800


async def test_inclusive_pricing_leaves_the_total_equal_to_the_listed_price(session, services):
    """Tax is extracted from the price, never added on top of it."""
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR"
    )
    assert cart.total_minor == 49900
    assert cart.taxable_minor + cart.tax.total_minor == cart.total_minor


async def test_exclusive_pricing_adds_tax_on_top(session, services, settings, monkeypatch):
    monkeypatch.setattr(settings, "prices_include_tax", False)
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR"
    )
    assert cart.taxable_minor == 49900
    assert cart.total_minor == 49900 + cart.tax.total_minor


async def test_billing_state_decides_the_gst_split(session, services):
    intra = await services["pricing"].quote(
        session,
        items=items(BOOK_A),
        user_id=READER_ID,
        currency="INR",
        billing=BillingAddress(state_code="GJ"),
    )
    inter = await services["pricing"].quote(
        session,
        items=items(BOOK_A),
        user_id=READER_ID,
        currency="INR",
        billing=BillingAddress(state_code="MH"),
    )
    assert intra.tax.igst_minor == 0
    assert inter.tax.cgst_minor == 0
    assert intra.tax.total_minor == inter.tax.total_minor  # same money, different filing


async def test_a_draft_book_cannot_be_bought(session, services):
    with pytest.raises(BadRequestError) as exc:
        await services["pricing"].quote(
            session, items=items(BOOK_DRAFT), user_id=READER_ID, currency="INR"
        )
    assert exc.value.code == "book_not_purchasable"


async def test_an_unknown_book_is_a_404_not_a_zero_priced_line(session, services):
    with pytest.raises(NotFoundError):
        await services["pricing"].quote(
            session, items=items(uuid.uuid4()), user_id=READER_ID, currency="INR"
        )


async def test_a_currency_mismatch_is_rejected_rather_than_guessed(session, services, catalogue):
    from services.catalogue import CatalogueBook

    catalogue.books[BOOK_B] = CatalogueBook(
        id=BOOK_B, slug="b", title="B", price_minor=999, currency="USD", status="published"
    )
    with pytest.raises(BadRequestError) as exc:
        await services["pricing"].quote(
            session, items=items(BOOK_A, BOOK_B), user_id=READER_ID, currency="INR"
        )
    assert exc.value.code == "currency_mismatch"


async def test_a_cart_beyond_the_item_cap_is_rejected(session, services, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_order_items", 1)
    with pytest.raises(BadRequestError):
        await services["pricing"].quote(
            session, items=items(BOOK_A, BOOK_B), user_id=READER_ID, currency="INR"
        )


async def test_already_owned_books_are_flagged_not_silently_dropped(session, services, catalogue):
    """Dropping a line the customer chose is worse than explaining why."""
    catalogue.owned[READER_ID] = {BOOK_A}
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A, BOOK_B), user_id=READER_ID, currency="INR"
    )
    owned = {line.book_id: line.already_owned for line in cart.lines}
    assert owned[BOOK_A] is True
    assert owned[BOOK_B] is False
    assert catalogue.ownership_calls == 1


async def test_an_anonymous_quote_does_not_check_ownership(session, services, catalogue):
    await services["pricing"].quote(session, items=items(BOOK_A), user_id=None, currency="INR")
    assert catalogue.ownership_calls == 0


async def test_a_free_book_prices_at_zero(session, services):
    cart = await services["pricing"].quote(
        session, items=items(BOOK_FREE), user_id=READER_ID, currency="INR"
    )
    assert cart.total_minor == 0
    assert cart.tax.total_minor == 0


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------


async def test_a_percentage_coupon_discounts_the_subtotal(session, services, coupon_factory):
    await coupon_factory(code="TEN", coupon_type=CouponType.PERCENT, value=10)
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="TEN"
    )
    assert cart.coupon_applied is True
    assert cart.discount_minor == 4990
    assert cart.total_minor == 44910


async def test_a_percentage_cap_limits_the_giveaway(session, services, coupon_factory):
    """'50% off' without a cap gives away an unbounded amount on a large cart."""
    await coupon_factory(
        code="HALF", coupon_type=CouponType.PERCENT, value=50, max_discount_minor=10000
    )
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="HALF"
    )
    assert cart.discount_minor == 10000


async def test_a_fixed_coupon_cannot_exceed_the_cart(session, services, coupon_factory):
    """A ₹5000 coupon on a ₹299 cart makes it free, not a ₹4701 payout."""
    await coupon_factory(code="BIG", coupon_type=CouponType.FIXED, value=500000)
    cart = await services["pricing"].quote(
        session, items=items(BOOK_B), user_id=READER_ID, currency="INR", coupon_code="BIG"
    )
    assert cart.discount_minor == 29900
    assert cart.total_minor == 0


async def test_a_coupon_code_is_matched_case_insensitively(session, services, coupon_factory):
    await coupon_factory(code="WELCOME")
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="welcome"
    )
    assert cart.coupon_applied is True


async def test_an_unknown_code_is_reported_not_raised(session, services):
    """A typo is a UI state, so the cart still prices — with an explanation."""
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="NOPE"
    )
    assert cart.coupon_applied is False
    assert cart.discount_minor == 0
    assert cart.total_minor == 49900
    assert cart.coupon_message == "That code is not recognised."


async def test_an_expired_coupon_is_refused(session, services, coupon_factory):
    await coupon_factory(code="OLD", valid_until=datetime.now(UTC) - timedelta(days=1))
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="OLD"
    )
    assert cart.coupon_applied is False
    assert "expired" in cart.coupon_message


async def test_a_future_coupon_is_not_active_yet(session, services, coupon_factory):
    await coupon_factory(code="SOON", valid_from=datetime.now(UTC) + timedelta(days=1))
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="SOON"
    )
    assert cart.coupon_applied is False


async def test_an_inactive_coupon_is_refused(session, services, coupon_factory):
    await coupon_factory(code="OFF", is_active=False)
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="OFF"
    )
    assert cart.coupon_applied is False


async def test_a_minimum_order_is_enforced(session, services, coupon_factory):
    await coupon_factory(code="MIN", min_order_minor=100000)
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="MIN"
    )
    assert cart.coupon_applied is False
    assert "minimum order" in cart.coupon_message


async def test_an_exhausted_coupon_is_refused(session, services, coupon_factory):
    await coupon_factory(code="GONE", usage_limit=5, usage_count=5)
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="GONE"
    )
    assert cart.coupon_applied is False
    assert "fully redeemed" in cart.coupon_message


async def test_a_book_scoped_coupon_discounts_only_that_line(session, services, coupon_factory):
    """Computing the discount on the whole subtotal is the easy, expensive mistake."""
    await coupon_factory(
        code="AONLY",
        coupon_type=CouponType.PERCENT,
        value=50,
        applicable_book_ids=[str(BOOK_A)],
    )
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A, BOOK_B), user_id=READER_ID, currency="INR", coupon_code="AONLY"
    )
    # 50% of BOOK_A (49900) only, not of the 79800 subtotal.
    assert cart.discount_minor == 24950


async def test_a_category_scoped_coupon_matches_through_the_catalogue(
    session, services, coupon_factory
):
    await coupon_factory(
        code="FICTION",
        coupon_type=CouponType.PERCENT,
        value=10,
        applicable_category_ids=[str(CATEGORY_FICTION)],
    )
    cart = await services["pricing"].quote(
        session,
        items=items(BOOK_A, BOOK_B),
        user_id=READER_ID,
        currency="INR",
        coupon_code="FICTION",
    )
    assert cart.discount_minor == 4990  # BOOK_A is the only fiction title


async def test_a_coupon_matching_nothing_in_the_cart_is_refused(session, services, coupon_factory):
    await coupon_factory(code="OTHER", applicable_book_ids=[str(uuid.uuid4())])
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="OTHER"
    )
    assert cart.coupon_applied is False
    assert "does not apply" in cart.coupon_message


async def test_a_currency_scoped_coupon_is_refused_in_another_currency(
    session, services, coupon_factory
):
    await coupon_factory(code="USDONLY", currency="USD")
    cart = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="USDONLY"
    )
    assert cart.coupon_applied is False


async def test_tax_is_computed_after_the_discount(session, services, coupon_factory):
    """Taxing the pre-discount amount overcharges the customer."""
    await coupon_factory(code="TEN", coupon_type=CouponType.PERCENT, value=10)
    undiscounted = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR"
    )
    discounted = await services["pricing"].quote(
        session, items=items(BOOK_A), user_id=READER_ID, currency="INR", coupon_code="TEN"
    )
    assert discounted.tax.total_minor < undiscounted.tax.total_minor

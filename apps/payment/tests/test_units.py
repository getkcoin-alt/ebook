"""Pure-function tests: no database, no event loop.

Kept out of the async modules so these stay readable as plain unit tests — the
arithmetic they pin down is the part that ends up on an invoice.
"""

from __future__ import annotations

from datetime import UTC, datetime

from services.affiliates import AffiliateService
from services.invoices import financial_year
from services.orders import generate_order_number


def test_the_financial_year_starts_in_april():
    """January belongs to the previous FY — getting this wrong restarts the serial
    sequence three months early."""
    assert financial_year(datetime(2026, 4, 1, tzinfo=UTC)) == (2026, 2027)
    assert financial_year(datetime(2026, 3, 31, tzinfo=UTC)) == (2025, 2026)
    assert financial_year(datetime(2027, 1, 15, tzinfo=UTC)) == (2026, 2027)


def test_commission_uses_basis_points_not_a_float_rate():
    """2.5% is not representable as an integer percent, and a float rate
    reintroduces the rounding error minor units exist to avoid."""
    assert AffiliateService.commission_for(100_000, 250) == 2_500
    assert AffiliateService.commission_for(1_000_000, 500) == 50_000
    assert AffiliateService.commission_for(1, 500) == 0  # floored, never negative
    assert AffiliateService.commission_for(-100, 500) == 0  # never negative


def test_order_numbers_avoid_ambiguous_characters():
    """A number read aloud or copied from a printed invoice must come back right."""
    numbers = [generate_order_number("KOS") for _ in range(200)]
    tails = "".join(number.rsplit("-", 1)[-1] for number in numbers)
    assert not set(tails) & set("IO10")
    assert len(set(numbers)) == len(numbers)

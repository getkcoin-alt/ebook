"""GST arithmetic.

Pure functions, so these are plain unit tests — no database, no fixtures. They are
also the tests most worth reading: everything downstream trusts these numbers, and
a rounding error here reaches an invoice and then a tax filing.
"""

from __future__ import annotations

import pytest

from services.tax import compute_tax


def test_inclusive_price_keeps_the_customer_total_intact():
    """A page that says ₹499 must charge ₹499, with the tax extracted from it."""
    result = compute_tax(
        49900, percent=18, seller_state_code="GJ", place_of_supply="GJ", inclusive=True
    )
    assert result.taxable_minor + result.total_minor == 49900
    # 49900 / 1.18 = 42288.13..., rounded half-up.
    assert result.taxable_minor == 42288
    assert result.total_minor == 7612


def test_exclusive_price_adds_tax_on_top():
    result = compute_tax(
        49900, percent=18, seller_state_code="GJ", place_of_supply="GJ", inclusive=False
    )
    assert result.taxable_minor == 49900
    assert result.total_minor == 8982  # 49900 * 0.18
    assert result.gross_minor == 58882


def test_intra_state_supply_splits_into_cgst_and_sgst():
    result = compute_tax(
        11800, percent=18, seller_state_code="GJ", place_of_supply="GJ", inclusive=True
    )
    assert result.intra_state is True
    assert result.igst_minor == 0
    assert result.cgst_minor + result.sgst_minor == result.total_minor


def test_inter_state_supply_is_a_single_igst_line():
    result = compute_tax(
        11800, percent=18, seller_state_code="GJ", place_of_supply="MH", inclusive=True
    )
    assert result.intra_state is False
    assert result.cgst_minor == 0
    assert result.sgst_minor == 0
    assert result.igst_minor == result.total_minor


def test_total_tax_is_identical_whichever_way_it_is_split():
    """Only the recipient government differs, never the amount charged."""
    intra = compute_tax(
        99900, percent=18, seller_state_code="GJ", place_of_supply="GJ", inclusive=True
    )
    inter = compute_tax(
        99900, percent=18, seller_state_code="GJ", place_of_supply="MH", inclusive=True
    )
    assert intra.total_minor == inter.total_minor
    assert intra.taxable_minor == inter.taxable_minor


def test_odd_paise_split_still_reconciles():
    """An odd tax amount cannot be halved evenly; cgst + sgst must still equal it."""
    for amount in range(10_000, 10_050):
        result = compute_tax(
            amount, percent=18, seller_state_code="GJ", place_of_supply="GJ", inclusive=True
        )
        assert result.cgst_minor + result.sgst_minor == result.total_minor
        assert result.taxable_minor + result.total_minor == amount


def test_export_of_services_attracts_no_gst():
    result = compute_tax(
        49900,
        percent=18,
        seller_state_code="GJ",
        place_of_supply=None,
        country="US",
        inclusive=True,
    )
    assert result.total_minor == 0
    assert result.taxable_minor == 49900
    assert result.percent == 0


def test_unknown_place_of_supply_defaults_to_the_sellers_state():
    """The conservative choice: file in the state we are registered in rather than
    file IGST against a state we cannot name."""
    result = compute_tax(
        49900, percent=18, seller_state_code="GJ", place_of_supply=None, inclusive=True
    )
    assert result.intra_state is True
    assert result.place_of_supply == "GJ"


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amounts_produce_no_tax(amount):
    result = compute_tax(
        amount, percent=18, seller_state_code="GJ", place_of_supply="GJ", inclusive=True
    )
    assert result.total_minor == 0


def test_zero_rate_produces_no_tax():
    result = compute_tax(
        49900, percent=0, seller_state_code="GJ", place_of_supply="GJ", inclusive=True
    )
    assert result.total_minor == 0
    assert result.taxable_minor == 49900


def test_place_of_supply_is_compared_case_insensitively():
    result = compute_tax(
        11800, percent=18, seller_state_code="gj", place_of_supply="GJ", inclusive=True
    )
    assert result.intra_state is True

"""Indian GST computation.

Pure functions over integers. No database, no I/O, no floats — every number in and
out of this module is an integer of the currency's minor unit.

Three rules implement the law we care about:

* **Intra-state supply** (buyer's state == seller's state) splits into CGST + SGST,
  each half the rate. **Inter-state supply** is a single IGST line at the full rate.
  The total tax is identical either way; what differs is which government is paid,
  which is why misfiling it is a compliance problem and not a cosmetic one.
* **Export of services** (buyer outside India) attracts no GST.
* **Rounding happens once, on the order total** — never per line. Rounding each line
  and summing produces a total that does not match tax computed on the sum, and the
  two figures then disagree on the invoice.

The inclusive/exclusive distinction matters more than it looks. Indian retail prices
are quoted inclusive of GST: a page that says ₹499 must charge ₹499, with the tax
*extracted* from it. Adding 18% on top at checkout is both a compliance issue and
the single most reliable way to make a customer abandon a cart.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Anything else is an export of services: zero-rated, no GST line.
INDIA = "IN"


@dataclass(frozen=True, slots=True)
class TaxResult:
    """The split, plus the taxable base it was computed from.

    ``taxable_minor + total_minor`` always equals the gross amount, whichever
    direction the computation ran. That invariant is what makes an invoice add up.
    """

    percent: int
    taxable_minor: int
    cgst_minor: int
    sgst_minor: int
    igst_minor: int
    total_minor: int
    place_of_supply: str | None
    intra_state: bool

    @property
    def gross_minor(self) -> int:
        return self.taxable_minor + self.total_minor


def _split(tax_minor: int, *, intra_state: bool) -> tuple[int, int, int]:
    """Divide the tax into (cgst, sgst, igst).

    An odd number of paise cannot be halved evenly. The remainder goes to SGST by
    convention; what matters is that ``cgst + sgst == tax`` exactly, so the invoice
    reconciles.
    """
    if not intra_state:
        return 0, 0, tax_minor
    cgst = tax_minor // 2
    return cgst, tax_minor - cgst, 0


def compute_tax(
    amount_minor: int,
    *,
    percent: int,
    seller_state_code: str,
    place_of_supply: str | None,
    country: str = INDIA,
    inclusive: bool = True,
) -> TaxResult:
    """Split ``amount_minor`` into base and GST.

    With ``inclusive=True`` the amount already contains the tax and is decomposed;
    with ``inclusive=False`` the tax is added on top. ``place_of_supply`` is the
    buyer's state code, and decides the CGST/SGST versus IGST split.

    A buyer outside India, or a zero rate, yields a zero-tax result with the full
    amount as the taxable base — the caller then treats the order as untaxed rather
    than special-casing it.
    """
    if amount_minor <= 0 or percent <= 0 or (country or INDIA).upper() != INDIA:
        return TaxResult(
            percent=0,
            taxable_minor=max(0, amount_minor),
            cgst_minor=0,
            sgst_minor=0,
            igst_minor=0,
            total_minor=0,
            place_of_supply=place_of_supply,
            intra_state=False,
        )

    # An unknown place of supply defaults to the seller's own state. That is the
    # conservative choice: it keeps the tax in the state we are actually registered
    # in, rather than filing IGST against a state we cannot name.
    supply_state = (place_of_supply or seller_state_code).upper()
    intra_state = supply_state == seller_state_code.upper()

    if inclusive:
        # taxable = gross * 100 / (100 + percent), rounded half-up, then the tax is
        # the remainder. Deriving tax by subtraction rather than by a second
        # multiplication guarantees base + tax == gross with no stray paisa.
        taxable = (amount_minor * 100 + (100 + percent) // 2) // (100 + percent)
        tax = amount_minor - taxable
    else:
        taxable = amount_minor
        tax = (amount_minor * percent + 50) // 100

    cgst, sgst, igst = _split(tax, intra_state=intra_state)
    return TaxResult(
        percent=percent,
        taxable_minor=taxable,
        cgst_minor=cgst,
        sgst_minor=sgst,
        igst_minor=igst,
        total_minor=tax,
        place_of_supply=supply_state,
        intra_state=intra_state,
    )

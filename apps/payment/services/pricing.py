"""Turning a cart into an amount.

This module is the only place in the platform where a price becomes a charge, so it
holds a single invariant:

    subtotal - discount == taxable + tax == total

Every path through :meth:`PricingService.quote` maintains it, and the order it
produces is written to the database with those exact numbers. If they ever drift,
the invoice and the payment disagree and someone has to reconcile by hand.

The client's contribution to pricing is a list of book ids and quantities. Nothing
else. Prices come from the catalogue, discounts from the coupon table, tax from the
GST rules — all server-side.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import BadRequestError, get_logger
from schemas import BillingAddress, OrderItemIn, QuoteLine
from services.catalogue import CatalogueBook, CatalogueClient
from services.coupons import CouponService
from services.tax import TaxResult, compute_tax
from settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class PricedCart:
    """A fully resolved cart, ready to be written as an order."""

    lines: list[QuoteLine]
    subtotal_minor: int
    discount_minor: int
    taxable_minor: int
    tax: TaxResult
    total_minor: int
    currency: str
    #: True when catalogue prices already contained the GST.
    tax_inclusive: bool = True
    coupon_id: uuid.UUID | None = None
    coupon_code: str | None = None
    coupon_message: str | None = None
    coupon_applied: bool = False
    books: dict[uuid.UUID, CatalogueBook] = field(default_factory=dict)

    def assert_consistent(self) -> None:
        """Fail loudly rather than persist an order whose numbers do not add up.

        A defensive check that should never fire — which is exactly why it is worth
        having. A silent arithmetic slip here is money, and without this it would be
        discovered during a tax filing months later.
        """
        # Tax is inside the total when prices are inclusive, and added on top when
        # they are not. Both directions must land on the same total.
        expected = self.subtotal_minor - self.discount_minor
        if not self.tax_inclusive:
            expected += self.tax.total_minor
        if expected != self.total_minor:
            raise RuntimeError(
                f"Order totals are inconsistent: expected {expected}, got {self.total_minor}"
            )
        if self.taxable_minor + self.tax.total_minor != self.total_minor:
            raise RuntimeError(
                "Tax split is inconsistent: "
                f"{self.taxable_minor} + {self.tax.total_minor} != {self.total_minor}"
            )


class PricingService:
    def __init__(
        self, settings: Settings, catalogue: CatalogueClient, coupons: CouponService
    ) -> None:
        self._settings = settings
        self._catalogue = catalogue
        self._coupons = coupons

    async def quote(
        self,
        session: AsyncSession,
        *,
        items: list[OrderItemIn],
        user_id: uuid.UUID | None,
        currency: str,
        coupon_code: str | None = None,
        billing: BillingAddress | None = None,
        exclude_owned: bool = True,
        require_purchasable: bool = True,
    ) -> PricedCart:
        """Price a cart.

        ``exclude_owned`` marks lines the buyer already holds. They are flagged, not
        silently removed — a checkout that quietly drops a line the customer chose is
        worse than one that explains why.
        """
        if not items:
            raise BadRequestError("An order needs at least one item.")
        if len(items) > self._settings.max_order_items:
            raise BadRequestError(
                f"An order may contain at most {self._settings.max_order_items} items.",
                details={"max_items": self._settings.max_order_items},
            )

        book_ids = [item.book_id for item in items]
        books = (
            await self._catalogue.require_purchasable(book_ids)
            if require_purchasable
            else await self._catalogue.fetch(book_ids)
        )

        # A cart that mixes currencies cannot be charged as one payment. Better to
        # say so than to guess which one the customer meant.
        mismatched = {
            book.currency.upper() for book in books.values() if book.currency.upper() != currency
        }
        if mismatched:
            raise BadRequestError(
                "Every book in one order must be priced in the same currency.",
                code="currency_mismatch",
                details={"expected": currency, "found": sorted(mismatched)},
            )

        owned: set[uuid.UUID] = set()
        if exclude_owned and user_id is not None:
            owned = await self._catalogue.owned_book_ids(user_id, book_ids)

        lines: list[QuoteLine] = []
        line_totals: dict[uuid.UUID, int] = {}
        for item in items:
            book = books[item.book_id]
            line_total = book.price_minor * item.quantity
            lines.append(
                QuoteLine(
                    book_id=book.id,
                    title=book.title,
                    slug=book.slug,
                    quantity=item.quantity,
                    unit_price_minor=book.price_minor,
                    line_total_minor=line_total,
                    already_owned=book.id in owned,
                )
            )
            line_totals[book.id] = line_total

        subtotal = sum(line_totals.values())

        discount = 0
        coupon_id: uuid.UUID | None = None
        coupon_message: str | None = None
        coupon_applied = False
        if coupon_code:
            evaluation = await self._coupons.evaluate(
                session,
                code=coupon_code,
                user_id=user_id,
                lines=line_totals,
                books=books,
                currency=currency,
            )
            if evaluation.valid and evaluation.coupon is not None:
                discount = evaluation.discount_minor
                coupon_id = evaluation.coupon.id
                coupon_applied = True
            else:
                coupon_message = evaluation.reason

        payable = max(0, subtotal - discount)
        tax = compute_tax(
            payable,
            percent=self._settings.gst_percent,
            seller_state_code=self._settings.seller_state_code,
            place_of_supply=billing.state_code if billing else None,
            country=billing.country if billing else "IN",
            inclusive=self._settings.prices_include_tax,
        )

        if self._settings.prices_include_tax:
            # The quoted price already contains the tax, so the customer pays exactly
            # what the page said.
            total = payable
            taxable = tax.taxable_minor
        else:
            total = payable + tax.total_minor
            taxable = payable

        cart = PricedCart(
            lines=lines,
            subtotal_minor=subtotal,
            discount_minor=discount,
            taxable_minor=taxable,
            tax=tax,
            total_minor=total,
            currency=currency,
            tax_inclusive=self._settings.prices_include_tax,
            coupon_id=coupon_id,
            coupon_code=coupon_code.strip().upper() if coupon_code else None,
            coupon_message=coupon_message,
            coupon_applied=coupon_applied,
            books=books,
        )
        cart.assert_consistent()
        return cart

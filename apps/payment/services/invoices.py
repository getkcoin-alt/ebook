"""Invoice generation.

Indian GST rules require invoice numbers to be a **consecutive serial** within a
financial year (April to March), unique, and not reused. That constraint is why the
number is not a UUID or a timestamp: it has to be countable, and a gap in the
sequence is a question an auditor will ask.

An invoice is immutable once issued. A correction is a credit note, never an edit —
which is why this module has no ``update``.

The line items and billing details are **snapshotted into the invoice row** rather
than joined at render time. A customer who changes their billing address next month
must not retroactively alter a document that was already filed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import NotFoundError, get_logger
from models import Invoice, Order
from settings import Settings

logger = get_logger(__name__)

#: How many times to retry a serial-number collision before giving up. Collisions
#: only happen when two invoices are issued in the same instant; three attempts is
#: far more headroom than that needs.
MAX_SERIAL_ATTEMPTS = 5


def financial_year(moment: datetime) -> tuple[int, int]:
    """The Indian financial year containing ``moment``: April 1 to March 31.

    January 2027 belongs to FY 2026-27, not 2027-28 — getting this wrong restarts
    the serial sequence three months early.
    """
    year = moment.year
    return (year, year + 1) if moment.month >= 4 else (year - 1, year)


class InvoiceService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _prefix(self, moment: datetime) -> str:
        start, end = financial_year(moment)
        return f"{self._settings.invoice_prefix}/{start}-{str(end)[-2:]}/"

    async def _next_number(self, session: AsyncSession, moment: datetime) -> str:
        """Next serial in the current financial year.

        Counting rows rather than holding a sequence keeps this dialect-portable and
        keeps the count honest after a restore. The race between two concurrent
        issuances is caught by the unique constraint on ``invoice_number``, and the
        caller retries — which is the correct place to resolve it, because the
        database is the only thing that can arbitrate.
        """
        prefix = self._prefix(moment)
        used = int(
            (
                await session.execute(
                    select(func.count(Invoice.id)).where(Invoice.invoice_number.like(f"{prefix}%"))
                )
            ).scalar_one()
        )
        return f"{prefix}{used + 1:06d}"

    async def for_order(self, session: AsyncSession, order: Order) -> Invoice:
        """Issue the invoice for a paid order, or return the one already issued.

        Idempotent on purpose: this runs from the payment path, which a redelivered
        webhook can enter more than once. A second invoice for one order would be a
        real compliance problem, not just a duplicate row.
        """
        existing = (
            (await session.execute(select(Invoice).where(Invoice.order_id == order.id)))
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            return existing

        issued_at = datetime.now(UTC)
        billing = dict(order.billing_address or {})
        billing.update(
            {
                "name": order.billing_name,
                "email": order.billing_email,
                "seller_name": self._settings.seller_legal_name,
                "seller_gstin": self._settings.seller_gstin,
                "seller_state_code": self._settings.seller_state_code,
            }
        )
        line_items = [
            {
                "title": item.title,
                "quantity": item.quantity,
                "unit_price_minor": item.unit_price_minor,
                "line_total_minor": item.line_total_minor,
            }
            for item in order.items
        ]

        for attempt in range(MAX_SERIAL_ATTEMPTS):
            # A SAVEPOINT, not a plain flush: this runs inside the transaction that
            # is settling the order, and a bare rollback on a serial collision would
            # discard the payment along with the failed invoice.
            savepoint = await session.begin_nested()
            invoice = Invoice(
                order_id=order.id,
                user_id=order.user_id,
                invoice_number=await self._next_number(session, issued_at),
                subtotal_minor=order.subtotal_minor,
                discount_minor=order.discount_minor,
                tax_minor=order.tax_minor,
                total_minor=order.total_minor,
                currency=order.currency,
                cgst_minor=order.cgst_minor,
                sgst_minor=order.sgst_minor,
                igst_minor=order.igst_minor,
                tax_percent=order.tax_percent,
                place_of_supply=order.place_of_supply,
                billing_snapshot=billing,
                line_items=line_items,
                issued_at=issued_at,
            )
            session.add(invoice)
            try:
                await session.flush()
                await savepoint.commit()
            except IntegrityError:
                # Another request took this serial. Unwind just this attempt and
                # recount; the surrounding transaction is untouched.
                await savepoint.rollback()
                if attempt == MAX_SERIAL_ATTEMPTS - 1:
                    raise
                logger.warning("invoice.serial_collision", order_id=str(order.id), attempt=attempt)
                continue
            logger.info(
                "invoice.issued",
                order_id=str(order.id),
                invoice_number=invoice.invoice_number,
                total_minor=invoice.total_minor,
            )
            return invoice

        raise RuntimeError("Could not allocate an invoice number.")

    # ---- reads ----------------------------------------------------------

    async def get_for_user(
        self, session: AsyncSession, invoice_id: uuid.UUID, user_id: uuid.UUID
    ) -> Invoice:
        invoice = await session.get(Invoice, invoice_id)
        # 404 rather than 403 for someone else's invoice: a 403 would confirm it
        # exists, which is enough to enumerate customers.
        if invoice is None or invoice.user_id != user_id:
            raise NotFoundError("Invoice not found.", details={"invoice_id": str(invoice_id)})
        return invoice

    async def list_for_user(
        self, session: AsyncSession, *, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[list[Invoice], int]:
        total = int(
            (
                await session.execute(
                    select(func.count(Invoice.id)).where(Invoice.user_id == user_id)
                )
            ).scalar_one()
        )
        stmt = (
            select(Invoice)
            .where(Invoice.user_id == user_id)
            .order_by(Invoice.issued_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all()), total

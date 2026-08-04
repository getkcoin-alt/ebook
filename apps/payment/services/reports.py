"""Revenue reporting.

Aggregation happens in SQL. Loading orders into Python to sum them works fine on a
demo dataset and falls over the first time a year of orders is asked for — and the
dashboard that asks for a year is the one an operator opens on a bad day.

Net revenue is **gross minus refunds minus tax**. Tax collected is not revenue; it
is held on the government's behalf. Reporting it as income overstates the business
and is the kind of error that only surfaces at filing time.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import OrderStatus
from models import Order
from schemas import RevenueSummary

#: Orders where money actually moved.
PAID_STATES = (OrderStatus.PAID, OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED)


class ReportService:
    def __init__(self, currency: str) -> None:
        self._currency = currency

    async def revenue(
        self,
        session: AsyncSession,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> RevenueSummary:
        conditions = []
        if since is not None:
            conditions.append(Order.created_at >= since)
        if until is not None:
            conditions.append(Order.created_at <= until)

        paid = Order.status.in_(PAID_STATES)
        # SUM over an empty set is NULL, and NULL propagates through arithmetic into
        # a response full of nulls. coalesce keeps every figure an integer.
        stmt = select(
            func.coalesce(func.sum(case((paid, Order.total_minor), else_=0)), 0),
            func.coalesce(func.sum(case((paid, Order.refunded_minor), else_=0)), 0),
            func.coalesce(func.sum(case((paid, Order.tax_minor), else_=0)), 0),
            func.coalesce(func.sum(case((paid, Order.discount_minor), else_=0)), 0),
            func.count(Order.id),
            func.coalesce(func.sum(case((paid, 1), else_=0)), 0),
        ).where(*conditions)

        gross, refunded, tax, discount, order_count, paid_count = (
            await session.execute(stmt)
        ).one()

        return RevenueSummary(
            gross_minor=int(gross),
            refunded_minor=int(refunded),
            # Tax is collected for the government, so it is never revenue.
            net_minor=max(0, int(gross) - int(refunded) - int(tax)),
            tax_minor=int(tax),
            discount_minor=int(discount),
            order_count=int(order_count),
            paid_order_count=int(paid_count),
            currency=self._currency,
            period_start=since,
            period_end=until,
        )

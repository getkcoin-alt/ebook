"""Coupon validation and redemption.

Validation is deliberately forgiving and redemption is deliberately strict.

A shopper typing a wrong code gets a readable sentence and a 200, because a bad
coupon is a UI state and not an error. Redemption, by contrast, is where a coupon
becomes money, so it runs as a conditional UPDATE that cannot oversell a usage
limit no matter how many checkouts race.

The discount always applies to the *eligible* subtotal. A coupon scoped to one
category must not discount the rest of a cart, and computing it against the whole
subtotal is the easy mistake that gives away far more than intended.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ConflictError, NotFoundError, get_logger
from models import Coupon, CouponRedemption
from schemas import CouponCreate, CouponType, CouponUpdate
from services.catalogue import CatalogueBook
from settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CouponEvaluation:
    """The answer to "can this cart use this code, and for how much?".

    ``reason`` is written for a customer to read, not for a log.
    """

    valid: bool
    discount_minor: int = 0
    coupon: Coupon | None = None
    reason: str | None = None


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres hands back aware ones.

    Comparing the two raises ``TypeError``, which would turn every coupon check into
    a 500 on one backend and pass silently on the other.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class CouponService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- lookup ---------------------------------------------------------

    async def find(self, session: AsyncSession, code: str) -> Coupon | None:
        normalised = code.strip().upper()
        stmt = select(Coupon).where(Coupon.code == normalised)
        return (await session.execute(stmt)).scalars().one_or_none()

    async def get(self, session: AsyncSession, coupon_id: uuid.UUID) -> Coupon:
        coupon = await session.get(Coupon, coupon_id)
        if coupon is None:
            raise NotFoundError("Coupon not found.", details={"coupon_id": str(coupon_id)})
        return coupon

    # ---- evaluation -----------------------------------------------------

    @staticmethod
    def _eligible_subtotal(
        coupon: Coupon,
        lines: dict[uuid.UUID, int],
        books: dict[uuid.UUID, CatalogueBook],
    ) -> int:
        """Sum of the lines this coupon is allowed to discount.

        An empty scope on the coupon means "everything", which is the common case.
        """
        book_scope = {uuid.UUID(str(b)) for b in (coupon.applicable_book_ids or [])}
        category_scope = {uuid.UUID(str(c)) for c in (coupon.applicable_category_ids or [])}
        if not book_scope and not category_scope:
            return sum(lines.values())

        total = 0
        for book_id, line_total in lines.items():
            if book_id in book_scope:
                total += line_total
                continue
            book = books.get(book_id)
            if book is not None and category_scope.intersection(book.category_ids):
                total += line_total
        return total

    def _discount_for(self, coupon: Coupon, eligible_minor: int) -> int:
        if eligible_minor <= 0:
            return 0
        if coupon.coupon_type == CouponType.PERCENT:
            discount = eligible_minor * coupon.value // 100
            if coupon.max_discount_minor is not None:
                discount = min(discount, coupon.max_discount_minor)
        else:
            discount = coupon.value
        # Never discount below zero: a fixed ₹500 coupon on a ₹200 cart makes the
        # cart free, not a ₹300 payout.
        return max(0, min(discount, eligible_minor))

    async def _user_redemptions(
        self, session: AsyncSession, coupon_id: uuid.UUID, user_id: uuid.UUID
    ) -> int:
        stmt = select(func.count(CouponRedemption.id)).where(
            CouponRedemption.coupon_id == coupon_id,
            CouponRedemption.user_id == user_id,
        )
        return int((await session.execute(stmt)).scalar_one())

    async def evaluate(
        self,
        session: AsyncSession,
        *,
        code: str | None,
        user_id: uuid.UUID | None,
        lines: dict[uuid.UUID, int],
        books: dict[uuid.UUID, CatalogueBook],
        currency: str,
    ) -> CouponEvaluation:
        """Decide whether ``code`` applies, and for how much. Never raises."""
        if not code:
            return CouponEvaluation(valid=False)

        coupon = await self.find(session, code)
        if coupon is None:
            return CouponEvaluation(valid=False, reason="That code is not recognised.")
        if not coupon.is_active:
            return CouponEvaluation(
                valid=False, coupon=coupon, reason="That code is no longer active."
            )

        now = datetime.now(UTC)
        valid_from = _utc(coupon.valid_from)
        valid_until = _utc(coupon.valid_until)
        if valid_from and now < valid_from:
            return CouponEvaluation(
                valid=False, coupon=coupon, reason="That code is not active yet."
            )
        if valid_until and now > valid_until:
            return CouponEvaluation(valid=False, coupon=coupon, reason="That code has expired.")

        if coupon.currency and coupon.currency.upper() != currency.upper():
            return CouponEvaluation(
                valid=False,
                coupon=coupon,
                reason=f"That code can only be used for payments in {coupon.currency}.",
            )

        if coupon.usage_limit is not None and coupon.usage_count >= coupon.usage_limit:
            return CouponEvaluation(
                valid=False, coupon=coupon, reason="That code has been fully redeemed."
            )

        if user_id is not None and coupon.per_user_limit:
            used = await self._user_redemptions(session, coupon.id, user_id)
            if used >= coupon.per_user_limit:
                return CouponEvaluation(
                    valid=False, coupon=coupon, reason="You have already used this code."
                )

        subtotal = sum(lines.values())
        if subtotal < coupon.min_order_minor:
            return CouponEvaluation(
                valid=False,
                coupon=coupon,
                reason=(
                    "This code needs a minimum order of "
                    f"{coupon.min_order_minor / 100:.2f} {currency}."
                ),
            )

        eligible = self._eligible_subtotal(coupon, lines, books)
        if eligible <= 0:
            return CouponEvaluation(
                valid=False,
                coupon=coupon,
                reason="This code does not apply to anything in your cart.",
            )

        discount = self._discount_for(coupon, eligible)
        if discount <= 0:
            return CouponEvaluation(
                valid=False, coupon=coupon, reason="This code gives no discount on this cart."
            )
        return CouponEvaluation(valid=True, discount_minor=discount, coupon=coupon)

    # ---- redemption -----------------------------------------------------

    async def redeem(
        self,
        session: AsyncSession,
        *,
        coupon: Coupon,
        order_id: uuid.UUID,
        user_id: uuid.UUID,
        discount_minor: int,
    ) -> CouponRedemption:
        """Consume one use. Safe against concurrent checkouts.

        The usage counter moves with a conditional UPDATE rather than a read, an
        increment and a write. Under a burst — a code posted to a large audience —
        read-modify-write lets a hundred checkouts all read the same count and every
        one of them oversell the limit.
        """
        if coupon.usage_limit is not None:
            result = await session.execute(
                update(Coupon)
                .where(Coupon.id == coupon.id, Coupon.usage_count < coupon.usage_limit)
                .values(usage_count=Coupon.usage_count + 1)
            )
            if result.rowcount == 0:
                raise ConflictError(
                    "This coupon was fully redeemed while you were checking out.",
                    code="coupon_exhausted",
                    details={"code": coupon.code},
                )
        else:
            await session.execute(
                update(Coupon)
                .where(Coupon.id == coupon.id)
                .values(usage_count=Coupon.usage_count + 1)
            )

        redemption = CouponRedemption(
            coupon_id=coupon.id,
            order_id=order_id,
            user_id=user_id,
            discount_minor=discount_minor,
        )
        # A SAVEPOINT so a duplicate redemption does not roll back the order that is
        # being created in the same transaction.
        savepoint = await session.begin_nested()
        session.add(redemption)
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError as exc:
            # (coupon_id, order_id) is unique, so a retried checkout lands here.
            # That is the constraint doing its job, not an error worth surfacing.
            await savepoint.rollback()
            raise ConflictError(
                "This coupon has already been applied to that order.",
                code="coupon_already_redeemed",
            ) from exc
        logger.info(
            "coupon.redeemed",
            code=coupon.code,
            order_id=str(order_id),
            discount_minor=discount_minor,
        )
        return redemption

    async def release(self, session: AsyncSession, *, order_id: uuid.UUID) -> None:
        """Give a coupon use back when its order is cancelled or expires.

        Without this, an abandoned checkout permanently consumes a single-use code
        and the customer can never use it again.
        """
        stmt = select(CouponRedemption).where(CouponRedemption.order_id == order_id)
        redemptions = list((await session.execute(stmt)).scalars().all())
        for redemption in redemptions:
            await session.execute(
                update(Coupon)
                .where(Coupon.id == redemption.coupon_id, Coupon.usage_count > 0)
                .values(usage_count=Coupon.usage_count - 1)
            )
            await session.delete(redemption)
        if redemptions:
            logger.info("coupon.released", order_id=str(order_id), count=len(redemptions))

    # ---- administration -------------------------------------------------

    async def create(self, session: AsyncSession, payload: CouponCreate) -> Coupon:
        existing = await self.find(session, payload.code)
        if existing is not None:
            raise ConflictError(
                "A coupon with that code already exists.", details={"code": payload.code}
            )
        coupon = Coupon(
            code=payload.code,
            description=payload.description,
            coupon_type=payload.coupon_type,
            value=payload.value,
            max_discount_minor=payload.max_discount_minor,
            min_order_minor=payload.min_order_minor,
            currency=str(payload.currency) if payload.currency else None,
            usage_limit=payload.usage_limit,
            per_user_limit=payload.per_user_limit,
            valid_from=payload.valid_from,
            valid_until=payload.valid_until,
            is_active=payload.is_active,
            applicable_book_ids=[str(b) for b in payload.applicable_book_ids],
            applicable_category_ids=[str(c) for c in payload.applicable_category_ids],
        )
        session.add(coupon)
        await session.commit()
        await session.refresh(coupon)
        logger.info("coupon.created", code=coupon.code, coupon_type=str(coupon.coupon_type))
        return coupon

    async def update(self, session: AsyncSession, coupon: Coupon, payload: CouponUpdate) -> Coupon:
        data = payload.model_dump(exclude_unset=True)
        for field in ("applicable_book_ids", "applicable_category_ids"):
            if field in data and data[field] is not None:
                data[field] = [str(item) for item in data[field]]
        for field, value in data.items():
            setattr(coupon, field, value)
        await session.commit()
        await session.refresh(coupon)
        return coupon

    async def list(
        self, session: AsyncSession, *, active_only: bool = False, limit: int = 50, offset: int = 0
    ) -> tuple[list[Coupon], int]:
        conditions = [Coupon.is_active.is_(True)] if active_only else []
        total = int(
            (await session.execute(select(func.count(Coupon.id)).where(*conditions))).scalar_one()
        )
        stmt = (
            select(Coupon)
            .where(*conditions)
            .order_by(Coupon.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        return rows, total

    async def deactivate(self, session: AsyncSession, coupon: Coupon) -> Coupon:
        """Coupons are never deleted — a redeemed one is part of the order record."""
        coupon.is_active = False
        await session.commit()
        await session.refresh(coupon)
        return coupon

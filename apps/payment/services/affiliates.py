"""Affiliate accounts and commission.

Commission is recorded when an order is paid but is **not payable immediately**. It
sits in ``pending`` until the refund window closes, then becomes ``approved``. Paying
out on the day of sale means clawing money back from an affiliate when the buyer
refunds a week later, and clawbacks are how affiliate programmes acquire a bad
reputation.

Commission is calculated on the **net of tax**, not the gross. GST is collected on
the government's behalf and passed straight through; paying a percentage of it would
be paying commission on money that was never revenue.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ConflictError, NotFoundError, get_logger
from models import AffiliateAccount, AffiliateConversion, Order
from schemas import AffiliateConversionStatus
from settings import Settings

logger = get_logger(__name__)


def generate_affiliate_code() -> str:
    return secrets.token_urlsafe(6).lower().replace("_", "").replace("-", "")[:8]


class AffiliateService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- accounts -------------------------------------------------------

    async def find_by_code(self, session: AsyncSession, code: str) -> AffiliateAccount | None:
        stmt = select(AffiliateAccount).where(AffiliateAccount.code == code.strip().lower())
        return (await session.execute(stmt)).scalars().one_or_none()

    async def for_user(self, session: AsyncSession, user_id: uuid.UUID) -> AffiliateAccount | None:
        stmt = select(AffiliateAccount).where(AffiliateAccount.user_id == user_id)
        return (await session.execute(stmt)).scalars().one_or_none()

    async def register(
        self, session: AsyncSession, *, user_id: uuid.UUID, code: str | None = None
    ) -> AffiliateAccount:
        """Enrol a user, or return the account they already have.

        Idempotent rather than a 409: a user clicking "become an affiliate" twice
        wants their account, not an error.
        """
        existing = await self.for_user(session, user_id)
        if existing is not None:
            return existing

        if code and await self.find_by_code(session, code) is not None:
            raise ConflictError("That affiliate code is already taken.", details={"code": code})

        account = AffiliateAccount(
            user_id=user_id,
            code=code or generate_affiliate_code(),
            commission_bps=self._settings.affiliate_commission_bps,
        )
        session.add(account)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            # Either the generated code collided or two enrolments raced. Both are
            # resolved by returning whichever account now exists.
            settled = await self.for_user(session, user_id)
            if settled is not None:
                return settled
            raise ConflictError("Could not create an affiliate account; please retry.") from exc
        await session.refresh(account)
        logger.info("affiliate.registered", user_id=str(user_id), code=account.code)
        return account

    async def get(self, session: AsyncSession, account_id: uuid.UUID) -> AffiliateAccount:
        account = await session.get(AffiliateAccount, account_id)
        if account is None:
            raise NotFoundError("Affiliate account not found.")
        return account

    # ---- conversions ----------------------------------------------------

    @staticmethod
    def commission_for(net_minor: int, commission_bps: int) -> int:
        """Basis points, floored.

        Basis points rather than a percentage because 2.5% is not representable as an
        integer percent, and a float rate reintroduces exactly the rounding error the
        minor-unit convention exists to avoid.
        """
        return max(0, net_minor * commission_bps // 10_000)

    async def record(self, session: AsyncSession, order: Order) -> AffiliateConversion | None:
        """Credit the referrer for a paid order.

        Returns ``None`` when there is nothing to credit — no code, an unknown or
        inactive code, a self-referral, or a conversion already recorded. All of
        those are ordinary, so none of them raise.
        """
        if not order.affiliate_code:
            return None

        account = await self.find_by_code(session, order.affiliate_code)
        if account is None or not account.is_active:
            logger.info("affiliate.code_not_credited", code=order.affiliate_code)
            return None
        if account.user_id == order.user_id:
            # Buying through your own link is not a referral.
            logger.info("affiliate.self_referral_ignored", user_id=str(order.user_id))
            return None

        existing = (
            (
                await session.execute(
                    select(AffiliateConversion).where(AffiliateConversion.order_id == order.id)
                )
            )
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            return existing

        # Net of tax: GST is collected for the government, not earned.
        net = max(0, order.total_minor - order.tax_minor)
        commission = self.commission_for(net, account.commission_bps)

        conversion = AffiliateConversion(
            account_id=account.id,
            order_id=order.id,
            order_total_minor=order.total_minor,
            commission_minor=commission,
            currency=order.currency,
            status=AffiliateConversionStatus.PENDING.value,
        )
        # A SAVEPOINT, opened before the row is added — begin_nested() autoflushes
        # first, so adding it beforehand would put the INSERT outside the savepoint.
        # Catching the IntegrityError without one leaves the session unusable, and
        # this runs inside the transaction that is settling the order.
        savepoint = await session.begin_nested()
        session.add(conversion)
        account.total_earned_minor += commission
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError:
            # The unique constraint on order_id caught a redelivered webhook.
            await savepoint.rollback()
            return None
        logger.info(
            "affiliate.conversion_recorded",
            code=account.code,
            order_id=str(order.id),
            commission_minor=commission,
        )
        return conversion

    async def reverse(self, session: AsyncSession, order_id: uuid.UUID) -> None:
        """Undo a conversion when its order is refunded."""
        conversion = (
            (
                await session.execute(
                    select(AffiliateConversion).where(AffiliateConversion.order_id == order_id)
                )
            )
            .scalars()
            .one_or_none()
        )
        if conversion is None or conversion.status == AffiliateConversionStatus.REVERSED.value:
            return
        conversion.status = AffiliateConversionStatus.REVERSED.value
        conversion.reversed_at = datetime.now(UTC)
        account = await session.get(AffiliateAccount, conversion.account_id)
        if account is not None:
            # Never below zero: a reversal after a payout would otherwise produce a
            # negative lifetime-earnings figure that no report can explain.
            account.total_earned_minor = max(
                0, account.total_earned_minor - conversion.commission_minor
            )
        await session.flush()
        logger.info("affiliate.conversion_reversed", order_id=str(order_id))

    async def approve_matured(self, session: AsyncSession, *, batch: int = 500) -> int:
        """Promote conversions past the refund window to ``approved``.

        Run from the worker. Until this happens the commission is recorded but not
        payable.
        """
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.affiliate_hold_days)
        stmt = (
            select(AffiliateConversion)
            .where(
                AffiliateConversion.status == AffiliateConversionStatus.PENDING.value,
                AffiliateConversion.created_at < cutoff,
            )
            .limit(batch)
        )
        matured = list((await session.execute(stmt)).scalars().all())
        for conversion in matured:
            conversion.status = AffiliateConversionStatus.APPROVED.value
        if matured:
            await session.commit()
            logger.info("affiliate.conversions_approved", count=len(matured))
        return len(matured)

    async def conversions_for(
        self,
        session: AsyncSession,
        *,
        account_id: uuid.UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[AffiliateConversion], int]:
        conditions = [AffiliateConversion.account_id == account_id]
        total = int(
            (
                await session.execute(select(func.count(AffiliateConversion.id)).where(*conditions))
            ).scalar_one()
        )
        stmt = (
            select(AffiliateConversion)
            .where(*conditions)
            .order_by(AffiliateConversion.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all()), total

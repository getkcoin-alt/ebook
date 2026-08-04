"""Subscription plans and memberships.

A subscription is a *recurring authorisation held by the gateway*, not a timer this
service runs. We record what the gateway tells us and read our own rows to answer
"is this person subscribed right now?" — we never try to charge on a schedule
ourselves, because that would mean storing payment credentials.

Cancellation defaults to the end of the paid period. The customer paid for that
period; taking it away the moment they click cancel is a refund we did not give.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ConflictError, NotFoundError, PaymentProvider, get_logger
from models import Subscription, SubscriptionPlan
from schemas import PlanCreate, PlanUpdate, SubscriptionStatus
from settings import Settings

logger = get_logger(__name__)

#: States in which a subscriber still has access.
LIVE = (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING, SubscriptionStatus.PAST_DUE)

#: Gateway status strings mapped onto ours. Both providers are covered here so the
#: webhook handler never has to know which one it is talking to.
PROVIDER_STATUS = {
    "created": SubscriptionStatus.PENDING,
    "authenticated": SubscriptionStatus.PENDING,
    "incomplete": SubscriptionStatus.PENDING,
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "halted": SubscriptionStatus.PAST_DUE,
    "pending": SubscriptionStatus.PAST_DUE,
    "cancelled": SubscriptionStatus.CANCELLED,
    "canceled": SubscriptionStatus.CANCELLED,
    "completed": SubscriptionStatus.EXPIRED,
    "expired": SubscriptionStatus.EXPIRED,
    "unpaid": SubscriptionStatus.EXPIRED,
}


class SubscriptionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- plans ----------------------------------------------------------

    async def list_plans(
        self, session: AsyncSession, *, active_only: bool = True
    ) -> list[SubscriptionPlan]:
        conditions = [SubscriptionPlan.is_active.is_(True)] if active_only else []
        stmt = (
            select(SubscriptionPlan).where(*conditions).order_by(SubscriptionPlan.price_minor.asc())
        )
        return list((await session.execute(stmt)).scalars().all())

    async def get_plan(self, session: AsyncSession, plan_id: uuid.UUID) -> SubscriptionPlan:
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan is None:
            raise NotFoundError("Subscription plan not found.", details={"plan_id": str(plan_id)})
        return plan

    async def create_plan(self, session: AsyncSession, payload: PlanCreate) -> SubscriptionPlan:
        existing = (
            (
                await session.execute(
                    select(SubscriptionPlan).where(SubscriptionPlan.code == payload.code)
                )
            )
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            raise ConflictError(
                "A plan with that code already exists.", details={"code": payload.code}
            )
        plan = SubscriptionPlan(
            code=payload.code,
            name=payload.name,
            description=payload.description,
            price_minor=payload.price_minor,
            currency=str(payload.currency),
            interval=payload.interval,
            trial_days=payload.trial_days,
            is_active=payload.is_active,
            provider_plan_ids=payload.provider_plan_ids,
        )
        session.add(plan)
        await session.commit()
        await session.refresh(plan)
        logger.info("plan.created", code=plan.code, price_minor=plan.price_minor)
        return plan

    async def update_plan(
        self, session: AsyncSession, plan: SubscriptionPlan, payload: PlanUpdate
    ) -> SubscriptionPlan:
        """Price changes affect new subscriptions only.

        Existing memberships keep the price they were sold at, because the recurring
        amount lives on the gateway's side and is not re-read from this row.
        """
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(plan, field, value)
        await session.commit()
        await session.refresh(plan)
        return plan

    # ---- subscriptions --------------------------------------------------

    async def get(self, session: AsyncSession, subscription_id: uuid.UUID) -> Subscription:
        subscription = await session.get(Subscription, subscription_id)
        if subscription is None:
            raise NotFoundError("Subscription not found.")
        return subscription

    async def get_for_user(
        self, session: AsyncSession, subscription_id: uuid.UUID, user_id: uuid.UUID
    ) -> Subscription:
        subscription = await self.get(session, subscription_id)
        if subscription.user_id != user_id:
            raise NotFoundError("Subscription not found.")
        return subscription

    async def active_for_user(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> Subscription | None:
        """The live membership, if any.

        The period end is checked as well as the status: a gateway that fails to
        send its cancellation webhook would otherwise leave someone with permanent
        access to everything.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_(LIVE),
                or_(
                    Subscription.current_period_end.is_(None),
                    Subscription.current_period_end > now,
                ),
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def list_for_user(self, session: AsyncSession, user_id: uuid.UUID) -> list[Subscription]:
        stmt = (
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    async def create(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        plan: SubscriptionPlan,
        provider: PaymentProvider,
        provider_subscription_id: str | None = None,
    ) -> Subscription:
        existing = await self.active_for_user(session, user_id)
        if existing is not None:
            raise ConflictError(
                "You already have an active subscription.",
                code="subscription_exists",
                details={"subscription_id": str(existing.id)},
            )

        now = datetime.now(UTC)
        trial_ends = now + timedelta(days=plan.trial_days) if plan.trial_days else None
        subscription = Subscription(
            user_id=user_id,
            plan_id=plan.id,
            provider=provider,
            provider_subscription_id=provider_subscription_id,
            # A trial starts live; a paid plan waits for the gateway to confirm the
            # first charge. Activating on creation would give away a month.
            status=SubscriptionStatus.TRIALING if trial_ends else SubscriptionStatus.PENDING,
            current_period_start=now,
            current_period_end=trial_ends,
            trial_ends_at=trial_ends,
        )
        session.add(subscription)
        await session.commit()
        await session.refresh(subscription)
        logger.info(
            "subscription.created",
            subscription_id=str(subscription.id),
            plan=plan.code,
            provider=provider.value,
        )
        return subscription

    async def cancel(
        self,
        session: AsyncSession,
        subscription: Subscription,
        *,
        at_period_end: bool = True,
    ) -> Subscription:
        if subscription.status in (SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED):
            return subscription
        now = datetime.now(UTC)
        if at_period_end and subscription.current_period_end:
            # Access continues; renewal does not happen.
            subscription.cancel_at_period_end = True
        else:
            subscription.status = SubscriptionStatus.CANCELLED
            subscription.current_period_end = now
        subscription.cancelled_at = now
        await session.commit()
        await session.refresh(subscription)
        logger.info(
            "subscription.cancelled",
            subscription_id=str(subscription.id),
            at_period_end=at_period_end,
        )
        return subscription

    async def apply_provider_status(
        self,
        session: AsyncSession,
        *,
        provider: PaymentProvider,
        provider_subscription_id: str,
        provider_status: str,
        current_period_start: datetime | None = None,
        current_period_end: datetime | None = None,
    ) -> Subscription | None:
        """Reconcile a subscription webhook. ``None`` when it is not one of ours."""
        stmt = select(Subscription).where(
            Subscription.provider == provider,
            Subscription.provider_subscription_id == provider_subscription_id,
        )
        subscription = (await session.execute(stmt)).scalars().one_or_none()
        if subscription is None:
            logger.warning(
                "subscription.webhook_for_unknown_subscription",
                provider_subscription_id=provider_subscription_id,
            )
            return None

        mapped = PROVIDER_STATUS.get(provider_status.lower())
        if mapped is not None:
            subscription.status = mapped
        if current_period_start is not None:
            subscription.current_period_start = current_period_start
        if current_period_end is not None:
            subscription.current_period_end = current_period_end
        await session.flush()
        logger.info(
            "subscription.status_applied",
            subscription_id=str(subscription.id),
            provider_status=provider_status,
            status=str(subscription.status),
        )
        return subscription

    async def expire_lapsed(self, session: AsyncSession, *, batch: int = 500) -> int:
        """Close out subscriptions whose period ended and were set to not renew.

        Run from the worker. This is a safety net for a missed webhook, not the
        primary mechanism.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Subscription)
            .where(
                Subscription.status.in_(LIVE),
                Subscription.cancel_at_period_end.is_(True),
                Subscription.current_period_end.is_not(None),
                Subscription.current_period_end < now,
            )
            .limit(batch)
        )
        lapsed = list((await session.execute(stmt)).scalars().all())
        for subscription in lapsed:
            subscription.status = SubscriptionStatus.EXPIRED
        if lapsed:
            await session.commit()
            logger.info("subscription.expired_batch", count=len(lapsed))
        return len(lapsed)

    async def count_active(self, session: AsyncSession) -> int:
        stmt = select(func.count(Subscription.id)).where(Subscription.status.in_(LIVE))
        return int((await session.execute(stmt)).scalar_one())

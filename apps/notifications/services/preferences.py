"""Who may be contacted, on which channel, about what.

The decision is made in one place because it has to be. A send path that checks
preferences in one branch and suppression in another eventually grows a third branch
that checks neither, and the failure mode is mailing someone who asked you not to.

Precedence, strictly in this order:

1. **Suppression wins over everything**, including transactional messages. An
   address that hard-bounced or issued a spam complaint is never contacted again.
   Continuing costs the sending domain its reputation, which takes password resets
   down with it.
2. **Transactional categories cannot be opted out of.** A receipt and a password
   reset are not marketing.
3. **The user's stored preference**, if any.
4. **The default**, which is on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import NotificationChannel, get_logger
from models import Preference, Suppression
from schemas import PreferenceUpdate, SuppressionReason
from settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether to send, and why not when the answer is no.

    ``reason`` is recorded on the skipped delivery row, so "why did this customer
    not get their receipt?" is answerable from the database rather than from logs.
    """

    allowed: bool
    reason: str | None = None


class PreferenceService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_transactional(self, category: str) -> bool:
        return category in set(self._settings.transactional_categories)

    # ---- the decision ---------------------------------------------------

    async def may_send(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | None,
        category: str,
        channel: NotificationChannel,
        destination: str | None,
    ) -> Decision:
        if not self._settings.sending_enabled:
            # The switch to pull during an incident, or when a production database
            # has been restored into staging and must not re-mail every customer.
            return Decision(False, "sending_disabled")

        if destination and self._settings.suppression_enforced:
            suppressed = await self.suppression_for(
                session, channel=channel, destination=destination
            )
            if suppressed is not None:
                return Decision(False, f"suppressed:{suppressed.reason}")

        if self.is_transactional(category):
            return Decision(True)

        if user_id is not None:
            preference = await self.get(
                session, user_id=user_id, category=category, channel=channel
            )
            if preference is not None and not preference.enabled:
                return Decision(False, "opted_out")

        return Decision(True)

    # ---- preferences ----------------------------------------------------

    async def get(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        category: str,
        channel: NotificationChannel,
    ) -> Preference | None:
        stmt = select(Preference).where(
            Preference.user_id == user_id,
            Preference.category == category,
            Preference.channel == channel,
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def list_for_user(self, session: AsyncSession, user_id: uuid.UUID) -> list[Preference]:
        stmt = (
            select(Preference)
            .where(Preference.user_id == user_id)
            .order_by(Preference.category, Preference.channel)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def set(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        updates: list[PreferenceUpdate],
    ) -> list[Preference]:
        """Upsert a batch of preferences.

        An attempt to disable a transactional category is accepted and ignored
        rather than rejected: the UI shows those toggles locked, and a 400 for a
        state the user cannot reach is noise. It is logged, because a client
        repeatedly trying is a bug worth seeing.
        """
        result: list[Preference] = []
        for update in updates:
            if self.is_transactional(update.category) and not update.enabled:
                logger.info(
                    "preference.transactional_opt_out_ignored",
                    user_id=str(user_id),
                    category=update.category,
                )
                continue
            existing = await self.get(
                session, user_id=user_id, category=update.category, channel=update.channel
            )
            if existing is None:
                existing = Preference(
                    user_id=user_id,
                    category=update.category,
                    channel=update.channel,
                    enabled=update.enabled,
                )
                session.add(existing)
            else:
                existing.enabled = update.enabled
            result.append(existing)
        await session.commit()
        return result

    async def disable_all(
        self, session: AsyncSession, *, user_id: uuid.UUID, channel: NotificationChannel
    ) -> int:
        """Global unsubscribe for one channel.

        Only touches categories the user already has rows for, plus a row for every
        non-transactional category we know about — a blanket "unsubscribe from
        everything" has to cover categories the user has never seen a message from.
        """
        categories = {
            preference.category
            for preference in await self.list_for_user(session, user_id)
            if not self.is_transactional(preference.category)
        }
        categories.update(KNOWN_CATEGORIES - set(self._settings.transactional_categories))

        await self.set(
            session,
            user_id=user_id,
            updates=[
                PreferenceUpdate(category=category, channel=channel, enabled=False)
                for category in sorted(categories)
            ],
        )
        logger.info(
            "preference.unsubscribed_all",
            user_id=str(user_id),
            channel=str(channel),
            categories=len(categories),
        )
        return len(categories)

    # ---- suppression ----------------------------------------------------

    async def suppression_for(
        self, session: AsyncSession, *, channel: NotificationChannel, destination: str
    ) -> Suppression | None:
        stmt = select(Suppression).where(
            Suppression.channel == channel,
            Suppression.destination == destination.strip().lower(),
        )
        return (await session.execute(stmt)).scalars().one_or_none()

    async def suppress(
        self,
        session: AsyncSession,
        *,
        channel: NotificationChannel,
        destination: str,
        reason: SuppressionReason,
        detail: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> Suppression:
        """Add an address to the block list. Idempotent."""
        normalised = destination.strip().lower()
        existing = await self.suppression_for(session, channel=channel, destination=normalised)
        if existing is not None:
            return existing

        suppression = Suppression(
            channel=channel,
            destination=normalised,
            reason=reason,
            detail=(detail or "")[:1000] or None,
            user_id=user_id,
        )
        session.add(suppression)
        await session.commit()
        await session.refresh(suppression)
        logger.warning(
            "notification.address_suppressed",
            channel=str(channel),
            reason=str(reason),
            destination=normalised,
        )
        return suppression

    async def unsuppress(
        self, session: AsyncSession, *, channel: NotificationChannel, destination: str
    ) -> bool:
        """Remove a block. An operator action, never automatic.

        Automatic removal would defeat the point: a bounce that resolves itself on
        retry is exactly the pattern that gets a domain blocklisted.
        """
        existing = await self.suppression_for(session, channel=channel, destination=destination)
        if existing is None:
            return False
        await session.delete(existing)
        await session.commit()
        logger.info("notification.suppression_lifted", destination=destination.strip().lower())
        return True

    async def list_suppressions(
        self, session: AsyncSession, *, limit: int = 50, offset: int = 0
    ) -> list[Suppression]:
        stmt = (
            select(Suppression).order_by(Suppression.created_at.desc()).limit(limit).offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all())


#: Categories the platform sends. Used to make a blanket unsubscribe cover
#: categories the user has never received a message from.
KNOWN_CATEGORIES: set[str] = {
    "account.verify",
    "account.password_reset",
    "account.security",
    "account.welcome",
    "order.receipt",
    "order.refund",
    "book.published",
    "book.recommendation",
    "marketing.digest",
    "marketing.promotion",
    "general",
}

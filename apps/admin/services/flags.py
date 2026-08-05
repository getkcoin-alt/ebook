"""Feature flags.

The one thing this service genuinely owns. Every service reads flags and no service
owns them, so putting them in any one service's schema would make that service a
dependency of every other for a reason unrelated to its domain.

**This service returns the decision, never the rule.** A sibling asking "is
`new_checkout` on for this user?" gets `true` or `false`. Handing back the rollout
percentage instead would make every service implement the bucketing itself, and two
implementations of a hash bucket always diverge eventually — which shows up as a user
who has the feature on one page and not on the next, and is close to impossible to
diagnose from either side.

**Bucketing is deterministic and stable.** The same user gets the same answer for the
same flag every time, across restarts and across services, because it is a hash of the
flag key and the user id rather than anything random or stored. Raising a rollout only
ever adds users; it never reshuffles who is in. A rollout that reshuffles is worse than
no rollout — users lose a feature they were shown, which reads as a bug in the feature
rather than in the flag.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ConflictError, NotFoundError, get_logger
from knowledgeos_core.redis import RedisClient
from models import FeatureFlag, FlagAudit
from schemas import FlagCreate, FlagEvaluation, FlagUpdate
from settings import Settings

logger = get_logger(__name__)

#: Cache key for the whole flag set. One key rather than one per flag: the set is
#: small, it is read together, and invalidating one key on any change is far easier to
#: reason about than invalidating the right subset.
_CACHE_KEY = "flags:all"


class FlagService:
    def __init__(self, settings: Settings, redis: RedisClient | None = None) -> None:
        self._settings = settings
        self._redis = redis

    # ---- reading ---------------------------------------------------------

    async def list_flags(self, session: AsyncSession) -> list[FeatureFlag]:
        stmt = select(FeatureFlag).order_by(FeatureFlag.key)
        return list((await session.execute(stmt)).scalars().all())

    async def get(self, session: AsyncSession, key: str) -> FeatureFlag:
        flag = await self._find(session, key)
        if flag is None:
            raise NotFoundError("No such feature flag.", details={"key": key})
        return flag

    async def _find(self, session: AsyncSession, key: str) -> FeatureFlag | None:
        stmt = select(FeatureFlag).where(FeatureFlag.key == key.strip().lower())
        return (await session.execute(stmt)).scalars().one_or_none()

    async def evaluate(
        self, session: AsyncSession, key: str, *, user_id: uuid.UUID | None = None
    ) -> FlagEvaluation:
        """Decide one flag for one user.

        An unknown flag is **off**, and says so. Defaulting an unknown flag to on
        would mean a typo in a service's flag name silently enables an unfinished
        feature, which is the worst possible direction for that mistake to fail.
        """
        flag = await self._find(session, key)
        if flag is None:
            return FlagEvaluation(key=key, enabled=False, reason="unknown_flag")
        if not flag.enabled:
            return FlagEvaluation(key=key, enabled=False, reason="disabled")

        allowlist = {str(entry) for entry in (flag.allowlist or [])}
        if user_id is not None and str(user_id) in allowlist:
            return FlagEvaluation(key=key, enabled=True, reason="allowlisted")

        if flag.rollout_percent >= 100:
            return FlagEvaluation(key=key, enabled=True, reason="fully_rolled_out")
        if flag.rollout_percent <= 0:
            return FlagEvaluation(key=key, enabled=False, reason="rollout_zero")

        if user_id is None:
            # An anonymous caller cannot be bucketed stably, and putting them in a
            # random bucket means the same page flickers between variants on reload.
            # Off is the safe answer for a partial rollout.
            return FlagEvaluation(key=key, enabled=False, reason="no_user_for_rollout")

        bucket = bucket_of(key, user_id)
        enabled = bucket < flag.rollout_percent
        return FlagEvaluation(
            key=key,
            enabled=enabled,
            reason=f"rollout_bucket_{bucket}_of_{flag.rollout_percent}",
        )

    async def evaluate_many(
        self, session: AsyncSession, keys: list[str], *, user_id: uuid.UUID | None = None
    ) -> tuple[dict[str, bool], list[str]]:
        """Every flag a service needs, in one call.

        One round trip rather than one per flag: a page that checks six flags should
        not make six network calls to the same service to render.
        """
        wanted = [key.strip().lower() for key in keys]
        stmt = select(FeatureFlag).where(FeatureFlag.key.in_(wanted))
        found = {flag.key: flag for flag in (await session.execute(stmt)).scalars().all()}

        decisions: dict[str, bool] = {}
        unknown: list[str] = []
        for key in wanted:
            flag = found.get(key)
            if flag is None:
                unknown.append(key)
                # Reported *and* answered. A caller that only reads `flags` should
                # still get a usable value rather than a KeyError.
                decisions[key] = False
                continue
            decisions[key] = (await self.evaluate(session, key, user_id=user_id)).enabled
        return decisions, unknown

    # ---- writing ---------------------------------------------------------

    async def create(
        self, session: AsyncSession, payload: FlagCreate, *, actor_id: uuid.UUID | None
    ) -> FeatureFlag:
        flag = FeatureFlag(
            key=payload.key,
            enabled=payload.enabled,
            description=payload.description,
            rollout_percent=payload.rollout_percent,
            allowlist=[str(entry) for entry in payload.allowlist],
            updated_by=actor_id,
        )
        # A SAVEPOINT opened before `session.add`, because `begin_nested()` autoflushes
        # pending state — adding first emits the INSERT outside the savepoint, so the
        # IntegrityError escapes this `try` and takes the transaction with it.
        savepoint = await session.begin_nested()
        session.add(flag)
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError as exc:
            await savepoint.rollback()
            raise ConflictError(
                "A flag with that key already exists.", details={"key": payload.key}
            ) from exc

        await self._audit(session, flag, action="created", before={}, reason="", actor_id=actor_id)
        await session.commit()
        await self._invalidate()
        logger.info("admin.flag_created", key=flag.key, enabled=flag.enabled)
        return flag

    async def update(
        self,
        session: AsyncSession,
        key: str,
        payload: FlagUpdate,
        *,
        actor_id: uuid.UUID | None,
    ) -> FeatureFlag:
        flag = await self.get(session, key)
        before = _snapshot(flag)

        if payload.enabled is not None:
            flag.enabled = payload.enabled
        if payload.description is not None:
            flag.description = payload.description
        if payload.rollout_percent is not None:
            flag.rollout_percent = payload.rollout_percent
        if payload.allowlist is not None:
            flag.allowlist = [str(entry) for entry in payload.allowlist]
        flag.updated_by = actor_id

        await self._audit(
            session,
            flag,
            action="updated",
            before=before,
            reason=payload.reason,
            actor_id=actor_id,
        )
        await session.commit()
        await self._invalidate()
        logger.info(
            "admin.flag_updated",
            key=flag.key,
            enabled=flag.enabled,
            rollout=flag.rollout_percent,
            actor=str(actor_id) if actor_id else None,
        )
        return flag

    async def delete(
        self, session: AsyncSession, key: str, *, actor_id: uuid.UUID | None, reason: str = ""
    ) -> None:
        """Remove a flag once the code reading it is gone.

        The audit row survives the flag. Deleting the history along with the switch
        would lose the record of a flag that was on during an incident.
        """
        flag = await self.get(session, key)
        before = _snapshot(flag)
        await self._audit(
            session, flag, action="deleted", before=before, reason=reason, actor_id=actor_id
        )
        await session.delete(flag)
        await session.commit()
        await self._invalidate()
        logger.info("admin.flag_deleted", key=key)

    async def _audit(
        self,
        session: AsyncSession,
        flag: FeatureFlag,
        *,
        action: str,
        before: dict,
        reason: str,
        actor_id: uuid.UUID | None,
    ) -> None:
        session.add(
            FlagAudit(
                flag_key=flag.key,
                action=action,
                before=before,
                # Both sides. "Enabled payments" is a far less useful record than
                # "rollout went from 5% to 100%", and only before-and-after separates
                # them.
                after={} if action == "deleted" else _snapshot(flag),
                reason=reason[:500],
                actor_id=actor_id,
            )
        )

    async def audits(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        key: str | None = None,
    ) -> tuple[list[FlagAudit], int]:
        stmt = select(FlagAudit)
        count_stmt = select(func.count(FlagAudit.id))
        if key:
            stmt = stmt.where(FlagAudit.flag_key == key)
            count_stmt = count_stmt.where(FlagAudit.flag_key == key)

        total = int((await session.execute(count_stmt)).scalar_one())
        stmt = stmt.order_by(FlagAudit.created_at.desc()).limit(limit).offset(offset)
        return list((await session.execute(stmt)).scalars().all()), total

    async def prune_audits(self, session: AsyncSession) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=self._settings.flag_audit_retention_days)
        total = int(
            (
                await session.execute(
                    select(func.count(FlagAudit.id)).where(FlagAudit.created_at < cutoff)
                )
            ).scalar_one()
        )
        if total:
            await session.execute(delete(FlagAudit).where(FlagAudit.created_at < cutoff))
            await session.commit()
        return total

    async def _invalidate(self) -> None:
        """Drop the cached flag set after any change.

        Best effort. A cache that cannot be cleared means the flag takes up to
        `FLAG_CACHE_TTL` to take effect, which is a delay — refusing the write because
        Redis is unavailable would be an outage.
        """
        if self._redis is None:
            return
        try:
            await self._redis.delete(_CACHE_KEY)
        except Exception as exc:
            logger.warning("admin.flag_cache_invalidation_failed", error=str(exc))


def bucket_of(key: str, user_id: uuid.UUID | str) -> int:
    """A stable 0-99 bucket for one user and one flag.

    Hashed from the flag key **and** the user id together, not the user alone. With
    the user alone, the same unlucky 5% would be the first cohort for every flag on
    the platform — so a small group of people would see every half-finished feature
    and nobody else would see any, and early rollouts would test one unrepresentative
    slice over and over.
    """
    digest = hashlib.sha256(f"{key}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def _snapshot(flag: FeatureFlag) -> dict:
    return {
        "enabled": flag.enabled,
        "description": flag.description,
        "rollout_percent": flag.rollout_percent,
        "allowlist": list(flag.allowlist or []),
    }

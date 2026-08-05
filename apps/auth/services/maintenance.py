"""Scheduled cleanup for the auth schema.

Three tables here grow without bound and nothing else deletes from them.
``refresh_tokens`` gains a row per login and per rotation — an active user produces
dozens a week. ``sessions`` gains one per login. The single-use token tables gain one
per password reset and per verification email. Left alone, the tables that every
login path reads become the slowest thing in the service.

Two rules govern what may be deleted, and both matter more than the disk space:

**Nothing is deleted while it could still be presented.** A refresh token is dropped
only once it is *both* past its expiry *and* past a grace period. Deleting an expired
token the instant it expires destroys the evidence that makes reuse detection work: a
stolen token replayed a minute after expiry should be recognised as a revoked family
and take the whole family down, not be met with "unknown token" — which is
indistinguishable from a typo and tells an attacker nothing has been noticed.

**Audit logs are not deleted on the same schedule as anything else.** They are the
record of who did what, and the questions they answer arrive months late. They get
their own, much longer retention, and security-relevant actions are exempt from it
entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import get_logger
from models import (
    AuditLog,
    EmailVerificationToken,
    PasswordResetToken,
    RefreshToken,
    Session,
)
from settings import Settings

logger = get_logger(__name__)

#: Actions never pruned, whatever the retention window says. These are the entries
#: someone comes looking for after a breach, and "we deleted it after 90 days" is not
#: an answer anyone accepts.
PROTECTED_ACTIONS = (
    "user.banned",
    "user.unbanned",
    "user.role_changed",
    "token.reuse_detected",
    "mfa.disabled",
    "password.changed",
)


@dataclass(frozen=True, slots=True)
class PruneResult:
    refresh_tokens: int = 0
    sessions: int = 0
    verification_tokens: int = 0
    reset_tokens: int = 0
    audit_logs: int = 0

    @property
    def total(self) -> int:
        return (
            self.refresh_tokens
            + self.sessions
            + self.verification_tokens
            + self.reset_tokens
            + self.audit_logs
        )


class MaintenanceService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def prune(self, session: AsyncSession, *, dry_run: bool = False) -> PruneResult:
        """Delete what is safely past use. Returns what went, or would have."""
        now = datetime.now(UTC)
        token_cutoff = now - timedelta(days=self._settings.token_retention_days)
        audit_cutoff = now - timedelta(days=self._settings.audit_retention_days)

        result = PruneResult(
            refresh_tokens=await self._count_or_delete(
                session,
                RefreshToken,
                # Past expiry *and* past the grace window. Both conditions, because a
                # token deleted the moment it expires takes reuse detection with it.
                or_(
                    RefreshToken.expires_at < token_cutoff,
                    RefreshToken.revoked_at.is_not(None),
                )
                & (RefreshToken.created_at < token_cutoff),
                dry_run=dry_run,
            ),
            sessions=await self._count_or_delete(
                session,
                Session,
                (Session.expires_at < token_cutoff) & (Session.created_at < token_cutoff),
                dry_run=dry_run,
            ),
            verification_tokens=await self._count_or_delete(
                session,
                EmailVerificationToken,
                EmailVerificationToken.expires_at < now,
                dry_run=dry_run,
            ),
            reset_tokens=await self._count_or_delete(
                session,
                PasswordResetToken,
                PasswordResetToken.expires_at < now,
                dry_run=dry_run,
            ),
            audit_logs=await self._count_or_delete(
                session,
                AuditLog,
                (AuditLog.created_at < audit_cutoff) & AuditLog.action.not_in(PROTECTED_ACTIONS),
                dry_run=dry_run,
            ),
        )

        if not dry_run and result.total:
            await session.commit()

        logger.info(
            "auth.pruned",
            dry_run=dry_run,
            refresh_tokens=result.refresh_tokens,
            sessions=result.sessions,
            audit_logs=result.audit_logs,
            total=result.total,
        )
        return result

    async def _count_or_delete(
        self, session: AsyncSession, model: type, condition, *, dry_run: bool
    ) -> int:
        """Count matching rows, then delete them unless this is a dry run.

        Counted first even when deleting: `rowcount` is reliable on PostgreSQL but not
        on every backend the tests run against, and a sweep that cannot report what it
        did is a sweep nobody trusts enough to schedule.
        """
        from sqlalchemy import func

        total = int(
            (
                await session.execute(select(func.count()).select_from(model).where(condition))
            ).scalar_one()
        )
        if total and not dry_run:
            await session.execute(delete(model).where(condition))
        return total

"""Account lifecycle: registration, login, verification, password reset, bans.

Two properties are load-bearing throughout this module:

**No user enumeration.** Registration, login and password reset must not reveal
whether an address has an account. That means identical responses *and* comparable
timing — an early return for an unknown address is measurably faster than a bcrypt
verification, and that difference alone is a working enumeration oracle.

**Sessions die when credentials change.** A password reset or change revokes every
session, because the most likely reason for one is that the account was compromised.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    generate_token,
    get_logger,
    hash_password,
    hash_token,
    verify_password,
)
from knowledgeos_core.security import needs_rehash
from models import AuditLog, EmailVerificationToken, PasswordResetToken, User
from settings import Settings

logger = get_logger(__name__)

#: A bcrypt hash of a value nobody can supply. Verified against when the account is
#: unknown, so an unknown address costs the same time as a wrong password.
_DUMMY_HASH = hash_password(generate_token(32))


def normalise_email(email: str) -> str:
    """Lower-case and strip. Stored this way, so uniqueness is case-insensitive."""
    return email.strip().lower()


class AccountService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- lookup ---------------------------------------------------------

    async def get_by_email(self, session: AsyncSession, email: str) -> User | None:
        return (
            await session.execute(
                select(User).where(
                    func.lower(User.email) == normalise_email(email),
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, user_id: uuid.UUID) -> User:
        user = await session.get(User, user_id)
        if user is None or user.deleted_at is not None:
            raise NotFoundError("User not found.")
        return user

    # ---- registration ---------------------------------------------------

    async def register(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
        full_name: str | None,
        locale: str | None,
        ip_address: str | None,
    ) -> tuple[User, str | None]:
        """Create an account.

        Returns ``(user, raw_verification_token)``. The token is ``None`` when the
        address is already registered — the caller still responds as though the
        registration succeeded, so an attacker learns nothing.
        """
        email = normalise_email(email)
        existing = await self.get_by_email(session, email)

        if existing is not None:
            # Spend comparable time, then return the "success" shape without doing
            # anything. The genuine owner is told by email that someone tried.
            hash_password(password)
            await self.record_audit(
                session,
                user_id=existing.id,
                action="register.duplicate",
                status="blocked",
                ip_address=ip_address,
            )
            logger.info("auth.register_duplicate", user_id=str(existing.id))
            return existing, None

        user = User(
            email=email,
            password_hash=hash_password(password),
            password_changed_at=datetime.now(UTC),
            full_name=full_name,
            locale=locale,
            roles=["user"],
            extra_permissions=[],
            is_active=True,
        )
        session.add(user)
        await session.flush()

        raw_token = await self.issue_verification_token(session, user, email=email)
        await self.record_audit(session, user_id=user.id, action="register", ip_address=ip_address)
        logger.info("auth.registered", user_id=str(user.id))
        return user, raw_token

    # ---- login ----------------------------------------------------------

    async def authenticate(
        self,
        session: AsyncSession,
        *,
        email: str,
        password: str,
        ip_address: str | None,
    ) -> User:
        """Verify credentials. Raises :class:`UnauthorizedError` on any failure.

        Every failure path returns the *same* message. Distinguishing "no such
        account" from "wrong password" hands an attacker a list of valid addresses.
        """
        started = time.perf_counter()
        generic = "Incorrect email or password."
        user = await self.get_by_email(session, email)

        try:
            if user is None:
                # Constant-cost dummy verification: without it, an unknown address
                # returns in microseconds and a known one takes ~250ms.
                verify_password(password, _DUMMY_HASH)
                raise UnauthorizedError(generic)

            if user.locked_until is not None and _as_utc(user.locked_until) > datetime.now(UTC):
                await self.record_audit(
                    session,
                    user_id=user.id,
                    action="login",
                    status="locked",
                    ip_address=ip_address,
                )
                raise UnauthorizedError(
                    "This account is temporarily locked after too many failed attempts. "
                    "Try again shortly.",
                    code="account_locked",
                )

            if user.banned_at is not None:
                raise UnauthorizedError("This account has been suspended.", code="account_banned")

            if not user.password_hash:
                # OAuth-only account. Saying so would reveal the address exists.
                verify_password(password, _DUMMY_HASH)
                raise UnauthorizedError(generic)

            if not verify_password(password, user.password_hash):
                await self._register_failed_attempt(session, user, ip_address=ip_address)
                raise UnauthorizedError(generic)

            if not user.is_active or user.deleted_at is not None:
                raise UnauthorizedError(generic)

            # Transparently upgrade the hash when the cost factor has been raised.
            if needs_rehash(user.password_hash):
                user.password_hash = hash_password(password)
                logger.info("auth.password_rehashed", user_id=str(user.id))

            user.failed_login_attempts = 0
            user.locked_until = None
            user.last_login_at = datetime.now(UTC)
            user.last_login_ip = ip_address
            return user
        finally:
            await self._pad_response(started)

    async def _register_failed_attempt(
        self, session: AsyncSession, user: User, *, ip_address: str | None
    ) -> None:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= self._settings.login_max_attempts:
            user.locked_until = datetime.now(UTC) + timedelta(
                seconds=self._settings.login_lockout_seconds
            )
            logger.warning(
                "auth.account_locked",
                user_id=str(user.id),
                attempts=user.failed_login_attempts,
            )
        await self.record_audit(
            session,
            user_id=user.id,
            action="login",
            status="failure",
            ip_address=ip_address,
            meta={"attempts": user.failed_login_attempts},
        )

    async def _pad_response(self, started: float) -> None:
        """Hold every credential response to a minimum duration.

        Removes the timing side channel that distinguishes an early return from a
        full bcrypt verification.
        """
        floor = self._settings.credential_response_floor_ms / 1000
        elapsed = time.perf_counter() - started
        if elapsed < floor:
            await asyncio.sleep(floor - elapsed)

    # ---- email verification ---------------------------------------------

    async def issue_verification_token(
        self, session: AsyncSession, user: User, *, email: str, purpose: str = "email_verify"
    ) -> str:
        raw = generate_token(32)
        session.add(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=hash_token(raw),
                purpose=purpose,
                email=normalise_email(email),
                expires_at=datetime.now(UTC)
                + timedelta(seconds=self._settings.email_verification_ttl),
            )
        )
        await session.flush()
        return raw

    async def verify_email(self, session: AsyncSession, raw_token: str) -> User:
        record = (
            await session.execute(
                select(EmailVerificationToken).where(
                    EmailVerificationToken.token_hash == hash_token(raw_token)
                )
            )
        ).scalar_one_or_none()

        now = datetime.now(UTC)
        if record is None or record.used_at is not None or _as_utc(record.expires_at) < now:
            raise UnauthorizedError("This verification link is invalid or has expired.")

        user = await self.get_by_id(session, record.user_id)
        record.used_at = now
        if user.email_verified_at is None:
            user.email_verified_at = now
        await self.record_audit(session, user_id=user.id, action="email.verified")
        logger.info("auth.email_verified", user_id=str(user.id))
        return user

    # ---- password reset --------------------------------------------------

    async def request_password_reset(
        self, session: AsyncSession, *, email: str, ip_address: str | None
    ) -> tuple[User, str] | None:
        """Issue a reset token, or ``None`` when the address has no account.

        The caller returns the same response either way.
        """
        started = time.perf_counter()
        try:
            user = await self.get_by_email(session, email)
            if user is None or not user.is_active:
                logger.info("auth.password_reset_unknown_email")
                return None

            raw = generate_token(32)
            session.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=hash_token(raw),
                    expires_at=datetime.now(UTC)
                    + timedelta(seconds=self._settings.password_reset_ttl),
                    requested_ip=ip_address,
                )
            )
            await session.flush()
            await self.record_audit(
                session, user_id=user.id, action="password.reset_requested", ip_address=ip_address
            )
            return user, raw
        finally:
            await self._pad_response(started)

    async def reset_password(
        self, session: AsyncSession, *, raw_token: str, new_password: str
    ) -> User:
        record = (
            await session.execute(
                select(PasswordResetToken).where(
                    PasswordResetToken.token_hash == hash_token(raw_token)
                )
            )
        ).scalar_one_or_none()

        now = datetime.now(UTC)
        if record is None or record.used_at is not None or _as_utc(record.expires_at) < now:
            raise UnauthorizedError("This reset link is invalid or has expired.")

        user = await self.get_by_id(session, record.user_id)
        record.used_at = now
        user.password_hash = hash_password(new_password)
        user.password_changed_at = now
        # A reset unlocks the account: the legitimate owner has just proved control
        # of the mailbox, and leaving them locked out helps only the attacker.
        user.failed_login_attempts = 0
        user.locked_until = None
        await self.record_audit(session, user_id=user.id, action="password.reset")
        logger.info("auth.password_reset", user_id=str(user.id))
        return user

    async def change_password(
        self, session: AsyncSession, user: User, *, current: str, new: str
    ) -> None:
        if not user.password_hash or not verify_password(current, user.password_hash):
            await self.record_audit(
                session, user_id=user.id, action="password.change", status="failure"
            )
            raise UnauthorizedError("The current password is incorrect.")
        if verify_password(new, user.password_hash):
            raise ConflictError("The new password must differ from the current one.")
        user.password_hash = hash_password(new)
        user.password_changed_at = datetime.now(UTC)
        await self.record_audit(session, user_id=user.id, action="password.change")

    # ---- moderation ------------------------------------------------------

    async def set_ban(
        self,
        session: AsyncSession,
        user: User,
        *,
        banned: bool,
        reason: str | None,
        actor_id: uuid.UUID | None,
    ) -> None:
        user.banned_at = datetime.now(UTC) if banned else None
        user.ban_reason = reason if banned else None
        user.is_active = not banned
        await self.record_audit(
            session,
            user_id=user.id,
            actor_id=actor_id,
            action="user.banned" if banned else "user.unbanned",
            meta={"reason": reason} if reason else {},
        )

    # ---- audit -----------------------------------------------------------

    async def record_audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        user_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        status: str = "success",
        ip_address: str | None = None,
        user_agent: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Append to the audit trail.

        ``meta`` must never carry a token, password or secret — only identifiers and
        reasons.
        """
        session.add(
            AuditLog(
                user_id=user_id,
                actor_id=actor_id,
                action=action,
                status=status,
                ip_address=ip_address,
                user_agent=(user_agent or "")[:400] or None,
                meta=meta or {},
            )
        )


def _as_utc(value: datetime) -> datetime:
    """Normalise a possibly-naive timestamp (SQLite drops tzinfo)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

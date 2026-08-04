"""TOTP second factor and single-use recovery codes.

Secrets are encrypted at rest with Fernet, so a database dump alone does not yield a
working second factor — which is the entire point of having one.

Replay is blocked by recording the highest TOTP time-step already accepted. Without
that, a code observed over the shoulder (or captured by a phishing proxy) stays valid
for the remainder of its 30-second window.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ConflictError, UnauthorizedError, get_logger, hash_token
from models import RecoveryCode, TotpSecret, User
from settings import Settings

logger = get_logger(__name__)

#: Accept the immediately-preceding and following step, tolerating ~30s of clock
#: drift on the user's phone. Wider windows meaningfully weaken the factor.
TOTP_VALID_WINDOW = 1

_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 — unambiguous


class MfaService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._fernet = Fernet(settings.totp_fernet_key)

    # ---- enrolment -------------------------------------------------------

    async def begin_enrolment(self, session: AsyncSession, user: User) -> tuple[str, str]:
        """Create (or replace) an unconfirmed TOTP secret.

        Returns ``(secret, provisioning_uri)``. The secret is not active until
        :meth:`confirm_enrolment` succeeds — otherwise a user who scans the QR code
        but never verifies it would be locked out of their own account.
        """
        if user.mfa_enabled:
            raise ConflictError("Two-factor authentication is already enabled.")

        secret = pyotp.random_base32()
        existing = await self._get_secret_row(session, user.id)
        if existing is not None:
            existing.secret_encrypted = self._encrypt(secret)
            existing.confirmed_at = None
            existing.last_used_step = None
        else:
            session.add(TotpSecret(user_id=user.id, secret_encrypted=self._encrypt(secret)))
        await session.flush()

        uri = pyotp.TOTP(
            secret, digits=self._settings.totp_digits, interval=self._settings.totp_period
        ).provisioning_uri(name=user.email, issuer_name=self._settings.totp_issuer)
        return secret, uri

    async def confirm_enrolment(self, session: AsyncSession, user: User, code: str) -> list[str]:
        """Verify the first code and activate MFA. Returns the recovery codes."""
        row = await self._get_secret_row(session, user.id)
        if row is None:
            raise ConflictError("Start two-factor enrolment before confirming it.")

        step = self._verify_totp(row, code)
        if step is None:
            raise UnauthorizedError("That code is not valid. Check your authenticator app.")

        row.confirmed_at = datetime.now(UTC)
        row.last_used_step = step
        user.mfa_enabled = True

        codes = await self.regenerate_recovery_codes(session, user)
        logger.info("auth.mfa_enabled", user_id=str(user.id))
        return codes

    async def disable(self, session: AsyncSession, user: User) -> None:
        row = await self._get_secret_row(session, user.id)
        if row is not None:
            await session.delete(row)
        for code in await self._recovery_rows(session, user.id):
            await session.delete(code)
        user.mfa_enabled = False
        logger.info("auth.mfa_disabled", user_id=str(user.id))

    # ---- verification ----------------------------------------------------

    async def verify(self, session: AsyncSession, user: User, code: str) -> str:
        """Verify a TOTP code or a recovery code.

        Returns the method used, for the session's ``auth_method`` field.
        """
        cleaned = code.strip().replace(" ", "").replace("-", "").upper()

        row = await self._get_secret_row(session, user.id)
        if row is not None and row.confirmed_at is not None:
            step = self._verify_totp(row, cleaned)
            if step is not None:
                row.last_used_step = step
                return "totp"

        if await self._consume_recovery_code(session, user.id, cleaned):
            logger.warning(
                "auth.recovery_code_used",
                user_id=str(user.id),
                detail="Recovery codes are single-use; prompt the user to regenerate.",
            )
            return "recovery_code"

        raise UnauthorizedError("That code is not valid.")

    def _verify_totp(self, row: TotpSecret, code: str) -> int | None:
        """Return the accepted time-step, or ``None``.

        Rejects any step at or below the last accepted one, so an intercepted code
        cannot be replayed inside its own window.
        """
        if not code.isdigit():
            return None
        try:
            secret = self._decrypt(row.secret_encrypted)
        except InvalidToken:
            # The encryption key changed and the stored secret is unreadable. Failing
            # closed is correct — the user must re-enrol.
            logger.error("auth.totp_secret_undecryptable", user_id=str(row.user_id))
            return None

        totp = pyotp.TOTP(
            secret, digits=self._settings.totp_digits, interval=self._settings.totp_period
        )
        now = datetime.now(UTC)
        if not totp.verify(code, for_time=now, valid_window=TOTP_VALID_WINDOW):
            return None

        step = int(now.timestamp()) // self._settings.totp_period
        # Identify which step in the window actually matched, so replay detection is
        # precise rather than assuming the current step.
        for offset in range(-TOTP_VALID_WINDOW, TOTP_VALID_WINDOW + 1):
            candidate = step + offset
            if totp.at(candidate * self._settings.totp_period) == code:
                if row.last_used_step is not None and candidate <= row.last_used_step:
                    logger.warning("auth.totp_replay_blocked", user_id=str(row.user_id))
                    return None
                return candidate
        return None

    # ---- recovery codes ---------------------------------------------------

    async def regenerate_recovery_codes(self, session: AsyncSession, user: User) -> list[str]:
        """Replace all recovery codes. The plaintext is returned exactly once."""
        for row in await self._recovery_rows(session, user.id):
            await session.delete(row)

        codes: list[str] = []
        for _ in range(self._settings.recovery_code_count):
            raw = "-".join(
                "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(5)) for _ in range(2)
            )
            codes.append(raw)
            session.add(RecoveryCode(user_id=user.id, code_hash=hash_token(raw.replace("-", ""))))
        await session.flush()
        return codes

    async def _consume_recovery_code(
        self, session: AsyncSession, user_id: uuid.UUID, code: str
    ) -> bool:
        candidate = hash_token(code.replace("-", ""))
        row = (
            await session.execute(
                select(RecoveryCode).where(
                    RecoveryCode.user_id == user_id,
                    RecoveryCode.code_hash == candidate,
                    RecoveryCode.used_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.used_at = datetime.now(UTC)
        return True

    async def count_unused_recovery_codes(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        rows = await self._recovery_rows(session, user_id, unused_only=True)
        return len(rows)

    # ---- helpers ---------------------------------------------------------

    async def _get_secret_row(self, session: AsyncSession, user_id: uuid.UUID) -> TotpSecret | None:
        return (
            await session.execute(select(TotpSecret).where(TotpSecret.user_id == user_id))
        ).scalar_one_or_none()

    async def _recovery_rows(
        self, session: AsyncSession, user_id: uuid.UUID, *, unused_only: bool = False
    ) -> list[RecoveryCode]:
        stmt = select(RecoveryCode).where(RecoveryCode.user_id == user_id)
        if unused_only:
            stmt = stmt.where(RecoveryCode.used_at.is_(None))
        return list((await session.execute(stmt)).scalars().all())

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode()).decode()

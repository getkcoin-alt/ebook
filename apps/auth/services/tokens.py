"""Access and refresh token issuance, rotation and revocation.

The security-critical mechanism here is **refresh rotation with reuse detection**
(ADR 0003). Every refresh consumes one token and issues its successor. Presenting a
token that was already consumed means two parties hold the chain — the legitimate
client and a thief cannot both have the current one — so the entire family is
revoked and the user must sign in again.

Without reuse detection, a stolen refresh token is a silent 30-day credential.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jose import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    UnauthorizedError,
    generate_token,
    get_logger,
    hash_token,
)
from knowledgeos_core.schemas import ROLE_PERMISSIONS, UserRole
from models import RefreshToken, Session, User
from services.keys import KeyRing
from settings import Settings

logger = get_logger(__name__)


def resolve_permissions(user: User) -> list[str]:
    """Flatten a user's roles into the permission set embedded in their token.

    Resolved at mint time and carried in the token, which is what lets other
    services authorise without a lookup. The cost is staleness bounded by the
    access-token lifetime; the denylist covers the case where that is too slow.
    """
    granted: set[str] = set()
    for role_name in user.roles or []:
        try:
            role = UserRole(role_name)
        except ValueError:
            logger.warning("auth.unknown_role", role=role_name, user_id=str(user.id))
            continue
        granted.update(str(p) for p in ROLE_PERMISSIONS.get(role, []))
    granted.update(user.extra_permissions or [])
    return sorted(granted)


@dataclass(slots=True)
class IssuedTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    csrf_token: str
    user_id: uuid.UUID
    session_id: uuid.UUID
    refresh_expires_at: datetime


class TokenService:
    def __init__(self, settings: Settings, keyring: KeyRing) -> None:
        self._settings = settings
        self._keyring = keyring

    # ---- access tokens --------------------------------------------------

    def mint_access_token(self, user: User, *, session_id: uuid.UUID) -> tuple[str, int]:
        now = datetime.now(UTC)
        ttl = self._settings.access_token_ttl
        key = self._keyring.active
        claims = {
            "sub": str(user.id),
            "email": user.email,
            "roles": list(user.roles or []),
            "permissions": resolve_permissions(user),
            "sid": str(session_id),
            "jti": str(uuid.uuid4()),
            "typ": "access",
            "iss": self._settings.jwt_issuer,
            "aud": self._settings.jwt_audience,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        }
        token = jwt.encode(
            claims,
            key.private_pem,
            algorithm=key.algorithm,
            headers={"kid": key.kid, "typ": "JWT"},
        )
        return token, ttl

    def mint_mfa_challenge(self, user_id: uuid.UUID) -> tuple[str, int]:
        """Short-lived token proving the password step succeeded.

        It carries ``typ: mfa_challenge`` and **no** roles or permissions, so it
        cannot be presented as an access token even though it is signed by the same
        key. `TokenVerifier` in core rejects any token whose ``typ`` is not
        ``access``.
        """
        now = datetime.now(UTC)
        ttl = self._settings.mfa_challenge_ttl
        key = self._keyring.active
        token = jwt.encode(
            {
                "sub": str(user_id),
                "typ": "mfa_challenge",
                "jti": str(uuid.uuid4()),
                "iss": self._settings.jwt_issuer,
                "aud": self._settings.jwt_audience,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=ttl)).timestamp()),
            },
            key.private_pem,
            algorithm=key.algorithm,
            headers={"kid": key.kid},
        )
        return token, ttl

    def read_mfa_challenge(self, token: str) -> uuid.UUID:
        """Validate an MFA challenge token and return the pending user id."""
        key = self._keyring.active
        try:
            claims = jwt.decode(
                token,
                key.public_pem,
                algorithms=[key.algorithm],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
            )
        except Exception as exc:
            raise UnauthorizedError("The verification challenge is invalid or expired.") from exc
        if claims.get("typ") != "mfa_challenge":
            raise UnauthorizedError("The verification challenge is invalid or expired.")
        return uuid.UUID(claims["sub"])

    # ---- sessions and refresh tokens ------------------------------------

    async def start_session(
        self,
        session: AsyncSession,
        user: User,
        *,
        ip_address: str | None,
        user_agent: str | None,
        device_label: str | None,
        auth_method: str,
    ) -> IssuedTokens:
        """Create a session and its first refresh token."""
        now = datetime.now(UTC)
        db_session = Session(
            user_id=user.id,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:400] or None,
            device_label=device_label,
            auth_method=auth_method,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=self._settings.refresh_token_ttl),
        )
        session.add(db_session)
        await session.flush()

        # A new login starts a new family: revoking one stolen chain must not sign
        # the user out of their other devices.
        return await self._issue_pair(
            session, user=user, db_session=db_session, family_id=uuid.uuid4(), parent=None
        )

    async def _issue_pair(
        self,
        session: AsyncSession,
        *,
        user: User,
        db_session: Session,
        family_id: uuid.UUID,
        parent: RefreshToken | None,
    ) -> IssuedTokens:
        raw_refresh = generate_token(48)
        raw_csrf = generate_token(24)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.refresh_token_ttl)

        record = RefreshToken(
            user_id=user.id,
            session_id=db_session.id,
            family_id=family_id,
            parent_id=parent.id if parent else None,
            token_hash=hash_token(raw_refresh),
            csrf_hash=hash_token(raw_csrf),
            expires_at=expires_at,
        )
        session.add(record)
        await session.flush()

        access_token, ttl = self.mint_access_token(user, session_id=db_session.id)
        return IssuedTokens(
            access_token=access_token,
            expires_in=ttl,
            refresh_token=raw_refresh,
            csrf_token=raw_csrf,
            user_id=user.id,
            session_id=db_session.id,
            refresh_expires_at=expires_at,
        )

    async def rotate(
        self, session: AsyncSession, *, raw_refresh: str, csrf_token: str | None
    ) -> IssuedTokens:
        """Consume a refresh token and issue its successor.

        Raises :class:`UnauthorizedError` for anything invalid, and revokes the whole
        family when a consumed token is replayed.
        """
        token_hash = hash_token(raw_refresh)
        record = (
            await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        ).scalar_one_or_none()

        if record is None:
            # Either forged or already rotated long ago and pruned.
            logger.info("auth.refresh_unknown_token")
            raise UnauthorizedError("The refresh token is invalid.")

        now = datetime.now(UTC)

        # --- reuse detection ------------------------------------------------
        if record.used_at is not None:
            logger.warning(
                "auth.refresh_reuse_detected",
                user_id=str(record.user_id),
                family_id=str(record.family_id),
                detail="A consumed refresh token was presented again; revoking the family.",
            )
            await self.revoke_family(session, record.family_id, reason="reuse_detected")
            raise UnauthorizedError(
                "This session has been ended for your security. Please sign in again.",
                code="refresh_token_reused",
            )

        if record.revoked_at is not None:
            raise UnauthorizedError("The refresh token has been revoked.")
        if _as_utc(record.expires_at) < now:
            raise UnauthorizedError("The refresh token has expired.")

        # --- CSRF double-submit --------------------------------------------
        # The cookie is sent automatically by the browser; the header is not. A
        # cross-site page can therefore trigger the request but cannot read the
        # cookie to populate the header, so it cannot forge a refresh.
        if csrf_token is not None and hash_token(csrf_token) != record.csrf_hash:
            logger.warning("auth.refresh_csrf_mismatch", user_id=str(record.user_id))
            raise UnauthorizedError("The CSRF token does not match.", code="csrf_mismatch")

        db_session = await session.get(Session, record.session_id)
        if db_session is None or db_session.revoked_at is not None:
            raise UnauthorizedError("The session has ended.")
        if _as_utc(db_session.expires_at) < now:
            raise UnauthorizedError("The session has expired.")

        user = await session.get(User, record.user_id)
        if user is None or not user.is_active or user.deleted_at is not None:
            raise UnauthorizedError("This account is no longer active.")
        if user.banned_at is not None:
            raise UnauthorizedError("This account has been suspended.", code="account_banned")

        record.used_at = now
        db_session.last_seen_at = now

        return await self._issue_pair(
            session,
            user=user,
            db_session=db_session,
            family_id=record.family_id,
            parent=record,
        )

    # ---- revocation -----------------------------------------------------

    async def revoke_family(
        self, session: AsyncSession, family_id: uuid.UUID, *, reason: str
    ) -> None:
        """Revoke every refresh token descended from one login."""
        now = datetime.now(UTC)
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )

    async def revoke_session(
        self, session: AsyncSession, session_id: uuid.UUID, *, reason: str
    ) -> None:
        now = datetime.now(UTC)
        await session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.session_id == session_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )

    async def revoke_all_sessions(
        self, session: AsyncSession, user_id: uuid.UUID, *, reason: str
    ) -> None:
        """Sign a user out everywhere — used on password change, ban and logout-all."""
        now = datetime.now(UTC)
        await session.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
        )
        logger.info("auth.all_sessions_revoked", user_id=str(user_id), reason=reason)


def _as_utc(value: datetime) -> datetime:
    """Normalise a possibly-naive timestamp for comparison.

    SQLite drops timezone information, so a value read back in tests is naive while
    the same value from PostgreSQL is aware. Comparing the two raises TypeError.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

"""SQLAlchemy models owned by the ``auth`` schema.

No other service reads these tables — identity is exposed over ``/internal/users*``
and through domain events, never through a cross-schema join (ADR 0002).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from knowledgeos_core import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin, UUIDType

SCHEMA = "auth"

#: JSONB on PostgreSQL (indexable, binary) and plain JSON elsewhere, so the test
#: suite can run the same models on SQLite without a second set of definitions.
JSONType = JSON().with_variant(JSONB(), "postgresql")

#: SHA-256 hex digests are exactly 64 characters.
HASH_LEN = 64


def _uuid_column(*args: Any, **kwargs: Any) -> Mapped[uuid.UUID]:
    """A UUID column. Positional args pass through, so ``ForeignKey`` works."""
    return mapped_column(UUIDType, *args, **kwargs)


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """A human identity. ``password_hash`` is null for OAuth-only accounts."""

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_is_active_deleted_at", "is_active", "deleted_at"),
        {"schema": SCHEMA},
    )

    #: Stored lower-cased and stripped; uniqueness is therefore case-insensitive
    #: without needing citext or a functional index.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    full_name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(1000))
    locale: Mapped[str | None] = mapped_column(String(16))

    #: Role names from :class:`knowledgeos_core.schemas.UserRole`.
    roles: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=lambda: ["user"])
    #: Grants beyond what the roles imply. Usually empty.
    extra_permissions: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ban_reason: Mapped[str | None] = mapped_column(String(500))

    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(45))

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None


class Session(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One signed-in device. Its id becomes the ``sid`` claim in access tokens."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id_revoked_at", "user_id", "revoked_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    device_label: Mapped[str | None] = mapped_column(String(120))
    #: ``password``, ``password+totp``, ``password+recovery_code`` or ``oauth:<provider>``.
    auth_method: Mapped[str] = mapped_column(String(40), nullable=False, default="password")

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(60))


class RefreshToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One link in a rotation chain.

    ``family_id`` is the lineage: every token descended from a single login shares
    it. Presenting a token that has already been consumed means two parties hold the
    chain, so the whole family is revoked (ADR 0003).
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_family_id_revoked_at", "family_id", "revoked_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = _uuid_column(nullable=False, index=True)
    parent_id: Mapped[uuid.UUID | None] = _uuid_column(
        ForeignKey(f"{SCHEMA}.refresh_tokens.id", ondelete="SET NULL"), index=True
    )

    #: SHA-256 of the opaque token. A database dump yields no usable credential.
    token_hash: Mapped[str] = mapped_column(
        String(HASH_LEN), nullable=False, unique=True, index=True
    )
    #: SHA-256 of the double-submit CSRF value handed out with this token.
    csrf_hash: Mapped[str] = mapped_column(String(HASH_LEN), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(60))


class OAuthAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A third-party identity linked to a local user."""

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="uq_oauth_accounts_provider_sub"),
        Index("ix_oauth_accounts_user_id_provider", "user_id", "provider"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(191), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    #: Display fields only. Provider access tokens are deliberately never stored:
    #: this service has no reason to act on the user's behalf after sign-in.
    profile: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailVerificationToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single-use, expiring proof of control over an email address."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("ix_email_verification_tokens_user_id_purpose", "user_id", "purpose"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(HASH_LEN), nullable=False, unique=True, index=True
    )
    #: ``email_verify`` or ``oauth_link``.
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="email_verify")
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single-use, expiring, hashed password-reset credential."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = ({"schema": SCHEMA},)

    user_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(HASH_LEN), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(45))


class TotpSecret(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A user's TOTP shared secret, encrypted at rest."""

    __tablename__ = "totp_secrets"
    __table_args__ = ({"schema": SCHEMA},)

    user_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    #: Fernet ciphertext. A database dump alone does not yield a working second
    #: factor, which is the whole point of having one.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Highest TOTP time-step already accepted; blocks replay of an observed code
    #: inside its 30-second window.
    last_used_step: Mapped[int | None] = mapped_column(BigInteger)


class RecoveryCode(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single-use backup code for when the authenticator device is lost."""

    __tablename__ = "recovery_codes"
    __table_args__ = (
        Index("ix_recovery_codes_user_id_used_at", "user_id", "used_at"),
        {"schema": SCHEMA},
    )

    user_id: Mapped[uuid.UUID] = _uuid_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(HASH_LEN), nullable=False, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Append-only record of privileged and security-relevant actions."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
        Index("ix_audit_logs_user_id_created_at", "user_id", "created_at"),
        {"schema": SCHEMA},
    )

    #: Subject of the action. Nullable: a failed login for an unknown address has no
    #: user, and inventing one would leak which addresses exist.
    user_id: Mapped[uuid.UUID | None] = _uuid_column(index=True)
    #: Who performed it, when that differs from the subject (an admin, say).
    actor_id: Mapped[uuid.UUID | None] = _uuid_column(index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    #: Never contains a token, password or secret — only identifiers and reasons.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class SigningKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Public half of a token-signing key, for JWKS publication and rotation audit.

    The private key is **never** stored here. It lives only in the environment /
    secret manager, so a database compromise cannot yield the ability to mint tokens.
    """

    __tablename__ = "signing_keys"
    __table_args__ = ({"schema": SCHEMA},)

    kid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    algorithm: Mapped[str] = mapped_column(String(10), nullable=False, default="RS256")
    public_pem: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``active`` (signs), ``retiring`` (verify only), ``retired`` (not published).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

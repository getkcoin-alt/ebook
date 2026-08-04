"""Request and response models for the authentication service.

These are the public contract: the OpenAPI generated from them is what the
TypeScript SDK is built against.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import EmailStr, Field, field_validator

from knowledgeos_core import BaseSchema
from settings import settings

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------

#: Rejected outright regardless of length. Not a substitute for a breach-corpus
#: check (that belongs behind a k-anonymity API call), but it removes the passwords
#: that appear at the top of every credential-stuffing list.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "12345678",
        "123456789",
        "1234567890",
        "qwertyuiop",
        "letmein123",
        "welcome123",
        "admin12345",
        "iloveyou123",
        "knowledgeos",
        "changeme123",
    }
)

_HAS_LOWER = re.compile(r"[a-z]")
_HAS_UPPER = re.compile(r"[A-Z]")
_HAS_DIGIT = re.compile(r"\d")


def validate_password(value: str) -> str:
    """Enforce the password policy.

    Length carries most of the strength — the character-class rules exist mainly to
    stop a 12-character all-lowercase dictionary word from qualifying. NIST advises
    against forcing exotic composition rules, so there is no symbol requirement.
    """
    if len(value) < settings.password_min_length:
        raise ValueError(f"Password must be at least {settings.password_min_length} characters.")
    if len(value) > settings.password_max_length:
        # An unbounded password is a cheap denial-of-service against bcrypt.
        raise ValueError(f"Password must be at most {settings.password_max_length} characters.")
    if value.lower() in _COMMON_PASSWORDS:
        raise ValueError("This password is too common. Choose something less predictable.")
    classes = sum(bool(pattern.search(value)) for pattern in (_HAS_LOWER, _HAS_UPPER, _HAS_DIGIT))
    if classes < 2:
        raise ValueError("Password must combine at least two of: lowercase, uppercase, digits.")
    return value


Password = Annotated[str, Field(min_length=1, max_length=256)]


# ---------------------------------------------------------------------------
# Registration and login
# ---------------------------------------------------------------------------


class RegisterRequest(BaseSchema):
    email: EmailStr
    password: Password
    full_name: str | None = Field(default=None, max_length=200)
    locale: str | None = Field(default=None, max_length=16)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        return validate_password(value)


class LoginRequest(BaseSchema):
    email: EmailStr
    password: Password
    #: Optional human label for the session ("Kavya's MacBook").
    device_label: str | None = Field(default=None, max_length=120)


class MfaChallengeRequest(BaseSchema):
    """Second step of a login that requires TOTP."""

    challenge_token: str = Field(min_length=16, max_length=512)
    code: str = Field(min_length=6, max_length=32, description="TOTP code or recovery code.")


class RefreshRequest(BaseSchema):
    """Body is optional — the refresh token normally arrives as an httpOnly cookie.

    The explicit field exists for non-browser clients (mobile, CLI) that cannot use
    cookies. Browsers should always use the cookie so the token is unreachable from
    JavaScript.
    """

    refresh_token: str | None = None


class TokenResponse(BaseSchema):
    """Issued on successful authentication.

    The refresh token is **not** in this body for browser clients — it is set as an
    httpOnly cookie. It appears here only for clients that requested a non-cookie
    flow, so an XSS on the web app can never read it.
    """

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - a scheme name, not a credential
    expires_in: int = Field(description="Access token lifetime in seconds.")
    refresh_token: str | None = None
    csrf_token: str | None = Field(
        default=None,
        description="Double-submit value; send back as X-CSRF-Token when refreshing.",
    )
    user: UserOut


class MfaRequiredResponse(BaseSchema):
    """Returned when credentials are valid but a second factor is outstanding."""

    mfa_required: Literal[True] = True
    challenge_token: str
    expires_in: int
    methods: list[str] = Field(default_factory=lambda: ["totp", "recovery_code"])


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserOut(BaseSchema):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    avatar_url: str | None = None
    locale: str | None = None
    roles: list[str]
    permissions: list[str] = Field(default_factory=list)
    email_verified: bool
    mfa_enabled: bool
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserUpdateRequest(BaseSchema):
    full_name: str | None = Field(default=None, max_length=200)
    avatar_url: str | None = Field(default=None, max_length=1000)
    locale: str | None = Field(default=None, max_length=16)


class ChangePasswordRequest(BaseSchema):
    current_password: Password
    new_password: Password

    @field_validator("new_password")
    @classmethod
    def _password(cls, value: str) -> str:
        return validate_password(value)


class ForgotPasswordRequest(BaseSchema):
    email: EmailStr


class ResetPasswordRequest(BaseSchema):
    token: str = Field(min_length=16, max_length=512)
    new_password: Password

    @field_validator("new_password")
    @classmethod
    def _password(cls, value: str) -> str:
        return validate_password(value)


class VerifyEmailRequest(BaseSchema):
    token: str = Field(min_length=16, max_length=512)


class ResendVerificationRequest(BaseSchema):
    email: EmailStr


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class SessionOut(BaseSchema):
    id: uuid.UUID
    ip_address: str | None = None
    user_agent: str | None = None
    device_label: str | None = None
    auth_method: str
    created_at: datetime
    last_seen_at: datetime | None = None
    expires_at: datetime
    #: True for the session making this request, so the UI can label it.
    current: bool = False


# ---------------------------------------------------------------------------
# Two-factor authentication
# ---------------------------------------------------------------------------


class MfaEnrollResponse(BaseSchema):
    secret: str = Field(description="Base32 TOTP secret, for manual entry.")
    provisioning_uri: str = Field(description="otpauth:// URI to render as a QR code.")


class MfaVerifyRequest(BaseSchema):
    code: str = Field(min_length=6, max_length=10)


class MfaEnabledResponse(BaseSchema):
    enabled: bool
    #: Shown exactly once, at enrolment. Only hashes are stored.
    recovery_codes: list[str] = Field(default_factory=list)


class MfaDisableRequest(BaseSchema):
    """Disabling a second factor is itself a privileged action, so it is reauthenticated."""

    password: Password


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class OAuthStartResponse(BaseSchema):
    authorization_url: str
    state: str


class OAuthAccountOut(BaseSchema):
    provider: str
    email: str | None = None
    linked_at: datetime | None = None
    last_login_at: datetime | None = None


# ---------------------------------------------------------------------------
# Internal (service-to-service)
# ---------------------------------------------------------------------------


class InternalUserOut(BaseSchema):
    """Trimmed projection for sibling services.

    Deliberately excludes password hashes, MFA state and audit fields — another
    service has no legitimate use for them, and not sending them means they cannot
    leak through a downstream bug.
    """

    id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    avatar_url: str | None = None
    roles: list[str]
    is_active: bool
    email_verified: bool


class InternalUserBatchRequest(BaseSchema):
    user_ids: list[uuid.UUID] = Field(max_length=200)


class BanUserRequest(BaseSchema):
    reason: str = Field(min_length=3, max_length=500)


class AuditLogOut(BaseSchema):
    id: uuid.UUID
    user_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None
    action: str
    status: str
    ip_address: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# ---------------------------------------------------------------------------
# JWKS
# ---------------------------------------------------------------------------


class JWK(BaseSchema):
    kty: str
    use: str
    alg: str
    kid: str
    n: str
    e: str


class JWKS(BaseSchema):
    keys: list[JWK]

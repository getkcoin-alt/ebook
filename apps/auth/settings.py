"""Configuration for the authentication service.

Everything the service reads from the environment is declared here. The RSA key
material is the one true secret on the platform: this process is the only one that
holds a private key, and every other service verifies with the public half fetched
from ``/.well-known/jwks.json``.
"""

from __future__ import annotations

import base64
import hashlib

from pydantic import computed_field, field_validator

from knowledgeos_core import ServiceSettings


def _normalise_pem(value: str | None) -> str | None:
    """Turn a dashboard-pasted PEM back into a real one.

    Railway (and most secret stores) cannot hold a literal newline in a variable, so
    keys are pasted with ``\\n`` escapes. ``scripts/generate-keys.sh`` prints them in
    exactly that form.
    """
    if not value:
        return None
    pem = value.strip().strip('"').strip("'")
    if "\\n" in pem:
        pem = pem.replace("\\n", "\n")
    return pem.strip() or None


class Settings(ServiceSettings):
    service_name: str = "auth"
    database_schema: str = "auth"
    port: int = 8001

    # This service verifies its own tokens in-process against the local key ring;
    # the URL is still declared because ``Components(auth=True)`` requires one and
    # it is what every other service is pointed at.
    jwks_url: str | None = "http://localhost:8001/.well-known/jwks.json"

    # ---- signing keys ---------------------------------------------------
    #: PEM (PKCS#8) of the RSA private key used to sign access tokens.
    jwt_private_key: str | None = None
    #: PEM of the matching public key. Derived from the private key when omitted.
    jwt_public_key: str | None = None
    #: Previous public key, published alongside the current one during a rotation so
    #: tokens minted before the swap keep verifying until they expire.
    jwt_previous_public_key: str | None = None

    # ---- token lifetimes ------------------------------------------------
    access_token_ttl: int = 900  # 15 minutes — the revocation window (ADR 0003)
    refresh_token_ttl: int = 2_592_000  # 30 days
    mfa_challenge_ttl: int = 300  # how long a half-authenticated login may sit
    email_verification_ttl: int = 86_400
    password_reset_ttl: int = 3_600
    oauth_state_ttl: int = 600

    # ---- credentials policy ---------------------------------------------
    password_min_length: int = 12
    password_max_length: int = 128
    #: Failed logins tolerated before the account is temporarily locked.
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900
    #: Floor applied to credential endpoints so a database hit for a known account
    #: cannot be distinguished from an early return for an unknown one.
    credential_response_floor_ms: int = 120

    # ---- cookies --------------------------------------------------------
    refresh_cookie_name: str = "kos_refresh"
    csrf_cookie_name: str = "kos_csrf"
    #: Scoped to the auth routes: no other endpoint has any use for the cookie, so
    #: it is never attached to a request that cannot consume it.
    cookie_path: str = "/v1/auth"
    cookie_domain: str | None = None
    #: Must be false only for plain-HTTP local development.
    cookie_secure: bool = True

    # ---- two-factor -----------------------------------------------------
    totp_issuer: str = "KnowledgeOS"
    totp_digits: int = 6
    totp_period: int = 30
    #: Fernet key (urlsafe-base64, 32 bytes) protecting TOTP secrets at rest.
    #: Derived from ``internal_api_secret`` when unset so local development works.
    totp_encryption_key: str | None = None
    recovery_code_count: int = 10

    # ---- oauth ----------------------------------------------------------
    google_client_id: str | None = None
    google_client_secret: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    #: Public origin the provider redirects back to. Must match the callback URL
    #: registered with the provider, which is why it is configured, not inferred
    #: from the inbound request (an attacker controls the Host header).
    oauth_redirect_base_url: str = "http://localhost:8000"
    oauth_http_timeout: float = 10.0

    # ---- denylist -------------------------------------------------------
    #: Extra seconds a ban stays in Redis beyond the access-token lifetime, covering
    #: clock skew between this service and the gateway.
    denylist_grace_seconds: int = 300

    @field_validator("jwt_private_key", "jwt_public_key", "jwt_previous_public_key", mode="after")
    @classmethod
    def _pem(cls, value: str | None) -> str | None:
        return _normalise_pem(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def denylist_ttl(self) -> int:
        """How long a denylist entry lives.

        A ban only has to outlive access tokens minted before it — once those expire
        the user cannot obtain new ones, because login and refresh both check the
        database.
        """
        return self.access_token_ttl + self.denylist_grace_seconds

    @property
    def totp_fernet_key(self) -> bytes:
        """Key used to encrypt TOTP secrets at rest."""
        if self.totp_encryption_key:
            return self.totp_encryption_key.encode()
        digest = hashlib.sha256(f"totp:{self.internal_api_secret}".encode()).digest()
        return base64.urlsafe_b64encode(digest)


settings = Settings()

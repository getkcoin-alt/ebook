"""Token verification, password hashing and internal request signing.

**Why RS256 and not HS256.** The auth service holds the private key and is the only
process that can mint a token. Every other service verifies with the public key
fetched from ``/.well-known/jwks.json``. A compromised book service therefore cannot
forge an admin token, and verification costs no network round trip because the JWKS
is cached.

**Key rotation.** Tokens carry a ``kid`` header. The auth service can publish a new
key alongside the old one; verifiers pick by ``kid`` and refresh the cache on an
unknown one, so rotation needs no synchronised deploy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import bcrypt
import httpx
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

from .errors import ForbiddenError, UnauthorizedError
from .logging import get_logger

logger = get_logger(__name__)

# The `bcrypt` package is used directly rather than through passlib: passlib 1.7.x
# imports the stdlib `crypt` module, which was removed in Python 3.13, and has had
# no release since 2020. Talking to bcrypt directly removes that upgrade blocker.

#: Cost factor. 12 rounds is ~250ms per hash on a typical cloud vCPU — a deliberate
#: balance between login latency and offline-cracking resistance. Raising this is
#: safe: `needs_rehash` upgrades existing hashes transparently on next login.
BCRYPT_ROUNDS = 12

#: bcrypt truncates input at 72 bytes. Longer passwords are pre-hashed so their full
#: entropy is used rather than silently discarded — otherwise two passwords sharing
#: a 72-byte prefix would be interchangeable.
_BCRYPT_MAX_BYTES = 72


def _prepare_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        # base64 of the digest, not hex: 44 bytes instead of 64, comfortably under
        # the limit while preserving all 256 bits.
        return base64.b64encode(hashlib.sha256(raw).digest())
    return raw


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare_password(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare_password(password), hashed.encode())
    except (ValueError, TypeError):
        # Malformed or truncated hash in the database — treat as a failed login
        # rather than a 500, and never leak which of the two it was.
        return False


def needs_rehash(hashed: str) -> bool:
    """True when a stored hash predates the current cost factor.

    Call after a successful login and re-store the upgraded hash, so cost increases
    roll out across the user base without a migration or a forced password reset.
    """
    try:
        # Format: $2b$<rounds>$<salt+digest>
        parts = hashed.split("$")
        return int(parts[2]) < BCRYPT_ROUNDS
    except (IndexError, ValueError):
        return True


@dataclass(slots=True)
class Principal:
    """The authenticated caller, decoded from a verified access token."""

    user_id: str
    email: str | None = None
    roles: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    session_id: str | None = None
    token_id: str | None = None
    is_service: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def has_role(self, *roles: str) -> bool:
        return bool(set(roles) & set(self.roles))

    def has_permission(self, permission: str) -> bool:
        """Check a permission, honouring ``resource:*`` wildcards and superadmin."""
        if "superadmin" in self.roles:
            return True
        if permission in self.permissions:
            return True
        resource = permission.split(":", 1)[0]
        return f"{resource}:*" in self.permissions or "*" in self.permissions

    def require_role(self, *roles: str) -> None:
        if not self.has_role(*roles):
            raise ForbiddenError(
                "This action requires a different role.",
                details={"required_any_of": list(roles)},
            )

    def require_permission(self, permission: str) -> None:
        if not self.has_permission(permission):
            raise ForbiddenError(
                "This action requires an additional permission.",
                details={"required": permission},
            )


class JWKSCache:
    """Fetches and caches the auth service's public keys.

    Refresh policy: serve from cache until the TTL expires; on an unknown ``kid``,
    refresh once immediately (rate-limited to one fetch per 10s so a token flood
    cannot turn into a stampede against the auth service).
    """

    def __init__(self, jwks_url: str, *, ttl: int = 600, timeout: float = 5.0) -> None:
        self._url = jwks_url
        self._ttl = ttl
        self._timeout = timeout
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        self._last_forced_refresh: float = 0.0

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            payload = response.json()
        keys = {key["kid"]: key for key in payload.get("keys", []) if "kid" in key}
        if not keys:
            raise UnauthorizedError("Auth service published an empty JWKS.")
        self._keys = keys
        self._fetched_at = time.monotonic()
        logger.info("jwks.refreshed", key_count=len(keys), url=self._url)

    async def get_key(self, kid: str) -> dict[str, Any]:
        now = time.monotonic()
        if not self._keys or (now - self._fetched_at) > self._ttl:
            await self._fetch()
        if kid not in self._keys and (now - self._last_forced_refresh) > 10:
            self._last_forced_refresh = now
            await self._fetch()
        if kid not in self._keys:
            raise UnauthorizedError("Token was signed with an unrecognised key.")
        return self._keys[kid]


class TokenVerifier:
    """Verifies RS256 access tokens issued by the auth service."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        algorithms: tuple[str, ...] = ("RS256",),
        cache_ttl: int = 600,
        leeway: int = 10,
    ) -> None:
        self._jwks = JWKSCache(jwks_url, ttl=cache_ttl)
        self._issuer = issuer
        self._audience = audience
        self._algorithms = list(algorithms)
        self._leeway = leeway

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise UnauthorizedError("Malformed access token.") from exc

        kid = header.get("kid")
        if not kid:
            raise UnauthorizedError("Access token is missing a key id.")
        # Reject a token whose declared algorithm we do not accept before touching
        # the key material — this is the classic `alg: none` / algorithm-confusion
        # defence.
        if header.get("alg") not in self._algorithms:
            raise UnauthorizedError("Unsupported token signing algorithm.")

        key = await self._jwks.get_key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                    "require_exp": True,
                    "require_sub": True,
                    "leeway": self._leeway,
                },
            )
        except ExpiredSignatureError as exc:
            raise UnauthorizedError("Access token has expired.", code="token_expired") from exc
        except JWTError as exc:
            logger.info("auth.token_rejected", reason=str(exc))
            raise UnauthorizedError("Access token is invalid.") from exc

        if claims.get("typ") != "access":
            # Refresh tokens are long-lived and must never authorise an API call.
            raise UnauthorizedError("A refresh token cannot be used as an access token.")

        return Principal(
            user_id=str(claims["sub"]),
            email=claims.get("email"),
            roles=list(claims.get("roles", [])),
            permissions=list(claims.get("permissions", [])),
            session_id=claims.get("sid"),
            token_id=claims.get("jti"),
            is_service=claims.get("typ") == "service",
            raw=claims,
        )


# ---- internal service-to-service authentication -------------------------


def sign_internal_request(
    secret: str, *, method: str, path: str, body_hash: str = "", timestamp: int | None = None
) -> tuple[str, str]:
    """Sign an internal call. Returns ``(timestamp, signature)``.

    Guards the private network against SSRF and against a leaked container being
    able to call ``/internal`` endpoints. The timestamp is inside the signed payload
    so a captured header cannot be replayed after the window closes.
    """
    ts = timestamp or int(time.time())
    payload = f"{ts}.{method.upper()}.{path}.{body_hash}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return str(ts), signature


def verify_internal_request(
    secret: str,
    *,
    method: str,
    path: str,
    timestamp: str,
    signature: str,
    body_hash: str = "",
    max_age: int = 300,
) -> bool:
    """Constant-time verification of an internal signature."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > max_age:
        return False
    _, expected = sign_internal_request(
        secret, method=method, path=path, body_hash=body_hash, timestamp=ts
    )
    return hmac.compare_digest(expected, signature)


def hash_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest() if body else ""


# ---- opaque tokens ------------------------------------------------------


def generate_token(nbytes: int = 32) -> str:
    """Cryptographically secure URL-safe token (email verification, resets, API keys)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Hash an opaque token for storage.

    Reset and verification tokens are single-use, high-entropy and short-lived, so a
    fast SHA-256 is appropriate — bcrypt's work factor exists to slow down guessing
    of low-entropy human passwords, which is not the threat here.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)

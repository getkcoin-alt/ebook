"""RSA signing key ring and JWKS publication.

The private key exists **only** in this process's environment. It is never written
to the database — a database compromise must not yield the ability to mint tokens.
The `signing_keys` table holds public halves solely so JWKS survives a restart and
so rotations are auditable.

Rotation, with no coordinated deploy:

1. Generate a new keypair. Set it as ``JWT_PRIVATE_KEY``/``JWT_PUBLIC_KEY`` and move
   the outgoing public key to ``JWT_PREVIOUS_PUBLIC_KEY``.
2. Deploy. New tokens are signed with the new ``kid``; JWKS publishes both keys, so
   tokens minted before the swap keep verifying until they expire.
3. After one access-token lifetime, drop the previous key.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import get_logger
from models import SigningKey
from settings import Settings

logger = get_logger(__name__)

MIN_RSA_BITS = 2048


def _b64url_uint(value: int) -> str:
    """Encode an RSA parameter as base64url, per RFC 7518."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def compute_kid(public_pem: str) -> str:
    """Stable key id: a SHA-256 thumbprint of the public key.

    Deriving the ``kid`` from the key itself (rather than a random value) means the
    same key always gets the same id — restarts and replicas agree without
    coordination, and a rotation is visibly a different id.
    """
    return hashlib.sha256(public_pem.strip().encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class KeyPair:
    kid: str
    private_pem: str | None
    public_pem: str
    algorithm: str = "RS256"

    @property
    def can_sign(self) -> bool:
        return self.private_pem is not None

    def to_jwk(self) -> dict[str, str]:
        public = serialization.load_pem_public_key(self.public_pem.encode())
        if not isinstance(public, rsa.RSAPublicKey):
            raise ValueError("Only RSA public keys can be published as RS256 JWKs.")
        numbers = public.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": self.algorithm,
            "kid": self.kid,
            "n": _b64url_uint(numbers.n),
            "e": _b64url_uint(numbers.e),
        }


def generate_keypair(bits: int = MIN_RSA_BITS) -> tuple[str, str]:
    """Generate an RSA keypair. Returns ``(private_pem, public_pem)``."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _derive_public_pem(private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


class KeyRing:
    """The keys this service signs and verifies with."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active: KeyPair | None = None
        self._verify_only: list[KeyPair] = []
        self._ephemeral = False

    @property
    def active(self) -> KeyPair:
        if self._active is None:
            raise RuntimeError("Key ring has not been loaded.")
        return self._active

    @property
    def all_public(self) -> list[KeyPair]:
        """Every key published in JWKS: the signer plus any still-valid predecessors."""
        return [self.active, *self._verify_only]

    @property
    def is_ephemeral(self) -> bool:
        """True when the key was generated at boot rather than supplied."""
        return self._ephemeral

    def public_pem_for(self, kid: str) -> str | None:
        for key in self.all_public:
            if key.kid == kid:
                return key.public_pem
        return None

    def load(self) -> None:
        """Build the ring from configuration."""
        private_pem = self._settings.jwt_private_key
        public_pem = self._settings.jwt_public_key

        if not private_pem:
            if self._settings.is_production:
                # Refusing to boot is correct: an ephemeral key would silently
                # invalidate every session on every restart and on every replica.
                raise RuntimeError(
                    "JWT_PRIVATE_KEY is required in production. "
                    "Generate one with ./scripts/generate-keys.sh"
                )
            private_pem, public_pem = generate_keypair()
            self._ephemeral = True
            logger.warning(
                "auth.ephemeral_signing_key",
                detail=(
                    "No JWT_PRIVATE_KEY set; generated a throwaway keypair. "
                    "Tokens will not survive a restart and replicas will disagree."
                ),
            )

        self._validate_private(private_pem)
        public_pem = public_pem or _derive_public_pem(private_pem)

        # A mismatched pair produces tokens nothing can verify — catch it at boot,
        # not on the first login.
        if public_pem.strip() != _derive_public_pem(private_pem).strip():
            raise RuntimeError(
                "JWT_PUBLIC_KEY does not match JWT_PRIVATE_KEY. Regenerate the pair."
            )

        self._active = KeyPair(
            kid=compute_kid(public_pem), private_pem=private_pem, public_pem=public_pem
        )

        self._verify_only = []
        if previous := self._settings.jwt_previous_public_key:
            self._verify_only.append(
                KeyPair(kid=compute_kid(previous), private_pem=None, public_pem=previous)
            )

        logger.info(
            "auth.keyring_loaded",
            active_kid=self._active.kid,
            verify_only=[k.kid for k in self._verify_only],
            ephemeral=self._ephemeral,
        )

    @staticmethod
    def _validate_private(private_pem: str) -> None:
        try:
            key = serialization.load_pem_private_key(private_pem.encode(), password=None)
        except Exception as exc:
            raise RuntimeError(
                "JWT_PRIVATE_KEY is not a valid PEM private key. Newlines must be real "
                "newlines or literal \\n escapes."
            ) from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError("JWT_PRIVATE_KEY must be an RSA key for RS256.")
        if key.key_size < MIN_RSA_BITS:
            raise RuntimeError(
                f"JWT_PRIVATE_KEY is {key.key_size}-bit; {MIN_RSA_BITS} is the minimum."
            )

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        return {"keys": [key.to_jwk() for key in self.all_public]}

    async def sync_to_database(self, session: AsyncSession) -> None:
        """Record public halves so rotations are auditable.

        Purely a record: JWKS is served from the in-memory ring, so this failing
        must never prevent the service from starting.
        """
        now = datetime.now(UTC)
        published = {key.kid for key in self.all_public}

        existing = {
            row.kid: row for row in (await session.execute(select(SigningKey))).scalars().all()
        }

        for key in self.all_public:
            is_active = key.kid == self.active.kid
            if (row := existing.get(key.kid)) is None:
                session.add(
                    SigningKey(
                        kid=key.kid,
                        algorithm=key.algorithm,
                        public_pem=key.public_pem,
                        status="active" if is_active else "retiring",
                        activated_at=now,
                    )
                )
            else:
                row.status = "active" if is_active else "retiring"

        for kid, row in existing.items():
            if kid not in published and row.status != "retired":
                row.status = "retired"
                row.retired_at = now

        await session.flush()

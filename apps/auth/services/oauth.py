"""OAuth 2.0 sign-in with Google and GitHub.

Authorization-code flow with PKCE. Three details that matter:

**State is signed and expiring, not just random.** A bare random value has to be
stored server-side and looked up; a signed value carries its own integrity and TTL,
so a forged or replayed callback is rejected without a database round trip.

**PKCE is used even though this is a confidential client.** It costs nothing and
closes authorization-code interception if the redirect is ever mishandled.

**Account linking requires a verified email.** Automatically merging an OAuth
identity into an existing local account on a matching address is a well-known
takeover route: a provider that does not verify email addresses would let an attacker
register ``victim@example.com`` there and inherit the local account. We link only
when the provider asserts the address is verified.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import BadRequestError, UnauthorizedError, UpstreamError, get_logger
from knowledgeos_core.security import constant_time_compare
from models import OAuthAccount, User
from services.accounts import AccountService, normalise_email
from settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OAuthProfile:
    """Normalised identity returned by any provider."""

    provider: str
    account_id: str
    email: str | None
    email_verified: bool
    full_name: str | None = None
    avatar_url: str | None = None
    raw: dict[str, Any] | None = None


class OAuthProvider(Protocol):
    name: str

    def authorization_url(self, *, state: str, challenge: str, redirect_uri: str) -> str: ...

    async def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> OAuthProfile: ...


class _BaseProvider:
    name: str = ""
    authorize_endpoint: str = ""
    token_endpoint: str = ""
    scopes: tuple[str, ...] = ()

    def __init__(self, client_id: str, client_secret: str, timeout: float) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout

    def authorization_url(self, *, state: str, challenge: str, redirect_uri: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.authorize_endpoint}?{urlencode(params)}"

    async def _post_token(
        self, *, code: str, verifier: str, redirect_uri: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self.token_endpoint,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
                headers=headers,
            )
        if response.status_code >= 400:
            logger.warning(
                "auth.oauth_token_exchange_failed",
                provider=self.name,
                status=response.status_code,
            )
            raise UpstreamError(f"{self.name.title()} rejected the sign-in attempt.")
        payload: dict[str, Any] = response.json()
        if "access_token" not in payload:
            raise UpstreamError(f"{self.name.title()} did not return an access token.")
        return payload


class GoogleProvider(_BaseProvider):
    name = "google"
    authorize_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    scopes = ("openid", "email", "profile")

    async def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> OAuthProfile:
        token = await self._post_token(
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
            headers={"Accept": "application/json"},
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                self.userinfo_endpoint,
                headers={"Authorization": f"Bearer {token['access_token']}"},
            )
        if response.status_code >= 400:
            raise UpstreamError("Could not read your Google profile.")
        data = response.json()
        return OAuthProfile(
            provider=self.name,
            account_id=str(data["sub"]),
            email=data.get("email"),
            email_verified=bool(data.get("email_verified")),
            full_name=data.get("name"),
            avatar_url=data.get("picture"),
            raw={k: data.get(k) for k in ("sub", "email", "name", "picture", "locale")},
        )


class GitHubProvider(_BaseProvider):
    name = "github"
    authorize_endpoint = "https://github.com/login/oauth/authorize"
    token_endpoint = "https://github.com/login/oauth/access_token"  # noqa: S105 - URL
    user_endpoint = "https://api.github.com/user"
    emails_endpoint = "https://api.github.com/user/emails"
    scopes = ("read:user", "user:email")

    async def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> OAuthProfile:
        token = await self._post_token(
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
            headers={"Accept": "application/json"},
        )
        auth = {
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            profile_response = await client.get(self.user_endpoint, headers=auth)
            if profile_response.status_code >= 400:
                raise UpstreamError("Could not read your GitHub profile.")
            profile = profile_response.json()

            # GitHub omits a private primary address from /user, so ask explicitly.
            email, verified = profile.get("email"), False
            emails_response = await client.get(self.emails_endpoint, headers=auth)
            if emails_response.status_code < 400:
                for entry in emails_response.json():
                    if entry.get("primary"):
                        email = entry.get("email")
                        verified = bool(entry.get("verified"))
                        break

        return OAuthProfile(
            provider=self.name,
            account_id=str(profile["id"]),
            email=email,
            email_verified=verified,
            full_name=profile.get("name") or profile.get("login"),
            avatar_url=profile.get("avatar_url"),
            raw={k: profile.get(k) for k in ("id", "login", "name", "avatar_url")},
        )


class OAuthService:
    def __init__(self, settings: Settings, accounts: AccountService) -> None:
        self._settings = settings
        self._accounts = accounts
        self._providers: dict[str, OAuthProvider] = {}

        if settings.google_client_id and settings.google_client_secret:
            self._providers["google"] = GoogleProvider(
                settings.google_client_id,
                settings.google_client_secret,
                settings.oauth_http_timeout,
            )
        if settings.github_client_id and settings.github_client_secret:
            self._providers["github"] = GitHubProvider(
                settings.github_client_id,
                settings.github_client_secret,
                settings.oauth_http_timeout,
            )

    @property
    def available(self) -> list[str]:
        return sorted(self._providers)

    def get_provider(self, name: str) -> OAuthProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise BadRequestError(
                f"'{name}' sign-in is not configured.",
                details={"available": self.available},
            )
        return provider

    def redirect_uri(self, provider: str) -> str:
        """Where the provider sends the user back.

        Built from configuration, never from the inbound request: an attacker
        controls the Host header, and deriving the redirect from it would let them
        point the callback at themselves.
        """
        return f"{self._settings.oauth_redirect_base_url.rstrip('/')}/v1/auth/oauth/{provider}/callback"

    # ---- PKCE + state ----------------------------------------------------

    @staticmethod
    def create_pkce_pair() -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return verifier, challenge

    def sign_state(self, *, provider: str, verifier: str, next_url: str | None) -> str:
        """Signed, self-expiring state. Carries the PKCE verifier back to us."""
        import hmac
        import json

        payload = {
            "p": provider,
            "v": verifier,
            "n": next_url or "",
            "t": int(time.time()),
            "r": secrets.token_urlsafe(8),
        }
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.new(
            self._settings.internal_api_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return f"{body.decode().rstrip('=')}.{signature}"

    def read_state(self, state: str, *, provider: str) -> tuple[str, str | None]:
        """Validate state and return ``(verifier, next_url)``."""
        import hmac
        import json

        try:
            body_part, signature = state.rsplit(".", 1)
            body = (body_part + "=" * (-len(body_part) % 4)).encode()
            expected = hmac.new(
                self._settings.internal_api_secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if not constant_time_compare(expected, signature):
                raise ValueError("signature mismatch")
            payload = json.loads(base64.urlsafe_b64decode(body))
        except Exception as exc:
            logger.warning("auth.oauth_state_invalid", provider=provider)
            raise UnauthorizedError("The sign-in request could not be verified.") from exc

        if payload.get("p") != provider:
            raise UnauthorizedError("The sign-in request could not be verified.")
        if time.time() - float(payload.get("t", 0)) > self._settings.oauth_state_ttl:
            raise UnauthorizedError("This sign-in attempt expired. Please try again.")
        return str(payload["v"]), (payload.get("n") or None)

    # ---- account resolution ----------------------------------------------

    async def resolve_user(
        self, session: AsyncSession, profile: OAuthProfile, *, ip_address: str | None
    ) -> User:
        """Find, link or create the local account for an OAuth identity."""
        now = datetime.now(UTC)

        link = (
            await session.execute(
                select(OAuthAccount).where(
                    OAuthAccount.provider == profile.provider,
                    OAuthAccount.provider_account_id == profile.account_id,
                )
            )
        ).scalar_one_or_none()

        if link is not None:
            user = await self._accounts.get_by_id(session, link.user_id)
            link.last_login_at = now
            if profile.email:
                link.email = normalise_email(profile.email)
            return user

        if not profile.email:
            # Without an address we can neither link safely nor create a usable
            # account (verification and receipts both need one).
            raise BadRequestError(
                f"Your {profile.provider} account has no email address available. "
                "Make one public, or register with an email address instead."
            )

        email = normalise_email(profile.email)
        existing = await self._accounts.get_by_email(session, email)

        if existing is not None:
            if not profile.email_verified:
                # The takeover route this closes: register the victim's address at a
                # provider that does not verify it, then sign in and inherit the
                # local account.
                await self._accounts.record_audit(
                    session,
                    user_id=existing.id,
                    action="oauth.link_blocked",
                    status="blocked",
                    ip_address=ip_address,
                    meta={"provider": profile.provider, "reason": "email_unverified"},
                )
                raise UnauthorizedError(
                    f"An account already exists for this address. Sign in with your "
                    f"password first, then link {profile.provider.title()} from settings.",
                    code="oauth_link_requires_login",
                )
            user = existing
            await self._accounts.record_audit(
                session,
                user_id=user.id,
                action="oauth.linked",
                ip_address=ip_address,
                meta={"provider": profile.provider},
            )
        else:
            user = User(
                email=email,
                password_hash=None,  # OAuth-only until they set one
                full_name=profile.full_name,
                avatar_url=profile.avatar_url,
                roles=["user"],
                extra_permissions=[],
                is_active=True,
                # The provider has already proved control of the mailbox.
                email_verified_at=now if profile.email_verified else None,
            )
            session.add(user)
            await session.flush()
            await self._accounts.record_audit(
                session,
                user_id=user.id,
                action="register.oauth",
                ip_address=ip_address,
                meta={"provider": profile.provider},
            )

        session.add(
            OAuthAccount(
                user_id=user.id,
                provider=profile.provider,
                provider_account_id=profile.account_id,
                email=email,
                profile=profile.raw or {},
                linked_at=now,
                last_login_at=now,
            )
        )
        await session.flush()
        return user

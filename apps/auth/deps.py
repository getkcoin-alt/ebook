"""Service-local dependencies.

The auth service is unusual in one respect: it verifies its own tokens against the
in-process key ring rather than fetching JWKS over HTTP from itself. Doing otherwise
would make the service depend on its own network reachability to serve a request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from knowledgeos_core import UnauthorizedError
from knowledgeos_core.deps import Ctx, DbSession
from knowledgeos_core.security import Principal
from models import User
from services import AccountService, KeyRing, MfaService, TokenService
from services.oauth import OAuthService

__all__ = [
    "AuthedUser",
    "CurrentPrincipal",
    "DbSession",
    "accounts_service",
    "client_ip",
    "key_ring",
    "mfa_service",
    "oauth_service",
    "token_service",
    "user_agent",
]


def key_ring(ctx: Ctx) -> KeyRing:
    return ctx.extras["keyring"]  # type: ignore[no-any-return]


def token_service(ctx: Ctx) -> TokenService:
    return ctx.extras["tokens"]  # type: ignore[no-any-return]


def accounts_service(ctx: Ctx) -> AccountService:
    return ctx.extras["accounts"]  # type: ignore[no-any-return]


def mfa_service(ctx: Ctx) -> MfaService:
    return ctx.extras["mfa"]  # type: ignore[no-any-return]


def oauth_service(ctx: Ctx) -> OAuthService:
    return ctx.extras["oauth"]  # type: ignore[no-any-return]


def client_ip(request: Request) -> str | None:
    """The caller's address, taken from the proxy header Railway sets.

    Only the first entry is trusted: later entries can be forged by the client, and
    the platform's proxy always prepends the real peer.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent", "")[:400] or None


async def get_principal(
    request: Request,
    ctx: Ctx,
) -> Principal:
    """Verify a bearer token against the local key ring."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise UnauthorizedError("An access token is required for this endpoint.")
    token = header[7:].strip()
    if not token:
        raise UnauthorizedError("An access token is required for this endpoint.")

    from jose import jwt
    from jose.exceptions import ExpiredSignatureError, JWTError

    ring: KeyRing = ctx.extras["keyring"]
    settings = ctx.settings

    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except JWTError as exc:
        raise UnauthorizedError("Malformed access token.") from exc

    public_pem = ring.public_pem_for(kid) if kid else None
    if public_pem is None:
        raise UnauthorizedError("Token was signed with an unrecognised key.")

    try:
        claims = jwt.decode(
            token,
            public_pem,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
    except ExpiredSignatureError as exc:
        raise UnauthorizedError("Access token has expired.", code="token_expired") from exc
    except JWTError as exc:
        raise UnauthorizedError("Access token is invalid.") from exc

    if claims.get("typ") != "access":
        # An MFA challenge or refresh token must never authorise an API call.
        raise UnauthorizedError("This token cannot be used to access the API.")

    return Principal(
        user_id=str(claims["sub"]),
        email=claims.get("email"),
        roles=list(claims.get("roles", [])),
        permissions=list(claims.get("permissions", [])),
        session_id=claims.get("sid"),
        token_id=claims.get("jti"),
        raw=claims,
    )


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def get_authed_user(
    session: DbSession,
    principal: CurrentPrincipal,
    accounts: Annotated[AccountService, Depends(accounts_service)],
) -> User:
    """Load the token's subject and re-check that the account is still usable.

    The token may have been minted up to 15 minutes ago, so state can have changed.
    """
    import uuid

    user = await accounts.get_by_id(session, uuid.UUID(principal.user_id))
    if not user.is_active or user.banned_at is not None:
        raise UnauthorizedError("This account is no longer active.", code="account_inactive")
    return user


AuthedUser = Annotated[User, Depends(get_authed_user)]

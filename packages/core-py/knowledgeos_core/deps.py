"""FastAPI dependencies: authentication, authorisation, rate limits, idempotency.

Usage in a service::

    @router.get("/me")
    async def me(user: CurrentUser) -> UserOut: ...

    @router.delete("/books/{id}", dependencies=[Depends(require_permission("books:delete"))])
    async def delete_book(...): ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .app import AppContext
from .db import Database
from .errors import ForbiddenError, UnauthorizedError
from .idempotency import IdempotencyStore
from .logging import user_id_ctx
from .ratelimit import POLICIES, RateLimiter, RateLimitPolicy
from .redis import RedisClient
from .security import Principal, verify_internal_request
from .storage import ObjectStorage

# auto_error=False so a missing header produces our error envelope rather than
# FastAPI's default `{"detail": ...}` shape.
_bearer = HTTPBearer(auto_error=False, description="RS256 access token from the auth service.")


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx  # type: ignore[no-any-return]


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def get_session(ctx: Ctx) -> AsyncIterator[AsyncSession]:
    async for session in ctx.require_db().session():
        yield session


DbSession = Annotated[AsyncSession, Depends(get_session)]


def get_db(ctx: Ctx) -> Database:
    return ctx.require_db()


def get_redis(ctx: Ctx) -> RedisClient:
    return ctx.require_redis()


def get_storage(ctx: Ctx) -> ObjectStorage:
    return ctx.require_storage()


def get_limiter(ctx: Ctx) -> RateLimiter:
    if ctx.limiter is None:
        raise RuntimeError("Rate limiting requires Components(redis=True).")
    return ctx.limiter


def get_idempotency(ctx: Ctx) -> IdempotencyStore:
    if ctx.idempotency is None:
        raise RuntimeError("Idempotency requires Components(redis=True).")
    return ctx.idempotency


Redis = Annotated[RedisClient, Depends(get_redis)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]
Limiter = Annotated[RateLimiter, Depends(get_limiter)]
Idempotency = Annotated[IdempotencyStore, Depends(get_idempotency)]


# ---- authentication -----------------------------------------------------


async def get_current_principal(
    ctx: Ctx,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    """Require a valid access token. Raises 401 otherwise."""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("An access token is required for this endpoint.")
    if ctx.verifier is None:
        raise RuntimeError("This service was not created with Components(auth=True).")
    principal = await ctx.verifier.verify(credentials.credentials)
    # Bind for logging so every subsequent line in this request carries the user id.
    user_id_ctx.set(principal.user_id)
    return principal


async def get_optional_principal(
    ctx: Ctx,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal | None:
    """Identify the caller when possible, but allow anonymous access.

    For endpoints whose *response* differs when signed in — a book detail page that
    shows "Read now" instead of "Buy" — without making auth mandatory.
    """
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await get_current_principal(ctx, credentials)
    except UnauthorizedError:
        # An expired token on a public page should render the anonymous view, not
        # break the page.
        return None


CurrentUser = Annotated[Principal, Depends(get_current_principal)]
OptionalUser = Annotated[Principal | None, Depends(get_optional_principal)]


# ---- authorisation ------------------------------------------------------


def require_roles(*roles: str) -> Callable[[Principal], Principal]:
    """Dependency factory: caller must hold at least one of ``roles``."""

    def dependency(principal: CurrentUser) -> Principal:
        principal.require_role(*roles)
        return principal

    return dependency


def require_permission(permission: str) -> Callable[[Principal], Principal]:
    """Dependency factory: caller must hold ``permission``."""

    def dependency(principal: CurrentUser) -> Principal:
        principal.require_permission(permission)
        return principal

    return dependency


def require_self_or_permission(
    permission: str, *, param: str = "user_id"
) -> Callable[..., Principal]:
    """Allow a user to act on their own resource, or a staff member with ``permission``.

    Covers the very common "users can edit their own profile; admins can edit
    anyone's" rule without each endpoint re-implementing the comparison.
    """

    def dependency(request: Request, principal: CurrentUser) -> Principal:
        target = request.path_params.get(param)
        if target is not None and str(target) == principal.user_id:
            return principal
        principal.require_permission(permission)
        return principal

    return dependency


AdminUser = Annotated[Principal, Depends(require_roles("admin", "superadmin"))]
StaffUser = Annotated[Principal, Depends(require_roles("moderator", "admin", "superadmin"))]


# ---- internal (service-to-service) authentication ------------------------


async def require_internal_caller(
    request: Request,
    ctx: Ctx,
    x_internal_timestamp: Annotated[str | None, Header()] = None,
    x_internal_signature: Annotated[str | None, Header()] = None,
    x_internal_service: Annotated[str | None, Header()] = None,
) -> str:
    """Guard ``/internal/*`` routes. Returns the calling service's name.

    Network reachability is not authorisation: on Railway's private network any
    container can reach any other, and an SSRF bug in a public endpoint could be
    used to call an internal one. The HMAC makes the caller prove it holds the
    shared secret.
    """
    if not (x_internal_timestamp and x_internal_signature and x_internal_service):
        raise UnauthorizedError("This endpoint requires a signed internal request.")

    body = await request.body()
    from .security import hash_body

    valid = verify_internal_request(
        ctx.settings.internal_api_secret,
        method=request.method,
        path=request.url.path,
        timestamp=x_internal_timestamp,
        signature=x_internal_signature,
        body_hash=hash_body(body),
    )
    if not valid:
        raise ForbiddenError("The internal request signature is invalid or expired.")
    return x_internal_service


InternalCaller = Annotated[str, Depends(require_internal_caller)]


# ---- rate limiting ------------------------------------------------------


def client_identifier(request: Request, principal: Principal | None = None) -> str:
    """The key a rate limit is counted against.

    Authenticated callers are limited per user id so a shared office IP does not
    throttle everyone. Anonymous callers fall back to the client IP taken from
    ``X-Forwarded-For``'s *first* entry, which is the one Railway's proxy sets.
    """
    if principal is not None:
        return f"user:{principal.user_id}"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def rate_limit(policy_name: str = "authenticated", *, cost: int = 1) -> Callable[..., object]:
    """Dependency factory applying a named policy from :data:`POLICIES`."""
    policy: RateLimitPolicy = POLICIES.get(
        policy_name, RateLimitPolicy(limit=60, window_seconds=60, scope=policy_name)
    )

    async def dependency(
        request: Request,
        limiter: Limiter,
        principal: OptionalUser = None,
    ) -> None:
        identifier = client_identifier(request, principal)
        result = await limiter.enforce(identifier, policy, cost=cost)
        # Stash so a response-level hook can surface remaining budget to clients.
        request.state.rate_limit = result

    return dependency

"""JWKS publication and internal service-to-service endpoints.

``/.well-known/jwks.json`` is the one genuinely public endpoint here — every other
service fetches it to verify tokens offline. Everything under ``/internal`` requires
an HMAC signature: private-network reachability is not authorisation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select

from deps import DbSession, accounts_service, key_ring, token_service
from knowledgeos_core import ListResponse, MessageResponse, NotFoundError
from knowledgeos_core.deps import Ctx, InternalCaller
from models import User
from schemas import (
    JWKS,
    BanUserRequest,
    InternalUserBatchRequest,
    InternalUserOut,
    PruneResponse,
)
from services import AccountService, KeyRing, MaintenanceService, TokenService
from settings import settings

router = APIRouter(tags=["internal"])

Keys = Annotated[KeyRing, Depends(key_ring)]
Accounts = Annotated[AccountService, Depends(accounts_service)]
Tokens = Annotated[TokenService, Depends(token_service)]
Maintenance = Annotated[MaintenanceService, Depends(lambda: MaintenanceService(settings))]


def _to_internal(user: User) -> InternalUserOut:
    return InternalUserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        roles=list(user.roles or []),
        is_active=user.is_active,
        email_verified=user.is_email_verified,
    )


# ---------------------------------------------------------------------------
# JWKS — public
# ---------------------------------------------------------------------------


@router.get(
    "/.well-known/jwks.json",
    response_model=JWKS,
    tags=["auth"],
    summary="Public signing keys (JWKS)",
    description=(
        "The RSA public keys used to verify access tokens. Services cache this and "
        "verify offline, so no request path depends on the auth service being reachable."
    ),
)
async def jwks(keys: Keys, response: Response) -> JWKS:
    # Cacheable, but briefly: a rotation must propagate quickly, and verifiers also
    # force a refresh when they meet an unknown `kid`.
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    return JWKS.model_validate(keys.jwks())


# ---------------------------------------------------------------------------
# Internal — HMAC-signed callers only
# ---------------------------------------------------------------------------


@router.get(
    "/internal/users/{user_id}",
    response_model=InternalUserOut,
    summary="Fetch one user (internal)",
)
async def internal_get_user(
    user_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    accounts: Accounts,
) -> InternalUserOut:
    return _to_internal(await accounts.get_by_id(session, user_id))


@router.post(
    "/internal/users/batch",
    response_model=ListResponse[InternalUserOut],
    summary="Fetch many users (internal)",
    description=(
        "Batch lookup so a sibling service rendering a list of reviews makes one call "
        "rather than one per row."
    ),
)
async def internal_batch_users(
    payload: InternalUserBatchRequest,
    caller: InternalCaller,
    session: DbSession,
) -> ListResponse[InternalUserOut]:
    if not payload.user_ids:
        return ListResponse[InternalUserOut](items=[], total=0)
    rows = (
        (
            await session.execute(
                select(User).where(User.id.in_(payload.user_ids), User.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    items = [_to_internal(row) for row in rows]
    return ListResponse[InternalUserOut](items=items, total=len(items))


@router.get(
    "/internal/users/{user_id}/active",
    summary="Check whether a user may act (internal)",
)
async def internal_user_active(
    user_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
) -> dict[str, bool]:
    user = await session.get(User, user_id)
    active = bool(user and user.is_active and user.deleted_at is None and user.banned_at is None)
    return {"active": active, "exists": user is not None}


@router.post(
    "/internal/users/{user_id}/ban",
    response_model=MessageResponse,
    summary="Ban a user (internal)",
    description=(
        "Revokes every session and writes a Redis denylist entry, so the ban takes "
        "effect within seconds despite access tokens remaining cryptographically "
        "valid until they expire."
    ),
)
async def internal_ban_user(
    user_id: uuid.UUID,
    payload: BanUserRequest,
    caller: InternalCaller,
    session: DbSession,
    accounts: Accounts,
    tokens: Tokens,
    ctx: Ctx,
) -> MessageResponse:
    user = await accounts.get_by_id(session, user_id)
    await accounts.set_ban(session, user, banned=True, reason=payload.reason, actor_id=None)
    await tokens.revoke_all_sessions(session, user.id, reason="banned")

    if ctx.redis is not None:
        # Key format is part of the platform contract — the gateway reads it.
        # See "Redis contract" in this service's README.
        await ctx.redis.client.set(
            f"kos:denylist:user:{user_id}",
            payload.reason.encode()[:200],
            ex=settings.denylist_ttl,
        )
    return MessageResponse(message="User banned and signed out everywhere.")


@router.post(
    "/internal/users/{user_id}/unban",
    response_model=MessageResponse,
    summary="Lift a ban (internal)",
)
async def internal_unban_user(
    user_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    accounts: Accounts,
    ctx: Ctx,
) -> MessageResponse:
    user = await accounts.get_by_id(session, user_id)
    if user.banned_at is None:
        raise NotFoundError("That user is not banned.")
    await accounts.set_ban(session, user, banned=False, reason=None, actor_id=None)
    if ctx.redis is not None:
        await ctx.redis.client.delete(f"kos:denylist:user:{user_id}")
    return MessageResponse(message="Ban lifted.")


@router.post(
    "/internal/maintenance/prune",
    response_model=PruneResponse,
    summary="Drop expired tokens, sessions and old audit rows (internal)",
    description=(
        "Driven by the worker on a schedule. Nothing else deletes from these tables, "
        "and every one of them gains a row per login — left alone, the tables every "
        "login path reads become the slowest thing in this service.\n\n"
        "A refresh token is dropped only once it is past its expiry **and** past a "
        "grace window. Deleting one the instant it expires destroys the evidence that "
        "makes reuse detection work: a stolen token replayed a minute later should be "
        'recognised as a revoked family, not met with "unknown token" — which is '
        "indistinguishable from a typo and tells an attacker nothing was noticed.\n\n"
        "Audit logs have their own, far longer retention, and security-relevant "
        "actions are exempt from it entirely. Pass `dry_run` to see what would go."
    ),
)
async def internal_prune(
    caller: InternalCaller,
    session: DbSession,
    maintenance: Maintenance,
    dry_run: Annotated[bool, Query(description="Report without deleting.")] = False,
) -> PruneResponse:
    result = await maintenance.prune(session, dry_run=dry_run)
    return PruneResponse(
        refresh_tokens=result.refresh_tokens,
        sessions=result.sessions,
        verification_tokens=result.verification_tokens,
        reset_tokens=result.reset_tokens,
        audit_logs=result.audit_logs,
        total=result.total,
        dry_run=dry_run,
    )

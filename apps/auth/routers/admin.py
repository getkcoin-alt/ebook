"""User administration.

The back-office half of the auth service: search the directory, look at one account,
ban or unban it, and read the audit log.

**These endpoints did not exist until an admin console needed them**, and the reason
is worth recording. Ban and unban were reachable only over the HMAC-signed
`/internal/*` path, which is correct for service-to-service calls and useless to a
browser — so a console could show a customer's orders and their reviews but could not
say who the customer was, let alone stop a fraudulent one. The capability existed; the
door did not.

Two constraints shape everything here:

**Reads need `users:read`, writes need `users:write`.** Support staff who answer "did
my payment go through" should not also be able to ban an account, and a single
`admin` role that grants both is how a support tool becomes an incident.

**No endpoint here returns anything that helps impersonate a user.** No password
hashes, no session ids, no MFA secrets, no tokens. An admin console aggregates
everything about everyone, which makes it the most valuable thing on the platform to
compromise — so it should hold as little as it can get away with.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from deps import (
    CurrentPrincipal,
    DbSession,
    accounts_service,
    directory_service,
    require_permission,
)
from knowledgeos_core import ForbiddenError, MessageResponse, get_logger
from knowledgeos_core.deps import Ctx
from models import User
from schemas import (
    AdminUserOut,
    AdminUserPage,
    AuditLogOut,
    AuditLogPage,
    BanUserRequest,
    UserStats,
)
from services import AccountService, DirectoryService, resolve_permissions
from settings import settings

logger = get_logger(__name__)

USERS_READ = Depends(require_permission("users:read"))
USERS_WRITE = Depends(require_permission("users:write"))

router = APIRouter(prefix="/v1/admin/users", tags=["admin"])
audit_router = APIRouter(prefix="/v1/admin/audit-logs", tags=["admin"])

Accounts = Annotated[AccountService, Depends(accounts_service)]
Directory = Annotated[DirectoryService, Depends(directory_service)]


def _to_admin(user: User) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        locale=user.locale,
        roles=list(user.roles or []),
        permissions=resolve_permissions(user),
        email_verified=user.is_email_verified,
        mfa_enabled=user.mfa_enabled,
        is_active=user.is_active,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=user.last_login_at,
        banned_at=user.banned_at,
        ban_reason=user.ban_reason,
        deleted_at=user.deleted_at,
    )


@router.get(
    "",
    response_model=AdminUserPage,
    summary="Search the user directory",
    description=(
        "Offset paginated **with a real total**, unlike the public feeds. A console "
        "needs to jump to page four and needs to know a search matched 3 people "
        "rather than 3,000 — and a count over this table, queried by a handful of "
        "operators, costs nothing like one on an infinite scroll.\n\n"
        "`search` is a substring match on email and full name only. Never on IP "
        "address or user agent: searching those turns a support tool into a "
        "surveillance one."
    ),
    dependencies=[USERS_READ],
)
async def list_users(
    session: DbSession,
    directory: Directory,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    search: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[str | None, Query(pattern="^(active|banned|unverified|deleted)$")] = None,
    sort_by: Annotated[str, Query(pattern="^(created_at|last_login_at|email)$")] = "created_at",
    sort_order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> AdminUserPage:
    rows, total = await directory.list_users(
        session,
        limit=limit,
        offset=offset,
        search=search,
        status=status,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return AdminUserPage(
        items=[_to_admin(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/stats",
    response_model=UserStats,
    summary="Headline user counts",
    dependencies=[USERS_READ],
)
async def user_stats(
    session: DbSession,
    directory: Directory,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> UserStats:
    return UserStats(window_days=days, **await directory.stats(session, days=days))


@router.get(
    "/{user_id}",
    response_model=AdminUserOut,
    summary="One account",
    dependencies=[USERS_READ],
)
async def get_user(user_id: uuid.UUID, session: DbSession, accounts: Accounts) -> AdminUserOut:
    return _to_admin(await accounts.get_by_id(session, user_id))


@router.post(
    "/{user_id}/ban",
    response_model=MessageResponse,
    summary="Ban an account",
    description=(
        "Signs the user out everywhere and adds them to the gateway's denylist, so "
        "an access token already in flight stops working within its remaining "
        "lifetime rather than at its natural expiry.\n\n"
        "A reason is **required**. A ban with no reason is one nobody can review, "
        "and the person who applied it will not remember by the time it is appealed."
    ),
    dependencies=[USERS_WRITE],
)
async def ban_user(
    user_id: uuid.UUID,
    payload: BanUserRequest,
    session: DbSession,
    accounts: Accounts,
    ctx: Ctx,
    actor: CurrentPrincipal,
) -> MessageResponse:
    actor_id = uuid.UUID(actor.user_id)
    if actor_id == user_id:
        # Not paranoia: the account that can ban is the account whose loss locks
        # everyone out, and a mis-click here is unrecoverable without database access.
        raise ForbiddenError("You cannot ban your own account.", code="cannot_ban_self")

    user = await accounts.get_by_id(session, user_id)
    if "superadmin" in (user.roles or []) and "superadmin" not in actor.roles:
        raise ForbiddenError("Only a superadmin can ban a superadmin.", code="insufficient_role")

    await accounts.set_ban(session, user, banned=True, reason=payload.reason, actor_id=actor_id)
    if ctx.redis is not None:
        # Key format is part of the platform contract — the gateway reads it.
        await ctx.redis.client.set(
            f"kos:denylist:user:{user_id}",
            payload.reason.encode()[:200],
            ex=settings.denylist_ttl,
        )
    logger.warning("auth.user_banned", user_id=str(user_id), actor=str(actor_id))
    return MessageResponse(message="User banned and signed out everywhere.")


@router.post(
    "/{user_id}/unban",
    response_model=MessageResponse,
    summary="Lift a ban",
    dependencies=[USERS_WRITE],
)
async def unban_user(
    user_id: uuid.UUID,
    session: DbSession,
    accounts: Accounts,
    ctx: Ctx,
    actor: CurrentPrincipal,
) -> MessageResponse:
    user = await accounts.get_by_id(session, user_id)
    await accounts.set_ban(
        session, user, banned=False, reason=None, actor_id=uuid.UUID(actor.user_id)
    )
    if ctx.redis is not None:
        await ctx.redis.client.delete(f"kos:denylist:user:{user_id}")
    logger.info("auth.user_unbanned", user_id=str(user_id), actor=actor.user_id)
    return MessageResponse(message="Ban lifted.")


@audit_router.get(
    "",
    response_model=AuditLogPage,
    summary="Search the audit log",
    description=(
        "Windowed by default. This is the largest table in the schema, and an "
        "unbounded `ORDER BY created_at DESC` over all of it — for a console that "
        "almost always wants the last week — is a full scan to render a first page."
    ),
    dependencies=[USERS_READ],
)
async def audit_logs(
    session: DbSession,
    directory: Directory,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> AuditLogPage:
    rows, total = await directory.audit_logs(
        session,
        limit=limit,
        offset=offset,
        user_id=user_id,
        actor_id=actor_id,
        action=action,
        days=days,
    )
    return AuditLogPage(
        items=[AuditLogOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@audit_router.get(
    "/actions",
    response_model=list[str],
    summary="Action names present in the window",
    description=(
        "Populates the filter dropdown. Read from the data rather than a constant: a "
        "hard-coded list goes stale the first time somebody adds an audited action "
        "and forgets this file."
    ),
    dependencies=[USERS_READ],
)
async def audit_actions(
    session: DbSession,
    directory: Directory,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[str]:
    return await directory.actions(session, days=days)

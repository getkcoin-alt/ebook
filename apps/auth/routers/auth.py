"""Registration, login, refresh, logout, password and email verification."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from cookies import clear_auth_cookies, set_auth_cookies
from deps import (
    AuthedUser,
    CurrentPrincipal,
    DbSession,
    accounts_service,
    client_ip,
    mfa_service,
    token_service,
    user_agent,
)
from knowledgeos_core import (
    EventType,
    MessageResponse,
    UnauthorizedError,
    get_logger,
)
from knowledgeos_core.deps import Ctx, rate_limit
from knowledgeos_core.schemas import ROLE_PERMISSIONS
from schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MfaChallengeRequest,
    MfaRequiredResponse,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserOut,
    UserUpdateRequest,
    VerifyEmailRequest,
)
from services import AccountService, MfaService, TokenService
from services.tokens import resolve_permissions
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

Accounts = Annotated[AccountService, Depends(accounts_service)]
Tokens = Annotated[TokenService, Depends(token_service)]
Mfa = Annotated[MfaService, Depends(mfa_service)]
ClientIP = Annotated[str | None, Depends(client_ip)]
UserAgent = Annotated[str | None, Depends(user_agent)]


def _user_out(user: object) -> UserOut:
    from models import User

    assert isinstance(user, User)
    return UserOut(
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
        last_login_at=user.last_login_at,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=MessageResponse,
    summary="Create an account",
    description=(
        "Registers a new account and sends a verification email. Responds identically "
        "whether or not the address is already registered, so the endpoint cannot be "
        "used to discover which addresses have accounts."
    ),
    dependencies=[Depends(rate_limit("register"))],
)
async def register(
    payload: RegisterRequest,
    session: DbSession,
    accounts: Accounts,
    ctx: Ctx,
    ip: ClientIP,
) -> MessageResponse:
    user, raw_token = await accounts.register(
        session,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        locale=payload.locale,
        ip_address=ip,
    )

    if raw_token is not None and ctx.publisher is not None:
        await ctx.publisher.publish(
            EventType.USER_REGISTERED,
            {
                "user_id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "verification_token": raw_token,
            },
        )

    return MessageResponse(
        message="Check your inbox — we've sent a link to confirm your email address."
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=TokenResponse | MfaRequiredResponse,
    summary="Sign in with email and password",
    description=(
        "Returns an access token plus an httpOnly refresh cookie. When the account has "
        "two-factor authentication enabled, returns an MFA challenge instead."
    ),
    dependencies=[Depends(rate_limit("login"))],
)
async def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    accounts: Accounts,
    tokens: Tokens,
    ctx: Ctx,
    ip: ClientIP,
    agent: UserAgent,
) -> TokenResponse | MfaRequiredResponse:
    user = await accounts.authenticate(
        session, email=payload.email, password=payload.password, ip_address=ip
    )

    if user.mfa_enabled:
        challenge, ttl = tokens.mint_mfa_challenge(user.id)
        return MfaRequiredResponse(challenge_token=challenge, expires_in=ttl)

    issued = await tokens.start_session(
        session,
        user,
        ip_address=ip,
        user_agent=agent,
        device_label=payload.device_label,
        auth_method="password",
    )
    await accounts.record_audit(
        session, user_id=user.id, action="login", ip_address=ip, user_agent=agent
    )
    set_auth_cookies(response, issued)

    if ctx.publisher is not None:
        await ctx.publisher.publish(EventType.USER_LOGGED_IN, {"user_id": str(user.id), "ip": ip})

    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        csrf_token=issued.csrf_token,
        user=_user_out(user),
    )


@router.post(
    "/login/mfa",
    response_model=TokenResponse,
    summary="Complete a two-factor sign-in",
    dependencies=[Depends(rate_limit("login"))],
)
async def login_mfa(
    payload: MfaChallengeRequest,
    response: Response,
    session: DbSession,
    accounts: Accounts,
    tokens: Tokens,
    mfa: Mfa,
    ip: ClientIP,
    agent: UserAgent,
) -> TokenResponse:
    user_id = tokens.read_mfa_challenge(payload.challenge_token)
    user = await accounts.get_by_id(session, user_id)
    if not user.is_active or user.banned_at is not None:
        raise UnauthorizedError("This account is no longer active.")

    method = await mfa.verify(session, user, payload.code)

    issued = await tokens.start_session(
        session,
        user,
        ip_address=ip,
        user_agent=agent,
        device_label=None,
        auth_method=f"password+{method}",
    )
    user.last_login_at = datetime.now(UTC)
    user.last_login_ip = ip
    await accounts.record_audit(
        session,
        user_id=user.id,
        action="login.mfa",
        ip_address=ip,
        user_agent=agent,
        meta={"method": method},
    )
    set_auth_cookies(response, issued)
    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        csrf_token=issued.csrf_token,
        user=_user_out(user),
    )


# ---------------------------------------------------------------------------
# Refresh and logout
# ---------------------------------------------------------------------------


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new access token",
    description=(
        "Rotates the refresh token: the presented one is consumed and a successor is "
        "issued. Replaying a consumed token revokes the entire token family, because "
        "it means two parties hold the chain."
    ),
)
async def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    accounts: Accounts,
    tokens: Tokens,
    payload: RefreshRequest | None = None,
) -> TokenResponse:
    raw = request.cookies.get(settings.refresh_cookie_name)
    from_cookie = raw is not None
    if raw is None and payload is not None:
        raw = payload.refresh_token
    if not raw:
        raise UnauthorizedError("No refresh token was supplied.")

    # CSRF only applies to the cookie flow: a non-browser client sending the token
    # in the body is not subject to cross-site request forgery.
    csrf = request.headers.get("x-csrf-token") if from_cookie else None
    if from_cookie and not csrf:
        raise UnauthorizedError("A CSRF token is required to refresh.", code="csrf_missing")

    issued = await tokens.rotate(session, raw_refresh=raw, csrf_token=csrf)
    user = await accounts.get_by_id(session, issued.user_id)
    set_auth_cookies(response, issued)
    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        csrf_token=issued.csrf_token,
        user=_user_out(user),
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Sign out of this device",
)
async def logout(
    request: Request,
    response: Response,
    session: DbSession,
    tokens: Tokens,
    accounts: Accounts,
    principal: CurrentPrincipal,
) -> MessageResponse:
    if principal.session_id:
        await tokens.revoke_session(session, uuid.UUID(principal.session_id), reason="logout")
    await accounts.record_audit(session, user_id=uuid.UUID(principal.user_id), action="logout")
    clear_auth_cookies(response)
    return MessageResponse(message="Signed out.")


@router.post(
    "/logout/all",
    response_model=MessageResponse,
    summary="Sign out of every device",
)
async def logout_all(
    response: Response,
    session: DbSession,
    tokens: Tokens,
    accounts: Accounts,
    user: AuthedUser,
) -> MessageResponse:
    await tokens.revoke_all_sessions(session, user.id, reason="logout_all")
    await accounts.record_audit(session, user_id=user.id, action="logout.all")
    clear_auth_cookies(response)
    return MessageResponse(message="Signed out of every device.")


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@router.post(
    "/verify-email",
    response_model=MessageResponse,
    summary="Confirm an email address",
)
async def verify_email(
    payload: VerifyEmailRequest,
    session: DbSession,
    accounts: Accounts,
    ctx: Ctx,
) -> MessageResponse:
    user = await accounts.verify_email(session, payload.token)
    if ctx.publisher is not None:
        await ctx.publisher.publish(
            EventType.USER_VERIFIED, {"user_id": str(user.id), "email": user.email}
        )
    return MessageResponse(message="Your email address is confirmed.")


@router.post(
    "/verify-email/resend",
    response_model=MessageResponse,
    summary="Resend the verification email",
    dependencies=[Depends(rate_limit("password_reset"))],
)
async def resend_verification(
    payload: ResendVerificationRequest,
    session: DbSession,
    accounts: Accounts,
    ctx: Ctx,
) -> MessageResponse:
    user = await accounts.get_by_email(session, payload.email)
    # Same response either way — this endpoint must not confirm an address exists.
    if user is not None and not user.is_email_verified:
        raw = await accounts.issue_verification_token(session, user, email=user.email)
        if ctx.publisher is not None:
            await ctx.publisher.publish(
                EventType.USER_REGISTERED,
                {
                    "user_id": str(user.id),
                    "email": user.email,
                    "full_name": user.full_name,
                    "verification_token": raw,
                    "resend": True,
                },
            )
    return MessageResponse(message="If that address needs confirming, we've sent a new link.")


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    summary="Request a password reset link",
    description=(
        "Always responds the same way, whether or not the address has an account. "
        "Anything else would let an attacker enumerate registered addresses."
    ),
    dependencies=[Depends(rate_limit("password_reset"))],
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    session: DbSession,
    accounts: Accounts,
    ctx: Ctx,
    ip: ClientIP,
) -> MessageResponse:
    result = await accounts.request_password_reset(session, email=payload.email, ip_address=ip)
    if result is not None and ctx.publisher is not None:
        user, raw = result
        await ctx.publisher.publish(
            EventType.PASSWORD_RESET_REQUESTED,
            {"user_id": str(user.id), "email": user.email, "reset_token": raw},
        )
    return MessageResponse(
        message="If an account exists for that address, we've sent a reset link."
    )


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    summary="Set a new password using a reset token",
    dependencies=[Depends(rate_limit("password_reset"))],
)
async def reset_password(
    payload: ResetPasswordRequest,
    response: Response,
    session: DbSession,
    accounts: Accounts,
    tokens: Tokens,
) -> MessageResponse:
    user = await accounts.reset_password(
        session, raw_token=payload.token, new_password=payload.new_password
    )
    # The most likely reason for a reset is that the account was compromised, so
    # every existing session dies with the old password.
    await tokens.revoke_all_sessions(session, user.id, reason="password_reset")
    clear_auth_cookies(response)
    return MessageResponse(message="Your password has been changed. Please sign in again.")


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Change your password",
)
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    session: DbSession,
    accounts: Accounts,
    tokens: Tokens,
    user: AuthedUser,
) -> MessageResponse:
    await accounts.change_password(
        session, user, current=payload.current_password, new=payload.new_password
    )
    await tokens.revoke_all_sessions(session, user.id, reason="password_change")
    clear_auth_cookies(response)
    return MessageResponse(message="Your password has been changed. Please sign in again.")


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@router.get("/me", response_model=UserOut, summary="Get the signed-in user")
async def me(user: AuthedUser) -> UserOut:
    return _user_out(user)


@router.patch("/me", response_model=UserOut, summary="Update your profile")
async def update_me(
    payload: UserUpdateRequest,
    session: DbSession,
    user: AuthedUser,
) -> UserOut:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await session.flush()
    return _user_out(user)


@router.get(
    "/permissions",
    summary="List the permissions each role grants",
    description="Reference data for admin UIs building a role picker.",
)
async def list_role_permissions() -> dict[str, list[str]]:
    return {role.value: [str(p) for p in perms] for role, perms in ROLE_PERMISSIONS.items()}

"""Two-factor enrolment and management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from deps import AuthedUser, DbSession, accounts_service, mfa_service
from knowledgeos_core import MessageResponse, UnauthorizedError
from knowledgeos_core.deps import rate_limit
from knowledgeos_core.security import verify_password
from schemas import (
    MfaDisableRequest,
    MfaEnabledResponse,
    MfaEnrollResponse,
    MfaVerifyRequest,
)
from services import AccountService, MfaService

router = APIRouter(prefix="/v1/auth/mfa", tags=["auth", "mfa"])

Mfa = Annotated[MfaService, Depends(mfa_service)]
Accounts = Annotated[AccountService, Depends(accounts_service)]


@router.post(
    "/enroll",
    response_model=MfaEnrollResponse,
    summary="Begin two-factor enrolment",
    description=(
        "Generates a TOTP secret and provisioning URI. The factor is not active until "
        "confirmed with a valid code — otherwise scanning the QR code and stopping "
        "would lock the user out of their own account."
    ),
    dependencies=[Depends(rate_limit("authenticated"))],
)
async def enroll(session: DbSession, user: AuthedUser, mfa: Mfa) -> MfaEnrollResponse:
    secret, uri = await mfa.begin_enrolment(session, user)
    return MfaEnrollResponse(secret=secret, provisioning_uri=uri)


@router.post(
    "/confirm",
    response_model=MfaEnabledResponse,
    summary="Confirm enrolment and activate two-factor",
    description="Returns single-use recovery codes. They are shown once and stored hashed.",
    dependencies=[Depends(rate_limit("login"))],
)
async def confirm(
    payload: MfaVerifyRequest,
    session: DbSession,
    user: AuthedUser,
    mfa: Mfa,
    accounts: Accounts,
) -> MfaEnabledResponse:
    codes = await mfa.confirm_enrolment(session, user, payload.code)
    await accounts.record_audit(session, user_id=user.id, action="mfa.enabled")
    return MfaEnabledResponse(enabled=True, recovery_codes=codes)


@router.post(
    "/disable",
    response_model=MessageResponse,
    summary="Turn off two-factor authentication",
    description="Requires the account password: removing a security control is itself privileged.",
    dependencies=[Depends(rate_limit("login"))],
)
async def disable(
    payload: MfaDisableRequest,
    session: DbSession,
    user: AuthedUser,
    mfa: Mfa,
    accounts: Accounts,
) -> MessageResponse:
    if not user.password_hash or not verify_password(payload.password, user.password_hash):
        await accounts.record_audit(
            session, user_id=user.id, action="mfa.disable", status="failure"
        )
        raise UnauthorizedError("The password is incorrect.")
    await mfa.disable(session, user)
    await accounts.record_audit(session, user_id=user.id, action="mfa.disabled")
    return MessageResponse(message="Two-factor authentication is off.")


@router.post(
    "/recovery-codes",
    response_model=MfaEnabledResponse,
    summary="Regenerate recovery codes",
    description="Invalidates every existing code and returns a fresh set, shown once.",
    dependencies=[Depends(rate_limit("login"))],
)
async def regenerate_codes(
    session: DbSession, user: AuthedUser, mfa: Mfa, accounts: Accounts
) -> MfaEnabledResponse:
    if not user.mfa_enabled:
        raise UnauthorizedError("Enable two-factor authentication first.")
    codes = await mfa.regenerate_recovery_codes(session, user)
    await accounts.record_audit(session, user_id=user.id, action="mfa.recovery_regenerated")
    return MfaEnabledResponse(enabled=True, recovery_codes=codes)


@router.get(
    "/status",
    summary="Two-factor status",
)
async def status(session: DbSession, user: AuthedUser, mfa: Mfa) -> dict[str, object]:
    return {
        "enabled": user.mfa_enabled,
        "recovery_codes_remaining": await mfa.count_unused_recovery_codes(session, user.id),
    }

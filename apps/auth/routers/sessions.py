"""Active session listing and revocation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from deps import AuthedUser, CurrentPrincipal, DbSession, accounts_service, token_service
from knowledgeos_core import ListResponse, MessageResponse, NotFoundError
from models import Session as SessionModel
from schemas import SessionOut
from services import AccountService, TokenService

router = APIRouter(prefix="/v1/auth/sessions", tags=["auth", "sessions"])

Tokens = Annotated[TokenService, Depends(token_service)]
Accounts = Annotated[AccountService, Depends(accounts_service)]


@router.get(
    "",
    response_model=ListResponse[SessionOut],
    summary="List your active sessions",
    description="One entry per signed-in device. The current session is flagged.",
)
async def list_sessions(
    session: DbSession, user: AuthedUser, principal: CurrentPrincipal
) -> ListResponse[SessionOut]:
    rows = (
        (
            await session.execute(
                select(SessionModel)
                .where(
                    SessionModel.user_id == user.id,
                    SessionModel.revoked_at.is_(None),
                    SessionModel.expires_at > datetime.now(UTC),
                )
                .order_by(SessionModel.last_seen_at.desc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    current = principal.session_id
    items = [
        SessionOut(
            id=row.id,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
            device_label=row.device_label,
            auth_method=row.auth_method,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            current=str(row.id) == current,
        )
        for row in rows
    ]
    return ListResponse[SessionOut](items=items, total=len(items))


@router.delete(
    "/{session_id}",
    response_model=MessageResponse,
    summary="Revoke one session",
    description="Signs out a single device. Use this to eject a session you do not recognise.",
)
async def revoke_session(
    session_id: uuid.UUID,
    session: DbSession,
    user: AuthedUser,
    tokens: Tokens,
    accounts: Accounts,
) -> MessageResponse:
    row = await session.get(SessionModel, session_id)
    # Ownership is checked against the token's subject, never a request field —
    # otherwise anyone could revoke anyone else's session by guessing an id.
    if row is None or row.user_id != user.id:
        raise NotFoundError("Session not found.")

    await tokens.revoke_session(session, session_id, reason="user_revoked")
    await accounts.record_audit(
        session,
        user_id=user.id,
        action="session.revoked",
        meta={"session_id": str(session_id)},
    )
    return MessageResponse(message="That device has been signed out.")

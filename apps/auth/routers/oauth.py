"""OAuth sign-in endpoints for Google and GitHub."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from cookies import set_auth_cookies
from deps import (
    AuthedUser,
    DbSession,
    accounts_service,
    client_ip,
    oauth_service,
    token_service,
    user_agent,
)
from knowledgeos_core import BadRequestError, EventType, ListResponse, get_logger
from knowledgeos_core.deps import Ctx, rate_limit
from models import OAuthAccount
from schemas import OAuthAccountOut, OAuthStartResponse
from services import AccountService, TokenService
from services.oauth import OAuthService
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/auth/oauth", tags=["auth", "oauth"])

OAuth = Annotated[OAuthService, Depends(oauth_service)]
Tokens = Annotated[TokenService, Depends(token_service)]
Accounts = Annotated[AccountService, Depends(accounts_service)]
ClientIP = Annotated[str | None, Depends(client_ip)]
UserAgent = Annotated[str | None, Depends(user_agent)]

#: PKCE verifiers are handed to the browser inside the signed state, so nothing is
#: stored server-side and any replica can complete a flow another one started.
_PKCE_COOKIE = "kos_oauth_v"


@router.get(
    "/providers",
    summary="List configured OAuth providers",
    description="Lets the frontend render only the sign-in buttons that will actually work.",
)
async def providers(oauth: OAuth) -> dict[str, list[str]]:
    return {"providers": oauth.available}


@router.get(
    "/{provider}/start",
    response_model=OAuthStartResponse,
    summary="Begin an OAuth sign-in",
    description=(
        "Returns the provider's authorization URL. The `state` parameter is HMAC-signed "
        "and expiring, and carries the PKCE verifier, so a forged or replayed callback "
        "is rejected without any server-side session."
    ),
    dependencies=[Depends(rate_limit("login"))],
)
async def start(
    provider: str,
    oauth: OAuth,
    next_url: Annotated[str | None, Query(alias="next", max_length=500)] = None,
) -> OAuthStartResponse:
    impl = oauth.get_provider(provider)
    verifier, challenge = oauth.create_pkce_pair()
    state = oauth.sign_state(provider=provider, verifier=verifier, next_url=next_url)
    url = impl.authorization_url(
        state=state, challenge=challenge, redirect_uri=oauth.redirect_uri(provider)
    )
    return OAuthStartResponse(authorization_url=url, state=state)


@router.get(
    "/{provider}/callback",
    summary="OAuth callback",
    description=(
        "Handles the provider redirect, exchanges the code, resolves or creates the "
        "local account, and redirects to the frontend with a short-lived handoff."
    ),
    response_class=RedirectResponse,
)
async def callback(
    provider: str,
    request: Request,
    session: DbSession,
    oauth: OAuth,
    tokens: Tokens,
    accounts: Accounts,
    ctx: Ctx,
    ip: ClientIP,
    agent: UserAgent,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    frontend = str(settings.frontend_url).rstrip("/")

    if error:
        # The user declined, or the provider refused. Not an exception — send them
        # back to the UI with something it can render.
        logger.info("auth.oauth_declined", provider=provider, reason=error[:100])
        return RedirectResponse(
            f"{frontend}/login?{urlencode({'error': 'oauth_declined'})}", status_code=303
        )

    if not code or not state:
        raise BadRequestError("The OAuth callback is missing required parameters.")

    impl = oauth.get_provider(provider)
    verifier, next_url = oauth.read_state(state, provider=provider)

    profile = await impl.exchange(
        code=code, verifier=verifier, redirect_uri=oauth.redirect_uri(provider)
    )
    user = await oauth.resolve_user(session, profile, ip_address=ip)

    issued = await tokens.start_session(
        session,
        user,
        ip_address=ip,
        user_agent=agent,
        device_label=None,
        auth_method=f"oauth:{provider}",
    )
    await accounts.record_audit(
        session,
        user_id=user.id,
        action="login.oauth",
        ip_address=ip,
        user_agent=agent,
        meta={"provider": provider},
    )

    if ctx.publisher is not None:
        await ctx.publisher.publish(
            EventType.USER_LOGGED_IN,
            {"user_id": str(user.id), "provider": provider, "ip": ip},
        )

    # Only relative paths are honoured, so a crafted `next` cannot turn the callback
    # into an open redirect to an attacker's site.
    target = f"{frontend}/auth/callback"
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        target = f"{frontend}{next_url}"

    response = RedirectResponse(target, status_code=303)
    set_auth_cookies(response, issued)
    return response


@router.get(
    "/accounts",
    response_model=ListResponse[OAuthAccountOut],
    summary="List linked OAuth accounts",
)
async def linked_accounts(session: DbSession, user: AuthedUser) -> ListResponse[OAuthAccountOut]:
    rows = (
        (await session.execute(select(OAuthAccount).where(OAuthAccount.user_id == user.id)))
        .scalars()
        .all()
    )
    items = [
        OAuthAccountOut(
            provider=row.provider,
            email=row.email,
            linked_at=row.linked_at,
            last_login_at=row.last_login_at,
        )
        for row in rows
    ]
    return ListResponse[OAuthAccountOut](items=items, total=len(items))


@router.delete(
    "/accounts/{provider}",
    summary="Unlink an OAuth account",
    description=(
        "Refused when it would leave the account with no way to sign in — an "
        "OAuth-only user must set a password before unlinking their last provider."
    ),
)
async def unlink(
    provider: str,
    session: DbSession,
    user: AuthedUser,
    accounts: Accounts,
) -> dict[str, str]:
    rows = list(
        (await session.execute(select(OAuthAccount).where(OAuthAccount.user_id == user.id)))
        .scalars()
        .all()
    )
    target = next((r for r in rows if r.provider == provider), None)
    if target is None:
        raise BadRequestError(f"No {provider} account is linked.")
    if len(rows) == 1 and not user.password_hash:
        raise BadRequestError(
            "Set a password before unlinking your only sign-in method.",
            code="last_login_method",
        )

    await session.delete(target)
    await accounts.record_audit(
        session, user_id=user.id, action="oauth.unlinked", meta={"provider": provider}
    )
    return {"message": f"{provider.title()} has been unlinked."}

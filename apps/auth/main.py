"""Authentication service entrypoint.

Owns identity for the whole platform: it is the only process holding a token-signing
private key, and the only one that can mint an access token (ADR 0003).
"""

from __future__ import annotations

from knowledgeos_core import Components, create_app, get_logger, run
from knowledgeos_core.app import AppContext
from routers import (
    admin_users_router,
    audit_router,
    auth_router,
    internal_router,
    mfa_router,
    oauth_router,
    sessions_router,
)
from services import AccountService, DirectoryService, KeyRing, MfaService, TokenService
from services.oauth import OAuthService
from settings import settings

logger = get_logger(__name__)


async def _bootstrap(ctx: AppContext) -> None:
    """Build the key ring and domain services before the first request."""
    keyring = KeyRing(settings)
    keyring.load()

    accounts = AccountService(settings)
    ctx.extras.update(
        {
            "keyring": keyring,
            "tokens": TokenService(settings, keyring),
            "accounts": accounts,
            "directory": DirectoryService(settings),
            "mfa": MfaService(settings),
            "oauth": OAuthService(settings, accounts),
        }
    )

    # Record the public halves for rotation auditing. Best-effort: JWKS is served
    # from the in-memory ring, so a database hiccup here must not stop the service
    # from starting and authenticating users.
    if ctx.database is not None:
        try:
            async with ctx.database.sessionmaker() as session:
                await keyring.sync_to_database(session)
                await session.commit()
        except Exception as exc:
            logger.warning("auth.signing_key_sync_failed", error=str(exc))

    oauth_providers = ctx.extras["oauth"].available
    logger.info(
        "auth.ready",
        active_kid=keyring.active.kid,
        oauth_providers=oauth_providers or ["none configured"],
    )


app = create_app(
    settings=settings,
    components=Components(
        database=True,
        redis=True,
        # Auth verifies its own tokens against the local key ring rather than
        # fetching JWKS from itself over HTTP — see deps.get_principal.
        auth=False,
        events=True,
    ),
    routers=[
        auth_router,
        admin_users_router,
        audit_router,
        mfa_router,
        sessions_router,
        oauth_router,
        internal_router,
    ],
    on_startup=[_bootstrap],
    description=(
        "Identity for KnowledgeOS: registration, sign-in, OAuth, two-factor, "
        "sessions, RBAC and JWKS publication."
    ),
)


if __name__ == "__main__":
    run("main:app", settings)

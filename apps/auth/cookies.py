"""Auth cookie handling.

The refresh token lives in an ``httpOnly`` cookie so JavaScript — and therefore any
XSS on the frontend — cannot read it. The access token deliberately does *not* get a
cookie: it is returned in the response body and held in memory by the client, where
it dies with the tab.

The CSRF cookie is the readable half of a double-submit pair. A cross-site page can
cause the browser to send the refresh cookie, but cannot read the CSRF cookie to
populate the matching header, so it cannot forge a refresh.
"""

from __future__ import annotations

from fastapi import Response

from services.tokens import IssuedTokens
from settings import settings


def set_auth_cookies(response: Response, issued: IssuedTokens) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=issued.refresh_token,
        max_age=settings.refresh_token_ttl,
        # Unreachable from JavaScript — the single most important flag here.
        httponly=True,
        # Never sent over plain HTTP. Disabled only for local development.
        secure=settings.cookie_secure,
        # Strict: the cookie is not attached to any cross-site request at all, which
        # is appropriate because refreshing is never a cross-site navigation.
        samesite="strict",
        # Scoped to the auth routes, so it is not attached to requests that have no
        # use for it (and cannot leak through an unrelated endpoint).
        path=settings.cookie_path,
        domain=settings.cookie_domain,
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=issued.csrf_token,
        max_age=settings.refresh_token_ttl,
        # Readable by design: the client must echo it back in X-CSRF-Token.
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        path=settings.cookie_path,
        domain=settings.cookie_domain,
    )


def clear_auth_cookies(response: Response) -> None:
    """Remove both cookies.

    Path and domain must match the values used when setting them, or the browser
    keeps the original cookie and the user stays signed in.
    """
    for name in (settings.refresh_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            key=name,
            path=settings.cookie_path,
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite="strict",
        )

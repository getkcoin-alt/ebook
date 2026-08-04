"""Test helpers shared by every service's suite.

Keeps service test suites focused on behaviour rather than on re-inventing token
minting and dependency overrides.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from .deps import get_current_principal, get_optional_principal, require_internal_caller
from .security import Principal


def make_principal(
    *,
    user_id: str | None = None,
    email: str = "test@knowledgeos.dev",
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> Principal:
    """Build a Principal without signing a real token."""
    return Principal(
        user_id=user_id or str(uuid.uuid4()),
        email=email,
        roles=roles or ["user"],
        permissions=permissions or ["books:read"],
        session_id=str(uuid.uuid4()),
        token_id=str(uuid.uuid4()),
    )


def authenticate_as(app: FastAPI, principal: Principal | None) -> None:
    """Override auth dependencies for a test.

    Overriding the dependency rather than minting a real RS256 token keeps unit
    tests free of key generation and of a live auth service. Token verification
    itself is covered by dedicated tests in the auth service.
    """
    if principal is None:
        app.dependency_overrides.pop(get_current_principal, None)
        app.dependency_overrides[get_optional_principal] = lambda: None
        return
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_optional_principal] = lambda: principal


def allow_internal_calls(app: FastAPI, caller: str = "test-service") -> None:
    """Bypass HMAC verification so internal endpoints can be tested directly."""
    app.dependency_overrides[require_internal_caller] = lambda: caller


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the app in-process (no network, no port binding)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as http_client:
        yield http_client


def assert_error(response: Any, *, status: int, code: str | None = None) -> dict[str, Any]:
    """Assert the platform error envelope and return the error body."""
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert "error" in body, f"response is not a platform error envelope: {body}"
    if code is not None:
        assert body["error"]["code"] == code, (
            f"expected code {code!r}, got {body['error']['code']!r}"
        )
    return body["error"]  # type: ignore[no-any-return]

"""Gateway test fixtures.

Upstreams are stub ASGI apps mounted behind an httpx MockTransport, so the whole
suite runs with no network, no Redis server and no real services.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Set before `main` is imported. `main.app` is built at module scope, so
# Components() is fixed the moment the module loads — a fixture override would be
# too late to stop the event consumer from starting.
#
# The consumer blocks on XREADGROUP, which fakeredis does not implement in its
# blocking form, so leaving it on hangs the whole suite. Cache invalidation is
# exercised directly against ResponseCache in test_cache.py instead.
os.environ.setdefault("EVENTS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")


_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "jwks_url": "http://auth.test/.well-known/jwks.json",
}


@pytest.fixture
def settings(monkeypatch):
    """Reconfigure the real settings singleton in place.

    The modules under test import `settings` at module scope, so handing the app a
    different instance would leave them reading production defaults.
    """
    import settings as settings_module

    for field, value in _TEST_OVERRIDES.items():
        monkeypatch.setattr(settings_module.settings, field, value, raising=True)
    return settings_module.settings


@pytest.fixture
def upstream_log() -> list[dict[str, Any]]:
    """Every request the gateway forwarded, for asserting on proxy behaviour."""
    return []


@pytest.fixture
def upstream_handler(upstream_log):
    """Default stub upstream. Echoes what it received."""

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_log.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query.decode()),
                "headers": dict(request.headers),
            }
        )
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "openapi": "3.1.0",
                    "paths": {f"/stub{request.url.host}": {}},
                    "components": {"schemas": {}},
                },
            )
        return httpx.Response(
            200,
            json={"upstream": request.url.host, "path": request.url.path},
            headers={"Content-Type": "application/json"},
        )

    return handler


@pytest_asyncio.fixture
async def app(settings, upstream_handler, monkeypatch):
    """The real gateway, with upstreams and Redis replaced by in-process fakes."""
    import fakeredis.aioredis
    import proxy as proxy_module

    import main as main_module
    from knowledgeos_core import redis as core_redis

    # Redirect the connection factory instead of replacing the client afterwards:
    # the gateway declares Components(redis=True), so the lifespan builds its Redis
    # client, health probe, rate limiter and event consumer before any fixture could
    # swap them. Patching here means the real lifespan runs against a fake server.
    _fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: _fake)

    # Every upstream client is backed by MockTransport instead of a socket.
    original_get = proxy_module.UpstreamPool.get

    def patched_get(self, upstream: str) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
        if upstream not in self._clients:
            self._clients[upstream] = httpx.AsyncClient(
                base_url=f"http://{upstream}.test",
                transport=httpx.MockTransport(upstream_handler),
            )
        return self._clients[upstream]

    monkeypatch.setattr(proxy_module.UpstreamPool, "get", patched_get)

    application = main_module.app

    async with LifespanManager(application):
        # Each test gets a clean cache and rate-limit budget; main.app is a
        # module-level singleton shared across the whole suite.
        await _fake.flushall()
        yield application

    # main.app is a module-level singleton reused across tests; clear the pools so
    # one test's clients cannot leak into the next.
    application.state.ctx.extras.get("pool")._clients.clear()
    monkeypatch.setattr(proxy_module.UpstreamPool, "get", original_get)


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://gateway.test",
    ) as http_client:
        yield http_client


@pytest.fixture
def as_user(app):
    """Authenticate subsequent requests as a given principal.

    Patches the gateway's token verification rather than minting a real RS256 token
    — token verification itself is covered by the auth service's suite.
    """
    import main as main_module
    from knowledgeos_core.security import Principal

    def _apply(user_id: str = "user-1", roles: list[str] | None = None) -> Principal:
        principal = Principal(
            user_id=user_id,
            email=f"{user_id}@knowledgeos.dev",
            roles=roles or ["user"],
            permissions=["books:read"],
            session_id="session-1",
        )

        async def fake_authenticate(ctx, request):  # type: ignore[no-untyped-def]
            header = request.headers.get("authorization", "")
            if not header.lower().startswith("bearer "):
                return None
            return principal

        main_module._authenticate = fake_authenticate
        return principal

    yield _apply

    import importlib

    importlib.reload(main_module)

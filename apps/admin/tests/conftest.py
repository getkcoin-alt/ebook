"""Admin service test fixtures.

In-memory SQLite, fakeredis, and a stub sibling registry that records every call —
including the **headers**, because forwarding the operator's token is the security
property this service turns on.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("EVENTS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import models  # noqa: E402, F401  - registers tables on Base.metadata
from knowledgeos_core import Base  # noqa: E402

SCHEMA = "admin"

ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TOKEN = "operator-access-token"

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "panel_timeout": 1.0,
    "dashboard_timeout": 3.0,
    "dashboard_cache_ttl": 60,
    "health_cache_ttl": 15,
}


@pytest.fixture
def settings(monkeypatch):
    import settings as settings_module

    for field, value in _TEST_OVERRIDES.items():
        monkeypatch.setattr(settings_module.settings, field, value, raising=True)
    return settings_module.settings


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _attach(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_connection.cursor()
        cur.execute(f"ATTACH DATABASE ':memory:' AS {SCHEMA}")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as db_session:
        yield db_session


# ---------------------------------------------------------------------------
# Stub sibling services
# ---------------------------------------------------------------------------


class RecordingClient:
    def __init__(self, parent: StubRegistry, name: str) -> None:
        self._parent = parent
        self.name = name

    async def request(  # type: ignore[no-untyped-def]
        self,
        method,
        path,
        *,
        params=None,
        json=None,
        headers=None,
        timeout=None,  # noqa: ASYNC109 - mirrors the real client's signature
        **kwargs,
    ):
        delay = self._parent.delay.get(self.name, 0.0)
        if delay:
            # Honour the caller's budget the way httpx does, rather than sleeping
            # through it. A stub that ignores the timeout it was handed makes every
            # timeout test pass by never timing out.
            if timeout is not None and delay > timeout:
                await asyncio.sleep(timeout)
                self._parent.calls.append(
                    {"service": self.name, "method": method, "path": path, "headers": {}}
                )
                raise httpx.ReadTimeout("no response")
            await asyncio.sleep(delay)

        self._parent.calls.append(
            {
                "service": self.name,
                "method": method,
                "path": path,
                "params": params,
                "headers": headers or {},
                "timeout": timeout,
            }
        )

        behaviour = self._parent.behaviour.get(self.name, "ok")
        if behaviour == "unreachable":
            from knowledgeos_core import UpstreamError

            raise UpstreamError(
                f"Could not reach the {self.name} service at db-prod-3.internal:5432."
            )

        status = self._parent.status.get(self.name, 200)
        body = self._parent.body.get(
            self.name,
            {"status": "up", "version": "0.1.0", "total": 12, "gross_minor": 4500},
        )
        return httpx.Response(
            status_code=status,
            json=body,
            request=httpx.Request(method, f"http://{self.name}{path}"),
        )

    async def aclose(self) -> None:
        return None


class StubRegistry:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.behaviour: dict[str, str] = {}
        self.status: dict[str, int] = {}
        self.body: dict[str, dict] = {}
        #: Per-service response delay, for exercising the panel and page budgets.
        self.delay: dict[str, float] = {}

    def get(self, name: str) -> RecordingClient:
        return RecordingClient(self, name)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def registry() -> StubRegistry:
    return StubRegistry()


@pytest_asyncio.fixture
async def redis():
    """A real client backed by fakeredis, so caching is the production path."""
    import fakeredis.aioredis

    import settings as settings_module
    from knowledgeos_core import redis as core_redis

    fake = fakeredis.aioredis.FakeRedis()
    original = core_redis.aioredis.from_url
    core_redis.aioredis.from_url = lambda *a, **k: fake
    try:
        client = core_redis.RedisClient(settings_module.settings)
        await fake.flushall()
        yield client
    finally:
        core_redis.aioredis.from_url = original


@pytest.fixture
def aggregator(settings, registry, redis):
    from services import Aggregator

    return Aggregator(settings, registry, redis)


@pytest.fixture
def flags(settings, redis):
    from services import FlagService

    return FlagService(settings, redis)


@pytest.fixture
def services(settings, aggregator, flags):
    return {"aggregator": aggregator, "flags": flags}


@pytest_asyncio.fixture
async def app(engine, settings, services, monkeypatch):
    import fakeredis.aioredis

    from knowledgeos_core import Components, create_app
    from knowledgeos_core import redis as core_redis
    from knowledgeos_core.app import AppContext
    from knowledgeos_core.db import Database
    from routers import dashboard_router, flags_router, internal_router

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        components=Components(redis=True, auth=False),
        routers=[dashboard_router, flags_router, internal_router],
        on_startup=[_bootstrap],
    )

    class _TestDatabase(Database):
        def __init__(self, engine) -> None:
            self._settings = settings
            self._engine = engine
            self._sessionmaker = async_sessionmaker(
                bind=engine, expire_on_commit=False, autoflush=False
            )

        async def dispose(self) -> None:
            return

    async with LifespanManager(application):
        application.state.ctx.database = _TestDatabase(engine)
        await fake.flushall()
        yield application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://test",
        # Sent on every request so the token-forwarding path is exercised by default.
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as http_client:
        yield http_client


@pytest.fixture
def as_admin(app):
    from knowledgeos_core.deps import get_current_principal, get_optional_principal
    from knowledgeos_core.security import Principal

    def _apply(permissions=None):  # type: ignore[no-untyped-def]
        principal = Principal(
            user_id=str(ADMIN_ID),
            email="ops@knowledgeos.dev",
            roles=["admin"],
            permissions=permissions or ["analytics:read", "settings:write"],
            session_id=str(uuid.uuid4()),
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_optional_principal] = lambda: principal
        return principal

    yield _apply
    app.dependency_overrides.clear()


@pytest.fixture
def as_internal(app):
    from knowledgeos_core.deps import require_internal_caller

    def _apply(name: str = "books") -> str:
        app.dependency_overrides[require_internal_caller] = lambda: name
        return name

    yield _apply
    app.dependency_overrides.clear()

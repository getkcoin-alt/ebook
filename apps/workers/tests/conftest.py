"""Workers service test fixtures.

In-memory SQLite, fakeredis, and a **recording sibling service** that returns
programmable responses. No broker, no real HTTP.

The stub sits at the `ServiceRegistry` boundary — the same seam the production runner
calls through — so the runner's own logic (locking, timeout classification, status
handling, history recording) is the real code under test. Stubbing the runner itself
would leave nothing worth testing in this service.
"""

from __future__ import annotations

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

SCHEMA = "workers"

ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "run_retention_days": 30,
    "unhealthy_after_failures": 3,
}


@pytest.fixture
def settings(monkeypatch):
    import settings as settings_module

    for field, value in _TEST_OVERRIDES.items():
        monkeypatch.setattr(settings_module.settings, field, value, raising=True)
    monkeypatch.setattr(settings_module.settings, "disabled_jobs", [], raising=True)
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
# Recording sibling services
# ---------------------------------------------------------------------------


class RecordingClient:
    """One sibling service, programmable per test."""

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
        timeout=None,  # noqa: ASYNC109 - mirrors the real client's signature
        **kwargs,
    ):
        if self._parent.delay:
            # A real call suspends. Without a suspension here the whole run —
            # acquire lock, call, release lock — completes without ever yielding to
            # the event loop, so concurrent callers never actually contend and a
            # single-flight test passes for the wrong reason.
            import asyncio

            await asyncio.sleep(self._parent.delay)
        self._parent.calls.append(
            {
                "service": self.name,
                "method": method,
                "path": path,
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )
        behaviour = self._parent.behaviour.get(self.name, "ok")
        if behaviour == "timeout":
            raise httpx.ReadTimeout("no response")
        if behaviour == "unreachable":
            from knowledgeos_core import UpstreamError

            raise UpstreamError(f"Could not reach the {self.name} service.")

        status = self._parent.status.get(self.name, 200)
        return httpx.Response(
            status_code=status,
            json=self._parent.body.get(self.name, {"message": "ok", "count": 3}),
            request=httpx.Request(method, f"http://{self.name}{path}"),
        )

    async def aclose(self) -> None:
        return None


class StubRegistry:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        #: service -> "ok" | "timeout" | "unreachable"
        self.behaviour: dict[str, str] = {}
        self.status: dict[str, int] = {}
        self.body: dict[str, dict] = {}
        #: Seconds each call suspends for. Set when a test needs real overlap.
        self.delay: float = 0.0

    def get(self, name: str) -> RecordingClient:
        return RecordingClient(self, name)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def registry() -> StubRegistry:
    return StubRegistry()


@pytest_asyncio.fixture
async def redis():
    """A real Redis client backed by fakeredis, so the lock is the production one."""
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
def runner(settings, registry, redis):
    from services import JobRunner

    return JobRunner(settings, registry, redis, worker_id="test-worker")


@pytest.fixture
def history(settings):
    from services import HistoryService

    return HistoryService(settings)


@pytest.fixture
def services(settings, runner, history):
    return {"runner": runner, "history": history}


@pytest_asyncio.fixture
async def app(engine, settings, services, monkeypatch):
    import fakeredis.aioredis

    from knowledgeos_core import Components, create_app
    from knowledgeos_core import redis as core_redis
    from knowledgeos_core.app import AppContext
    from knowledgeos_core.db import Database
    from routers import admin_router, internal_router

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        components=Components(redis=True, auth=False),
        routers=[admin_router, internal_router],
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

    def _apply(name: str = "admin") -> str:
        app.dependency_overrides[require_internal_caller] = lambda: name
        return name

    yield _apply
    app.dependency_overrides.clear()


def make_run(job_name: str, outcome, *, minutes_ago: int = 0, duration_ms: int = 100):  # type: ignore[no-untyped-def]
    """A history row, positioned in time."""
    from datetime import UTC, datetime, timedelta

    from models import TaskRun

    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return TaskRun(
        job_name=job_name,
        outcome=outcome,
        status_code=200 if str(outcome) == "succeeded" else None,
        duration_ms=duration_ms,
        detail={},
        started_at=started,
        finished_at=started,
    )

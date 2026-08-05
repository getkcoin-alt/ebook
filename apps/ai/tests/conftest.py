"""AI service test fixtures. In-memory SQLite plus fakeredis — no model calls.

The provider is a **recording** stub implementing the real protocol, with a
programmable response and a programmable failure mode. That is what makes the parts
worth testing testable: the budget ceiling, the cache, failover between providers,
JSON recovery, and moderation failing closed. None of those involve a real model, and
all of them are where the money and the safety live.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

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

SCHEMA = "ai"

READER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
BOOK_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "anthropic_api_key": "sk-ant-test",
    "openai_api_key": "sk-openai-test",
    "cache_enabled": True,
    "cost_tracking_enabled": True,
    "moderation_enabled": True,
    "moderation_fail_closed": True,
    "daily_cost_limit_usd": 10.0,
    "user_daily_cost_limit_usd": 1.0,
}


@pytest.fixture
def settings(monkeypatch):
    """Reconfigure the real settings singleton in place."""
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
# Recording provider
# ---------------------------------------------------------------------------


class RecordingProvider:
    """A model provider that records instead of calling anything.

    Implements the real protocol, so the registry's failover logic and the
    generator's ledger path are the production ones.
    """

    def __init__(self, name: str = "anthropic", model: str = "claude-sonnet-4-5") -> None:
        self.name = name
        self._model = model
        self.calls: list[dict] = []
        #: What to return. Set per test.
        self.response = "A generated description."
        #: None | "retryable" | "permanent". Retryable failover is the interesting one.
        self.fail_mode: str | None = None
        self.input_tokens = 1000
        self.output_tokens = 500
        self.usage_missing = False

    @property
    def configured(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, *, system, user, max_tokens, temperature=0.7):  # type: ignore[no-untyped-def]
        from knowledgeos_core import ServiceUnavailableError, UpstreamError
        from services.providers import Completion

        self.calls.append(
            {"system": system, "user": user, "max_tokens": max_tokens, "temperature": temperature}
        )
        if self.fail_mode == "retryable":
            raise ServiceUnavailableError("rate limited", details={"provider": self.name})
        if self.fail_mode == "permanent":
            raise UpstreamError("bad request", details={"provider": self.name})

        return Completion(
            content=self.response,
            provider=self.name,
            model=self._model,
            input_tokens=0 if self.usage_missing else self.input_tokens,
            output_tokens=0 if self.usage_missing else self.output_tokens,
            usage_missing=self.usage_missing,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def provider() -> RecordingProvider:
    return RecordingProvider()


@pytest.fixture
def secondary() -> RecordingProvider:
    return RecordingProvider(name="openai", model="gpt-4o-mini")


@pytest.fixture
def providers(settings, provider, secondary):
    from services import ProviderRegistry

    registry = ProviderRegistry(settings)
    registry._providers = {"anthropic": provider, "openai": secondary}
    return registry


class StubServices:
    """Stands in for the sibling-service registry used by chat grounding."""

    def __init__(self) -> None:
        self.books: list[dict] = []
        self.owned: set[str] = set()
        self.search_fails = False

    def get(self, name: str):  # type: ignore[no-untyped-def]
        return _StubClient(self, name)


class _StubClient:
    def __init__(self, parent: StubServices, name: str) -> None:
        self._parent = parent
        self._name = name

    async def get_json(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
        if self._name == "search":
            if self._parent.search_fails:
                raise RuntimeError("search is down")
            return {"hits": self._parent.books}
        if path.startswith("/internal/books/"):
            book_id = path.rsplit("/", 1)[-1]
            return next((b for b in self._parent.books if str(b.get("id")) == book_id), None)
        return None

    async def post_json(self, path: str, json=None, **kwargs):  # type: ignore[no-untyped-def]
        if path == "/internal/entitlements/check":
            requested = {str(b) for b in (json or {}).get("book_ids", [])}
            return {"owned_book_ids": sorted(requested & self._parent.owned)}
        return None


@pytest.fixture
def sibling_services() -> StubServices:
    return StubServices()


@pytest.fixture
def services(settings, providers, sibling_services):
    from services import BudgetService, ChatService, Generator, ModerationService

    budget = BudgetService(settings)
    generator = Generator(settings, providers, budget, None)
    return {
        "providers": providers,
        "budget": budget,
        "generator": generator,
        "moderation": ModerationService(settings, generator),
        "chat": ChatService(settings, generator, providers, budget, sibling_services),
    }


@pytest_asyncio.fixture
async def app(engine, settings, services, monkeypatch):
    import fakeredis.aioredis

    from knowledgeos_core import Components, create_app
    from knowledgeos_core import redis as core_redis
    from knowledgeos_core.app import AppContext
    from knowledgeos_core.db import Database
    from routers import assistant_router, internal_router

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        components=Components(redis=True, auth=False),
        routers=[assistant_router, internal_router],
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
def as_user(app):
    from knowledgeos_core.deps import get_current_principal, get_optional_principal
    from knowledgeos_core.security import Principal

    def _apply(user_id: uuid.UUID = READER_ID, roles=None, permissions=None):  # type: ignore[no-untyped-def]
        principal = Principal(
            user_id=str(user_id),
            email=f"{user_id}@knowledgeos.dev",
            roles=roles or ["user"],
            permissions=permissions or ["ai:use"],
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

    def _apply(name: str = "automation") -> str:
        app.dependency_overrides[require_internal_caller] = lambda: name
        return name

    yield _apply
    app.dependency_overrides.clear()


def book_context(**overrides):  # type: ignore[no-untyped-def]
    from schemas import BookContext

    defaults = {
        "title": "Deep Work",
        "authors": ["Cal Newport"],
        "categories": ["productivity"],
        "language": "en",
        "excerpt": "Focus is the new IQ.",
    }
    defaults.update(overrides)
    return BookContext(**defaults)

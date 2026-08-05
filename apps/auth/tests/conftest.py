"""Test fixtures for the auth service.

The suite runs entirely on in-memory SQLite — no Postgres, no Redis, no network. A
test suite that needs infrastructure is a test suite that gets skipped.

SQLite has no schemas, so the `auth` schema is provided by ATTACHing a second
in-memory database under that name. The models keep their explicit `schema="auth"`
and the same definitions run on both backends.
"""

from __future__ import annotations

import sys
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

import models  # noqa: E402, F401  - registers the tables on Base.metadata
from knowledgeos_core import Base  # noqa: E402


@pytest_asyncio.fixture
async def engine():
    """In-memory SQLite engine with the `auth` schema attached."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        # StaticPool keeps every connection pointed at the *same* in-memory
        # database; the default pool would give each connection its own empty one.
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _attach_schema(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS auth")
        # Enforce foreign keys — SQLite ignores them unless asked, which would let a
        # test pass against a constraint violation that PostgreSQL would reject.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session


#: Overrides applied to the module-level settings singleton for every test.
_TEST_OVERRIDES = {
    "environment": "local",
    "database_url": "sqlite+aiosqlite:///:memory:",
    "internal_api_secret": "test-internal-secret",
    # The test client speaks plain HTTP, and a Secure cookie is never sent over it.
    "cookie_secure": False,
    # Keep the test output readable; logging itself is covered in core-py.
    "log_level": "CRITICAL",
    # Keep the anti-enumeration delay out of the test runtime; the padding behaviour
    # itself is asserted directly in test_security.py.
    "credential_response_floor_ms": 0,
}


@pytest.fixture
def settings(monkeypatch):
    """Reconfigure the real settings singleton in place.

    `cookies.py` and the routers import `settings` at module scope, so handing the
    app a *different* Settings instance would leave them reading production defaults
    — which is exactly how a test can pass while the code under test is misconfigured.
    Patching the singleton keeps one source of truth.
    """
    import settings as settings_module

    for field, value in _TEST_OVERRIDES.items():
        monkeypatch.setattr(settings_module.settings, field, value, raising=True)
    return settings_module.settings


@pytest.fixture
def keyring(settings):
    from services import KeyRing

    ring = KeyRing(settings)
    ring.load()  # generates an ephemeral keypair outside production
    return ring


@pytest.fixture
def services(settings, keyring):
    from services import AccountService, DirectoryService, MfaService, TokenService
    from services.oauth import OAuthService

    accounts = AccountService(settings)
    return {
        "keyring": keyring,
        "directory": DirectoryService(settings),
        "tokens": TokenService(settings, keyring),
        "accounts": accounts,
        "mfa": MfaService(settings),
        "oauth": OAuthService(settings, accounts),
    }


@pytest_asyncio.fixture
async def app(engine, settings, services):
    """The real application, wired to the SQLite engine."""
    from knowledgeos_core import Components, create_app
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

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        # No database/redis component: the lifespan would build its own engine.
        # A test double is injected below instead.
        components=Components(),
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
    )

    # Core's real Database, rebound to the test engine. Subclassing rather than
    # faking means the tests exercise the same session/commit/rollback path that
    # runs in production, including the rollback-on-exception behaviour.
    from knowledgeos_core.db import Database

    class _TestDatabase(Database):
        def __init__(self, engine) -> None:
            self._settings = settings
            self._engine = engine
            self._sessionmaker = async_sessionmaker(
                bind=engine, expire_on_commit=False, autoflush=False
            )

        async def dispose(self) -> None:
            # The engine fixture owns disposal; doing it here would tear the
            # database down before the test's assertions run.
            return

    async with LifespanManager(application):
        ctx = application.state.ctx
        ctx.database = _TestDatabase(engine)

        # fakeredis rather than a stub: the rate limiter's Lua script, the
        # idempotency store and the denylist all run their real code, so the tests
        # cover them instead of routing around them.
        import fakeredis.aioredis

        from knowledgeos_core.idempotency import IdempotencyStore
        from knowledgeos_core.ratelimit import RateLimiter
        from knowledgeos_core.redis import RedisClient

        fake = fakeredis.aioredis.FakeRedis()
        redis_client = RedisClient.__new__(RedisClient)
        redis_client._settings = settings
        redis_client._prefix = f"kos:{settings.service_name}"
        redis_client._client = fake
        redis_client._release_lock = fake.register_script("return redis.call('DEL', KEYS[1])")

        ctx.redis = redis_client
        ctx.limiter = RateLimiter(fake, service_name=settings.service_name)
        ctx.idempotency = IdempotencyStore(fake, service_name=settings.service_name)

        yield application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def user(session, settings):
    """A verified, active account with a known password."""
    from services import AccountService

    accounts = AccountService(settings)
    created, _ = await accounts.register(
        session,
        email="reader@knowledgeos.dev",
        password="correct horse battery staple",
        full_name="Test Reader",
        locale="en",
        ip_address="127.0.0.1",
    )
    from datetime import UTC, datetime

    created.email_verified_at = datetime.now(UTC)
    await session.commit()
    return created


PASSWORD = "correct horse battery staple"

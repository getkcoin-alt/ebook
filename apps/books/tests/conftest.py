"""Book service test fixtures. In-memory SQLite plus fakeredis — no infrastructure."""

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

# Set before `main` is imported — Components() is fixed at module load, so a fixture
# override would be too late to stop the event consumer starting. It blocks on
# XREADGROUP, which fakeredis does not implement; the entitlement handler is tested
# directly instead.
os.environ.setdefault("EVENTS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import models  # noqa: E402, F401  - registers tables on Base.metadata
from knowledgeos_core import Base  # noqa: E402

SCHEMA = "books"

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "cache_enabled": False,  # exercised separately; keeps assertions about DB reads honest
}


@pytest.fixture
def settings(monkeypatch):
    """Reconfigure the real settings singleton in place.

    Modules import `settings` at module scope, so a separate instance would leave
    them on production defaults.
    """
    import settings as settings_module

    for field, value in _TEST_OVERRIDES.items():
        monkeypatch.setattr(settings_module.settings, field, value, raising=True)
    return settings_module.settings


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,  # one shared in-memory database, not one per connection
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _attach(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_connection.cursor()
        cur.execute(f"ATTACH DATABASE ':memory:' AS {SCHEMA}")
        # SQLite ignores foreign keys unless asked, which would let a test pass
        # against a constraint violation PostgreSQL would reject.
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


@pytest.fixture
def services(settings):
    from services import (
        CatalogueCache,
        CatalogueService,
        EntitlementService,
        ListService,
        ReadingService,
        ReviewService,
        TaxonomyService,
    )

    cache = CatalogueCache(None, settings)  # cache disabled in tests
    entitlements = EntitlementService(settings)
    return {
        "cache": cache,
        "catalogue": CatalogueService(settings, cache),
        "taxonomy": TaxonomyService(settings, cache),
        "reviews": ReviewService(settings),
        "reading": ReadingService(settings),
        "lists": ListService(settings),
        "entitlements": entitlements,
    }


@pytest_asyncio.fixture
async def app(engine, settings, services, monkeypatch):
    import fakeredis.aioredis

    from knowledgeos_core import Components, create_app
    from knowledgeos_core import redis as core_redis
    from knowledgeos_core.app import AppContext
    from knowledgeos_core.db import Database
    from routers import (
        admin_router,
        authors_router,
        catalogue_router,
        categories_router,
        entitlements_router,
        internal_router,
        library_router,
        lists_router,
        moderation_router,
        publishers_router,
        reading_router,
        reviews_router,
    )

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        # No database component: the lifespan would build its own engine against
        # DATABASE_URL. The test engine is injected below instead.
        components=Components(redis=True, storage=False, auth=False),
        routers=[
            catalogue_router,
            library_router,
            reviews_router,
            reading_router,
            lists_router,
            authors_router,
            publishers_router,
            categories_router,
            admin_router,
            moderation_router,
            entitlements_router,
            internal_router,
        ],
        on_startup=[_bootstrap],
    )

    class _TestDatabase(Database):
        """Core's real Database rebound to the test engine, so the session/commit/
        rollback path under test is the same one that runs in production."""

        def __init__(self, engine) -> None:
            self._settings = settings
            self._engine = engine
            self._sessionmaker = async_sessionmaker(
                bind=engine, expire_on_commit=False, autoflush=False
            )

        async def dispose(self) -> None:
            return  # the engine fixture owns disposal

    class _StubStorage:
        """Stands in for S3. Download tests assert that entitlement gating ran and a
        URL was minted — not that boto3 works."""

        async def signed_download_url(self, key, *, expires_in=300, download_filename=None):
            return f"https://storage.test/{key}?sig=stub"

        async def create_upload_target(self, **kwargs):
            from knowledgeos_core.storage import UploadTarget

            return UploadTarget(
                url="https://storage.test/upload",
                key=f"{kwargs['category']}/{kwargs['filename']}",
                fields={},
                expires_in=900,
                max_bytes=kwargs.get("max_bytes", 0),
            )

        async def healthcheck(self):
            return {"status": "up"}

    async with LifespanManager(application):
        application.state.ctx.database = _TestDatabase(engine)
        application.state.ctx.storage = _StubStorage()
        await fake.flushall()
        yield application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

READER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@pytest.fixture
def as_user(app):
    """Authenticate requests as a principal, without minting a real token.

    Token verification itself is covered by the auth service's suite.
    """
    from knowledgeos_core.deps import get_current_principal, get_optional_principal
    from knowledgeos_core.security import Principal

    def _apply(
        user_id: uuid.UUID = READER_ID,
        roles: list[str] | None = None,
        permissions: list[str] | None = None,
    ) -> Principal:
        principal = Principal(
            user_id=str(user_id),
            email=f"{user_id}@knowledgeos.dev",
            roles=roles or ["user"],
            permissions=permissions or ["books:read"],
            session_id=str(uuid.uuid4()),
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_optional_principal] = lambda: principal
        return principal

    yield _apply
    app.dependency_overrides.clear()


@pytest.fixture
def as_admin(as_user):
    def _apply():
        return as_user(
            ADMIN_ID,
            roles=["admin"],
            permissions=[
                "books:read",
                "books:write",
                "books:publish",
                "books:delete",
                "reviews:moderate",
            ],
        )

    return _apply


@pytest.fixture
def as_internal(app):
    from knowledgeos_core.deps import require_internal_caller

    def _apply(name: str = "automation") -> str:
        app.dependency_overrides[require_internal_caller] = lambda: name
        return name

    yield _apply
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def book_factory(session):
    """Create published books directly, bypassing the admin API."""
    from datetime import UTC, datetime

    from knowledgeos_core.schemas import BookStatus
    from models import Book

    counter = {"n": 0}

    async def _create(**overrides) -> Book:  # type: ignore[no-untyped-def]
        counter["n"] += 1
        n = counter["n"]
        defaults = {
            "slug": f"book-{n}",
            "title": f"Test Book {n}",
            "language": "en",
            "price_minor": 49900,
            "currency": "INR",
            "status": BookStatus.PUBLISHED.value,
            "published_at": datetime.now(UTC),
            "pdf_key": f"private/book/{n}/book.pdf",
        }
        defaults.update(overrides)
        book = Book(**defaults)
        session.add(book)
        await session.commit()
        return book

    return _create

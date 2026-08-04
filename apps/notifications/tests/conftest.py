"""Notification service test fixtures. In-memory SQLite plus fakeredis.

The email provider is a **recording** stub rather than a mock: it implements the
real `ChannelProvider` protocol and can be told to fail transiently or permanently,
which is what makes the retry and suppression paths testable. Everything upstream of
the network call — preferences, suppression, rendering, delivery records, backoff —
is the production code path.
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

# Set before `main` is imported — Components() is fixed at module load, so a fixture
# override would be too late to stop the event consumer starting.
os.environ.setdefault("EVENTS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import models  # noqa: E402, F401  - registers tables on Base.metadata
from knowledgeos_core import Base, NotificationChannel  # noqa: E402

SCHEMA = "notifications"

READER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

UNSUBSCRIBE_SECRET = "test-unsubscribe-secret"

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "unsubscribe_secret": UNSUBSCRIBE_SECRET,
    "email_provider": "console",
    "sending_enabled": True,
    "suppression_enforced": True,
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
    """A channel provider that records instead of sending.

    Implements the real protocol, so the dispatcher treats it exactly as it treats
    Resend or Twilio — including the retryable/permanent distinction that drives
    backoff and suppression.
    """

    def __init__(self, channel: NotificationChannel = NotificationChannel.EMAIL) -> None:
        self.channel = channel
        self.name = "recording"
        self.sent: list = []
        #: Set to make the next send fail. "permanent" suppresses; "transient" retries.
        self.fail_mode: str | None = None
        self.message_id_counter = 0

    @property
    def configured(self) -> bool:
        return True

    async def send(self, message):  # type: ignore[no-untyped-def]
        from services.channels import SendResult

        self.sent.append(message)
        if self.fail_mode == "permanent":
            return SendResult(
                success=False,
                provider=self.name,
                error="mailbox does not exist",
                permanent=True,
            )
        if self.fail_mode == "transient":
            return SendResult(success=False, provider=self.name, error="temporary failure")

        self.message_id_counter += 1
        return SendResult(
            success=True, provider=self.name, provider_message_id=f"rec-{self.message_id_counter}"
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def email_provider() -> RecordingProvider:
    return RecordingProvider(NotificationChannel.EMAIL)


@pytest.fixture
def push_provider() -> RecordingProvider:
    return RecordingProvider(NotificationChannel.PUSH)


@pytest.fixture
def channels(settings, email_provider, push_provider):
    from services import ChannelRegistry

    registry = ChannelRegistry(settings)
    registry._providers[NotificationChannel.EMAIL] = email_provider
    registry._providers[NotificationChannel.PUSH] = push_provider
    return registry


@pytest.fixture
def services(settings, channels):
    from services import Dispatcher, InboxService, PreferenceService, TemplateService

    templates = TemplateService(settings)
    preferences = PreferenceService(settings)
    return {
        "channels": channels,
        "templates": templates,
        "preferences": preferences,
        "dispatcher": Dispatcher(settings, templates, preferences, channels),
        "inbox": InboxService(settings),
    }


@pytest_asyncio.fixture
async def app(engine, settings, services, monkeypatch):
    import fakeredis.aioredis

    from knowledgeos_core import Components, create_app
    from knowledgeos_core import redis as core_redis
    from knowledgeos_core.app import AppContext
    from knowledgeos_core.db import Database
    from routers import admin_router, inbox_router, internal_router

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        components=Components(redis=True, auth=False),
        routers=[inbox_router, admin_router, internal_router],
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
    # raise_app_exceptions=True so a genuine bug shows its traceback rather than an
    # opaque 500 body. Handled AppErrors still become proper responses.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://test",
    ) as http_client:
        yield http_client


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def as_user(app):
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
        return as_user(ADMIN_ID, roles=["admin"], permissions=["settings:write", "analytics:read"])

    return _apply


@pytest.fixture
def as_internal(app):
    from knowledgeos_core.deps import require_internal_caller

    def _apply(name: str = "payment") -> str:
        app.dependency_overrides[require_internal_caller] = lambda: name
        return name

    yield _apply
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def template_factory(session):
    """Create templates directly, bypassing the admin API."""
    from models import Template

    async def _create(
        key: str = "order.receipt",
        channel: NotificationChannel = NotificationChannel.EMAIL,
        *,
        category: str = "order.receipt",
        subject: str | None = "Your receipt for {{order_number}}",
        body_text: str = "Thanks {{name}}. Order {{order_number}} is confirmed.",
        body_html: str | None = None,
        required_variables: list[str] | None = None,
        locale: str = "en",
        is_active: bool = True,
    ) -> Template:
        template = Template(
            key=key,
            channel=channel,
            locale=locale,
            category=category,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            required_variables=required_variables or [],
            is_active=is_active,
        )
        session.add(template)
        await session.commit()
        await session.refresh(template)
        return template

    return _create


@pytest_asyncio.fixture
async def both_channel_templates(template_factory):
    """A template pair for in-app and email under one key — the common shape."""
    await template_factory(channel=NotificationChannel.IN_APP)
    await template_factory(channel=NotificationChannel.EMAIL)

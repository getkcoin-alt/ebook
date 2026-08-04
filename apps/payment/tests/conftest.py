"""Payment service test fixtures. In-memory SQLite plus fakeredis — no infrastructure.

The catalogue is stubbed rather than mocked at the HTTP layer: pricing needs *some*
source of book prices, and a stub that returns known amounts makes the money
assertions readable. What is deliberately **not** stubbed is any of the settlement
logic, the coupon arithmetic or the signature verification — those are the parts
worth testing.
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
# override would be too late to stop an event consumer starting.
os.environ.setdefault("EVENTS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import models  # noqa: E402, F401  - registers tables on Base.metadata
from knowledgeos_core import Base  # noqa: E402

SCHEMA = "payment"

READER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

BOOK_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
BOOK_B = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
BOOK_FREE = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000003")
BOOK_DRAFT = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000004")
CATEGORY_FICTION = uuid.UUID("cccccccc-0000-0000-0000-000000000001")

RAZORPAY_SECRET = "rzp-test-secret"
RAZORPAY_WEBHOOK_SECRET = "rzp-webhook-secret"
STRIPE_WEBHOOK_SECRET = "whsec_test"

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "razorpay_key_id": "rzp_test_key",
    "razorpay_key_secret": RAZORPAY_SECRET,
    "razorpay_webhook_secret": RAZORPAY_WEBHOOK_SECRET,
    "stripe_secret_key": "sk_test_key",
    "stripe_publishable_key": "pk_test_key",
    "stripe_webhook_secret": STRIPE_WEBHOOK_SECRET,
}


@pytest.fixture
def settings(monkeypatch):
    """Reconfigure the real settings singleton in place.

    Modules import `settings` at module scope, so constructing a separate instance
    would leave them on production defaults — which is how a test suite ends up
    passing while the deployed service is misconfigured.
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


# ---------------------------------------------------------------------------
# Catalogue stub
# ---------------------------------------------------------------------------


class StubCatalogue:
    """Stands in for the books service.

    Records what it was asked for, so a test can assert that ownership was checked
    at all — the check being silently skipped is the failure mode that lets a
    customer be charged twice for one file.
    """

    def __init__(self) -> None:
        from services.catalogue import CatalogueBook

        self.books = {
            BOOK_A: CatalogueBook(
                id=BOOK_A,
                slug="book-a",
                title="Book A",
                price_minor=49900,
                currency="INR",
                status="published",
                category_ids=(CATEGORY_FICTION,),
            ),
            BOOK_B: CatalogueBook(
                id=BOOK_B,
                slug="book-b",
                title="Book B",
                price_minor=29900,
                currency="INR",
                status="published",
            ),
            BOOK_FREE: CatalogueBook(
                id=BOOK_FREE,
                slug="book-free",
                title="Free Book",
                price_minor=0,
                currency="INR",
                status="published",
            ),
            BOOK_DRAFT: CatalogueBook(
                id=BOOK_DRAFT,
                slug="book-draft",
                title="Draft Book",
                price_minor=19900,
                currency="INR",
                status="draft",
            ),
        }
        self.owned: dict[uuid.UUID, set[uuid.UUID]] = {}
        self.ownership_calls = 0

    async def fetch(self, book_ids):  # type: ignore[no-untyped-def]
        from knowledgeos_core import NotFoundError

        missing = [b for b in book_ids if b not in self.books]
        if missing:
            raise NotFoundError(
                "One or more books in this order no longer exist.",
                details={"book_ids": [str(b) for b in missing]},
            )
        return {b: self.books[b] for b in dict.fromkeys(book_ids)}

    async def require_purchasable(self, book_ids):  # type: ignore[no-untyped-def]
        from knowledgeos_core import BadRequestError

        books = await self.fetch(book_ids)
        unavailable = [b for b in books.values() if not b.is_purchasable]
        if unavailable:
            raise BadRequestError(
                "One or more books in this order are not available for purchase.",
                code="book_not_purchasable",
                details={"book_ids": [str(b.id) for b in unavailable]},
            )
        return books

    async def owned_book_ids(self, user_id, book_ids):  # type: ignore[no-untyped-def]
        self.ownership_calls += 1
        return {b for b in book_ids if b in self.owned.get(user_id, set())}


class StubGateway:
    """A gateway that records calls instead of making them.

    Signature verification is *not* stubbed away in the tests that care about it —
    those use the real Razorpay and Stripe implementations directly.
    """

    def __init__(self, name) -> None:  # type: ignore[no-untyped-def]
        self.name = name
        self.created: list[dict] = []
        self.refunds: list[dict] = []
        self.refund_status = "processed"

    @property
    def configured(self) -> bool:
        return True

    async def create_order(self, **kwargs):  # type: ignore[no-untyped-def]
        from services.providers.base import ProviderOrder

        self.created.append(kwargs)
        return ProviderOrder(
            provider_order_id=f"prov_{kwargs['order_number']}",
            checkout_url=f"https://gateway.test/{kwargs['order_number']}",
            client_secret="cs_test_secret",
        )

    async def refund(self, **kwargs):  # type: ignore[no-untyped-def]
        from services.providers.base import ProviderRefund

        self.refunds.append(kwargs)
        return ProviderRefund(
            provider_refund_id=f"rfnd_{len(self.refunds)}", status=self.refund_status
        )

    def verify_webhook(self, *, body, headers) -> bool:  # type: ignore[no-untyped-def]
        return headers.get("x-test-signature") == "valid"

    def verify_checkout(self, *, provider_order_id, provider_payment_id, signature) -> bool:  # type: ignore[no-untyped-def]
        return signature == "valid"

    def parse_webhook(self, payload, headers):  # type: ignore[no-untyped-def]
        from services.providers.base import NormalisedEvent

        return NormalisedEvent(
            event_id=str(payload.get("id", "")),
            event_type=str(payload.get("event", "payment.captured")),
            category=str(payload.get("category", "payment")),
            provider_order_id=payload.get("provider_order_id"),
            provider_payment_id=payload.get("provider_payment_id"),
            provider_refund_id=payload.get("provider_refund_id"),
            amount_minor=payload.get("amount_minor"),
            currency=payload.get("currency"),
            raw=payload,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture
def catalogue() -> StubCatalogue:
    return StubCatalogue()


@pytest.fixture
def gateways(settings, monkeypatch):
    """A registry whose Razorpay entry is a recording stub."""
    from knowledgeos_core import PaymentProvider
    from services.providers import GatewayRegistry

    registry = GatewayRegistry(settings)
    stub = StubGateway(PaymentProvider.RAZORPAY)
    registry._gateways[PaymentProvider.RAZORPAY] = stub
    registry.stub = stub  # type: ignore[attr-defined]
    return registry


@pytest.fixture
def services(settings, catalogue, gateways):
    from services import (
        AffiliateService,
        CouponService,
        InvoiceService,
        OrderService,
        PaymentService,
        PricingService,
        RefundService,
        ReportService,
        SubscriptionService,
        WebhookService,
    )

    coupons = CouponService(settings)
    invoices = InvoiceService(settings)
    affiliates = AffiliateService(settings)
    orders = OrderService(settings, coupons, gateways)
    payments = PaymentService(settings, orders, invoices, affiliates)
    refunds = RefundService(settings, orders, gateways, affiliates)
    subscriptions = SubscriptionService(settings)
    return {
        "gateways": gateways,
        "catalogue": catalogue,
        "coupons": coupons,
        "pricing": PricingService(settings, catalogue, coupons),
        "orders": orders,
        "payments": payments,
        "refunds": refunds,
        "invoices": invoices,
        "subscriptions": subscriptions,
        "affiliates": affiliates,
        "reports": ReportService(settings.default_currency),
        "webhooks": WebhookService(
            settings, gateways, orders, payments, refunds, subscriptions, affiliates
        ),
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
        affiliates_router,
        checkout_router,
        internal_router,
        invoices_router,
        subscriptions_router,
        webhooks_router,
    )

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        # No database component: the lifespan would build its own engine against
        # DATABASE_URL. The test engine is injected below instead.
        components=Components(redis=True, auth=False),
        routers=[
            checkout_router,
            invoices_router,
            subscriptions_router,
            affiliates_router,
            webhooks_router,
            admin_router,
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

    async with LifespanManager(application):
        application.state.ctx.database = _TestDatabase(engine)
        await fake.flushall()
        yield application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=True: the platform's handlers already turn every AppError
    # into a proper response, so anything that still escapes is a genuine bug. Letting
    # it propagate shows the traceback instead of an opaque 500 body.
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
    """Authenticate as a principal without minting a real token.

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
                "orders:read",
                "orders:refund",
                "settings:write",
                "analytics:read",
            ],
        )

    return _apply


@pytest.fixture
def as_internal(app):
    from knowledgeos_core.deps import require_internal_caller

    def _apply(name: str = "admin") -> str:
        app.dependency_overrides[require_internal_caller] = lambda: name
        return name

    yield _apply
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def coupon_factory(session, settings):
    from schemas import CouponType

    counter = {"n": 0}

    async def _create(**overrides):  # type: ignore[no-untyped-def]
        from models import Coupon

        counter["n"] += 1
        defaults = {
            "code": f"SAVE{counter['n']}",
            "coupon_type": CouponType.PERCENT,
            "value": 10,
            "min_order_minor": 0,
            "per_user_limit": 1,
            "is_active": True,
            "applicable_book_ids": [],
            "applicable_category_ids": [],
        }
        defaults.update(overrides)
        coupon = Coupon(**defaults)
        session.add(coupon)
        await session.commit()
        await session.refresh(coupon)
        return coupon

    return _create


@pytest_asyncio.fixture
async def order_factory(session, services, catalogue):
    """Create an order through the real pricing and order services."""
    from schemas import OrderCreate, OrderItemIn

    async def _create(
        *,
        user_id: uuid.UUID = READER_ID,
        book_ids: list[uuid.UUID] | None = None,
        coupon_code: str | None = None,
        affiliate_code: str | None = None,
        idempotency_key: str | None = None,
        attach_gateway: bool = True,
    ):
        items = [OrderItemIn(book_id=b) for b in (book_ids or [BOOK_A])]
        payload = OrderCreate(items=items, coupon_code=coupon_code, affiliate_code=affiliate_code)
        cart = await services["pricing"].quote(
            session,
            items=items,
            user_id=user_id,
            currency="INR",
            coupon_code=coupon_code,
        )
        order = await services["orders"].create(
            session,
            user_id=user_id,
            payload=payload,
            cart=cart,
            idempotency_key=idempotency_key,
            billing_email="buyer@knowledgeos.dev",
        )
        if attach_gateway:
            await services["orders"].attach_gateway(
                session, order, requested_provider=None, return_url=None
            )
        await session.commit()
        await session.refresh(order, ["items"])
        return order

    return _create

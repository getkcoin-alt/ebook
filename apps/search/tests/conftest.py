"""Search service test fixtures. In-memory SQLite, fakeredis, and a fake engine.

Meilisearch is replaced at the **HTTP transport**, not at the client: `MeiliClient`
builds every request and interprets every response exactly as it does in production,
and a small in-memory engine answers them. That is deliberate — the interesting bugs
in this service live in the request bodies (filter syntax, sort expressions, hybrid
blocks) and in the response mapping, and stubbing `MeiliClient` would test neither.

The fake engine is not a search engine. It matches substrings and applies the
filters it is given, which is enough to assert that the right query was *built* and
the right documents came back. Relevance ranking is Meilisearch's job, not ours.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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

# Set before `main` is imported — Components() is fixed at module load, so a fixture
# override would be too late to stop the event consumer starting. It blocks on
# XREADGROUP, which fakeredis does not implement; the indexer's handler is tested
# directly instead.
os.environ.setdefault("EVENTS_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import models  # noqa: E402, F401  - registers tables on Base.metadata
from knowledgeos_core import Base  # noqa: E402

SCHEMA = "search"

READER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "meilisearch_url": "http://meili.test",
    "meilisearch_master_key": "test-key",
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
# Fake Meilisearch
# ---------------------------------------------------------------------------

_FILTER_EQ = re.compile(r'^\s*([\w.]+)\s*=\s*"?(.*?)"?\s*$')
_FILTER_NE = re.compile(r'^\s*([\w.]+)\s*!=\s*"?(.*?)"?\s*$')
_FILTER_CMP = re.compile(r"^\s*([\w.]+)\s*(>=|<=)\s*([\d.]+)\s*$")


class FakeMeili:
    """An in-memory stand-in that speaks Meilisearch's HTTP shapes.

    It records every request body, so a test can assert on the *query that was
    built* — which is where the filter-escaping and sort-expression bugs live —
    as well as on the documents that came back.
    """

    def __init__(self) -> None:
        self.indexes: dict[str, dict[str, dict[str, Any]]] = {}
        self.settings: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.healthy = True
        #: Set to make the next search fail, for degradation tests.
        self.fail_next = False

    # ---- helpers ----------------------------------------------------------

    def add(self, index: str, *documents: dict[str, Any]) -> None:
        store = self.indexes.setdefault(index, {})
        for document in documents:
            store[str(document["id"])] = document

    def bodies(self, path_fragment: str) -> list[Any]:
        return [body for _method, path, body in self.requests if path_fragment in path]

    # ---- matching ---------------------------------------------------------

    @staticmethod
    def _matches_clause(document: dict[str, Any], clause: str) -> bool:
        if match := _FILTER_CMP.match(clause):
            field, operator, raw = match.groups()
            value = document.get(field)
            if value is None:
                return False
            return float(value) >= float(raw) if operator == ">=" else float(value) <= float(raw)
        if match := _FILTER_NE.match(clause):
            field, expected = match.groups()
            return str(document.get(field)) != expected
        if match := _FILTER_EQ.match(clause):
            field, expected = match.groups()
            value = document.get(field)
            if isinstance(value, list):
                return expected in [str(item) for item in value]
            if isinstance(value, bool):
                return str(value).lower() == expected.lower()
            return str(value) == expected
        return True

    def _matches(self, document: dict[str, Any], filters: Any) -> bool:
        """Outer elements AND, a nested list ORs — Meilisearch's array form."""
        if not filters:
            return True
        for clause in filters:
            if isinstance(clause, list):
                if not any(self._matches_clause(document, item) for item in clause):
                    return False
            elif not self._matches_clause(document, clause):
                return False
        return True

    def _search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        documents = list(self.indexes.get(index, {}).values())
        query = str(body.get("q", "")).strip().lower()
        if query:
            documents = [
                document
                for document in documents
                if query in " ".join(str(v) for v in document.values()).lower()
            ]
        documents = [d for d in documents if self._matches(d, body.get("filter"))]

        for expression in reversed(body.get("sort", []) or []):
            field, _, direction = str(expression).partition(":")
            documents.sort(
                key=lambda d, f=field: (d.get(f) is None, d.get(f) or 0),
                reverse=direction == "desc",
            )

        total = len(documents)
        offset = int(body.get("offset", 0))
        limit = int(body.get("limit", 20))
        page = documents[offset : offset + limit]

        facets: dict[str, dict[str, int]] = {}
        for attribute in body.get("facets", []) or []:
            counts: dict[str, int] = {}
            for document in documents:
                value = document.get(attribute)
                for item in value if isinstance(value, list) else [value]:
                    if item is not None:
                        counts[str(item)] = counts.get(str(item), 0) + 1
            facets[attribute] = counts

        return {
            "hits": page,
            "estimatedTotalHits": total,
            "processingTimeMs": 1,
            "facetDistribution": facets,
        }

    # ---- transport --------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        import orjson

        path = request.url.path
        body = orjson.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))

        if not self.healthy:
            return httpx.Response(503, json={"message": "unavailable"})

        if path == "/health":
            return httpx.Response(200, json={"status": "available"})

        if path == "/multi-search":
            results = []
            for query in (body or {}).get("queries", []):
                index = query["indexUid"]
                results.append({"indexUid": index, **self._search(index, query)})
            return httpx.Response(200, json={"results": results})

        if match := re.match(r"^/indexes/([^/]+)/search$", path):
            if self.fail_next:
                self.fail_next = False
                return httpx.Response(500, json={"message": "engine error"})
            return httpx.Response(200, json=self._search(match.group(1), body or {}))

        if match := re.match(r"^/indexes/([^/]+)/documents$", path):
            index = match.group(1)
            if request.method == "POST":
                self.add(index, *(body or []))
                return httpx.Response(202, json={"taskUid": 1, "status": "enqueued"})
            if request.method == "DELETE":
                store = self.indexes.setdefault(index, {})
                for document_id in body or []:
                    store.pop(str(document_id), None)
                return httpx.Response(202, json={"taskUid": 1})

        if match := re.match(r"^/indexes/([^/]+)/documents/delete-batch$", path):
            store = self.indexes.setdefault(match.group(1), {})
            for document_id in body or []:
                store.pop(str(document_id), None)
            return httpx.Response(202, json={"taskUid": 1})

        if match := re.match(r"^/indexes/([^/]+)/documents/([^/]+)$", path):
            index, document_id = match.groups()
            document = self.indexes.get(index, {}).get(document_id)
            if document is None:
                return httpx.Response(404, json={"code": "document_not_found"})
            return httpx.Response(200, json=document)

        if match := re.match(r"^/indexes/([^/]+)/settings$", path):
            self.settings[match.group(1)] = body or {}
            return httpx.Response(202, json={"taskUid": 1})

        if match := re.match(r"^/indexes/([^/]+)/stats$", path):
            index = match.group(1)
            return httpx.Response(200, json={"numberOfDocuments": len(self.indexes.get(index, {}))})

        if match := re.match(r"^/indexes/([^/]+)$", path):
            return httpx.Response(200, json={"uid": match.group(1)})

        if path == "/indexes" and request.method == "POST":
            self.indexes.setdefault((body or {}).get("uid", ""), {})
            return httpx.Response(202, json={"taskUid": 1})

        return httpx.Response(404, json={"code": "not_found", "message": path})


@pytest.fixture
def meili_engine() -> FakeMeili:
    return FakeMeili()


@pytest.fixture
def meili(settings, meili_engine):
    """A real MeiliClient wired to the fake transport."""
    from services.meili import MeiliClient

    # MeiliClient takes an injected client, so the transport is the only seam
    # needed — every request body and response mapping under test is the real one.
    return MeiliClient(
        settings,
        httpx.AsyncClient(
            base_url=settings.meilisearch_url,
            transport=httpx.MockTransport(meili_engine.handler),
        ),
    )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class StubCatalogue:
    """Stands in for the books service during reconciliation."""

    def __init__(self) -> None:
        self.books: list[dict[str, Any]] = []
        self.authors: list[dict[str, Any]] = []
        self.categories: list[dict[str, Any]] = []

    async def get_book(self, book_id: str):  # type: ignore[no-untyped-def]
        return next((b for b in self.books if str(b.get("id")) == str(book_id)), None)

    # These yield *pages*, matching CatalogueGateway: the indexer batches its
    # writes to match the page it was handed, which is what keeps memory flat
    # across a catalogue of any size.
    async def iter_books(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.books:
            yield list(self.books)

    async def iter_authors(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.authors:
            yield list(self.authors)

    async def iter_categories(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.categories:
            yield list(self.categories)


@pytest.fixture
def catalogue() -> StubCatalogue:
    return StubCatalogue()


@pytest.fixture
def services(settings, meili, catalogue, engine):
    from services import (
        AnalyticsService,
        Indexer,
        QueryService,
    )

    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    return {
        "meili": meili,
        "catalogue": catalogue,
        "indexer": Indexer(settings, meili, sessionmaker, catalogue=catalogue),
        "queries": QueryService(settings, meili),
        "trending": None,  # replaced in the app fixture, which owns the fake Redis
        "analytics": AnalyticsService(settings),
        "_sessionmaker": sessionmaker,
    }


@pytest_asyncio.fixture
async def app(engine, settings, services, monkeypatch):
    import fakeredis.aioredis

    from knowledgeos_core import Components, create_app
    from knowledgeos_core import redis as core_redis
    from knowledgeos_core.app import AppContext
    from knowledgeos_core.db import Database
    from routers import admin_router, internal_router, search_router
    from services import TrendingService

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        extras = {k: v for k, v in services.items() if not k.startswith("_")}
        extras["trending"] = TrendingService(fake, settings)
        ctx.extras.update(extras)

    application = create_app(
        settings=settings,
        # No database component: the lifespan would build its own engine against
        # DATABASE_URL. The test engine is injected below instead.
        components=Components(redis=True, auth=False),
        routers=[search_router, admin_router, internal_router],
        on_startup=[_bootstrap],
    )

    class _TestDatabase(Database):
        """Core's real Database rebound to the test engine."""

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
        return as_user(ADMIN_ID, roles=["admin"], permissions=["analytics:read", "settings:write"])

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


def book_document(**overrides: Any) -> dict[str, Any]:
    """A document already in index shape, for query-path tests."""
    document = {
        "id": str(uuid.uuid4()),
        "slug": "test-book",
        "title": "Test Book",
        "subtitle": None,
        "description": "A book about testing.",
        "status": "published",
        "language": "en",
        "price_minor": 49900,
        "is_free": False,
        "price_bucket": "199_499",
        "rating_average": 4.0,
        "rating_count": 10,
        "popularity": 1.0,
        "category_slugs": ["fiction"],
        "category_names": ["Fiction"],
        "author_slugs": ["jane-doe"],
        "author_names": ["Jane Doe"],
        "formats": ["pdf"],
        "tags": ["testing"],
        "publisher": "KnowledgeOS Press",
        "published_at_ts": 1_700_000_000,
        "thumbnail_url": None,
    }
    document.update(overrides)
    return document


def catalogue_book(**overrides: Any) -> dict[str, Any]:
    """A record in *books service* shape, for indexing tests."""
    book = {
        "id": str(uuid.uuid4()),
        "slug": "catalogue-book",
        "title": "Catalogue Book",
        "description": "From the books service.",
        "status": "published",
        "language": "en",
        "price_minor": 29900,
        "effective_price_minor": 29900,
        "currency": "INR",
        "rating_average": 3.5,
        "rating_count": 4,
        "view_count": 100,
        "available_formats": ["pdf", "epub"],
        "ai_tags": ["nonfiction"],
        "authors": [{"id": str(uuid.uuid4()), "slug": "jane-doe", "name": "Jane Doe"}],
        "categories": [{"id": str(uuid.uuid4()), "slug": "fiction", "name": "Fiction"}],
        "published_at": "2026-01-01T00:00:00Z",
    }
    book.update(overrides)
    return book

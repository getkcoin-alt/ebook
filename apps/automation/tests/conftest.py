"""Automation service test fixtures.

In-memory SQLite, fakeredis, an in-memory object store, and stub sibling services.
No broker, no bucket, no model provider.

The important choice here is that the **documents are real**. `make_pdf` builds an
actual PDF with pypdf and `make_epub` builds an actual EPUB zip, so the inspection,
compression and sample stages run their production code paths against files that a
parser genuinely has to parse. A fixture that hands the pipeline a dict called
"pdf" would pass while proving nothing about the one part of this service that
touches untrusted input.
"""

from __future__ import annotations

import io
import os
import sys
import uuid
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
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

SCHEMA = "automation"

BOOK_ID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
ADMIN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SOURCE_KEY = "private/uploads/deep-work.pdf"

_TEST_OVERRIDES = {
    "environment": "local",
    "internal_api_secret": "test-internal-secret",
    "log_level": "CRITICAL",
    "inline_execution": True,
    "ai_enabled": True,
    "auto_publish": False,
    "max_attempts": 3,
    "retry_base_seconds": 1,
    "watermark_enabled": True,
    "excerpt_chars": 4_000,
    "excerpt_max_pages": 5,
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
# Real documents
# ---------------------------------------------------------------------------


def make_pdf(
    *, pages: int = 12, text: str = "Focus is the new IQ. ", title: str | None = "Deep Work"
) -> bytes:
    """A genuine PDF, with real text on real pages.

    Built by drawing a content stream rather than by stitching bytes together, so
    `extract_text` has something to extract and the page-count and excerpt assertions
    mean what they say.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        body = f"BT /F1 12 Tf 72 720 Td ({text}page {index + 1}) Tj ET".encode("latin-1")
        stream = DecodedStreamObject()
        stream.set_data(body)
        page[NameObject("/Contents")] = writer._add_object(stream)

        font = DictionaryObject()
        font.update(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject()
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        page[NameObject("/MediaBox")] = ArrayObject(
            [NumberObject(0), NumberObject(0), NumberObject(612), NumberObject(792)]
        )

    if title:
        writer.add_metadata({"/Title": title, "/Author": "Cal Newport"})

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_epub(
    *,
    title: str = "Deep Work",
    author: str = "Cal Newport",
    language: str = "en-GB",
    chapters: int = 3,
    entity_bomb: bool = False,
) -> bytes:
    """A genuine EPUB: mimetype, container, OPF and spine documents."""
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>{language}</dc:language>
    <dc:date>2016-01-05</dc:date>
    <dc:identifier id="bid">urn:isbn:9781455586691</dc:identifier>
  </metadata>
  <manifest>
    {"".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(chapters))}
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine>
    {"".join(f'<itemref idref="c{i}"/>' for i in range(chapters))}
  </spine>
</package>"""

    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

    if entity_bomb:
        container = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE container [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # The mimetype entry must be first and stored, per the spec.
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/style.css", "body { font-family: serif; }")
        for index in range(chapters):
            archive.writestr(
                f"OEBPS/c{index}.xhtml",
                f"<html><body><h1>Chapter {index + 1}</h1>"
                f"<p>Deliberate practice &amp; focus, chapter {index + 1}.</p>"
                "<script>ignore me</script></body></html>",
            )
    return buffer.getvalue()


def make_zip_bomb(*, declared: int = 4_000_000_000) -> bytes:
    """A zip whose directory declares an enormous uncompressed size."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")
        archive.writestr("payload.bin", b"\0" * 4096)
    data = bytearray(buffer.getvalue())

    # Rewrite the declared uncompressed size in the central directory so the guard
    # sees what a real bomb declares, without writing 10GB to build the fixture.
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("payload.bin")
    marker = info.file_size.to_bytes(4, "little")
    index = data.find(marker, data.find(b"PK\x01\x02"))
    if index != -1:
        data[index : index + 4] = declared.to_bytes(4, "little")
    return bytes(data)


def make_png(*, width: int = 1200, height: int = 1800, colour=(30, 40, 60)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# In-memory storage
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Stored:
    key: str
    size: int
    etag: str
    content_type: str
    last_modified: datetime


class MemoryStorage:
    """Implements the slice of `ObjectStorage` the pipeline uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.writes: list[str] = []
        self.fail_on: set[str] = set()

    def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None:
        self.objects[key] = data
        self.content_types[key] = content_type

    async def head(self, key: str) -> _Stored:
        from knowledgeos_core import NotFoundError

        if key in self.fail_on:
            raise RuntimeError("storage is unreachable")
        if key not in self.objects:
            raise NotFoundError("The requested file does not exist.")
        return _Stored(
            key=key,
            size=len(self.objects[key]),
            etag="etag",
            content_type=self.content_types.get(key, "application/octet-stream"),
            last_modified=datetime.now(UTC),
        )

    async def get_bytes(self, key: str) -> bytes:
        if key in self.fail_on:
            raise RuntimeError("storage is unreachable")
        return self.objects[key]

    async def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None, cache_seconds: int = 0
    ) -> _Stored:
        if key in self.fail_on:
            raise RuntimeError("storage is unreachable")
        self.objects[key] = data
        self.content_types[key] = content_type or "application/octet-stream"
        self.writes.append(key)
        return _Stored(
            key=key,
            size=len(data),
            etag="etag",
            content_type=self.content_types[key],
            last_modified=datetime.now(UTC),
        )


@pytest.fixture
def storage() -> MemoryStorage:
    store = MemoryStorage()
    store.put(SOURCE_KEY, make_pdf())
    return store


# ---------------------------------------------------------------------------
# Stub sibling services
# ---------------------------------------------------------------------------


class StubClients:
    """Stands in for `PipelineClients`, recording every call.

    Implements the same surface so the stages and the runner exercise their real
    code. What it does not do is speak HTTP, which is where the tests would
    otherwise spend all their time.
    """

    def __init__(self) -> None:
        self.book: dict | None = {
            "id": str(BOOK_ID),
            "title": "Deep Work",
            "slug": "deep-work",
            "language": "en",
            "authors": [{"name": "Cal Newport"}],
            "categories": [{"name": "Productivity"}],
            "description": None,
        }
        self.updates: list[dict] = []
        self.created: list[dict] = []
        self.published: list[str] = []
        self.indexed: list[str] = []
        self.ai_response: object | None = None
        self.ai_available = True
        self.update_fails = False
        self.create_fails = False
        self.available = True

    async def get_book(self, book_id):  # type: ignore[no-untyped-def]
        return self.book

    async def generate(self, *, kinds, context, book_id, force_refresh=False):  # type: ignore[no-untyped-def]
        from services.clients import AiOutput

        if not self.ai_available:
            return None
        if self.ai_response is not None:
            return self.ai_response
        output = AiOutput(cost_usd=0.01)
        if "description" in kinds:
            output.description = "A book about focused work."
            output.summary = "Attention is a skill."
        if "seo" in kinds:
            output.meta_title = "Deep Work"
            output.meta_description = "Rules for focused success."
            output.keywords = ["focus", "attention"]
        if "tags" in kinds:
            output.tags = ["productivity", "focus"]
        return output

    async def create_book(self, payload):  # type: ignore[no-untyped-def]
        if self.create_fails:
            raise RuntimeError("catalogue rejected it")
        self.created.append(payload)
        return {"id": str(uuid.uuid4()), **payload}

    async def update_book(self, book_id, fields):  # type: ignore[no-untyped-def]
        if self.update_fails:
            raise RuntimeError("books service is down")
        self.updates.append({"book_id": str(book_id), **fields})
        return True

    async def publish_book(self, book_id):  # type: ignore[no-untyped-def]
        self.published.append(str(book_id))
        return True

    async def index_book(self, book_id):  # type: ignore[no-untyped-def]
        self.indexed.append(str(book_id))
        return True


@pytest.fixture
def clients() -> StubClients:
    return StubClients()


@pytest.fixture
def jobs(settings):
    from services import JobService

    return JobService(settings)


@pytest.fixture
def runner(settings, jobs, clients, storage):
    from services import PipelineRunner

    return PipelineRunner(settings, jobs, clients, storage, publisher=None)


@pytest.fixture
def imports(settings, jobs, clients):
    from services import ImportService

    return ImportService(settings, jobs, clients)


@pytest.fixture
def services(settings, jobs, runner, clients, imports):
    return {
        "jobs": jobs,
        "runner": runner,
        "clients": clients,
        "imports": imports,
        # The API's dispatcher, replaced with a recorder. Real dispatch would either
        # need a broker or would run the pipeline inside the request under test.
        "dispatch": _Dispatcher(),
    }


class _Dispatcher:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def __call__(self, job_id: uuid.UUID) -> None:
        self.calls.append(job_id)


@pytest_asyncio.fixture
async def app(engine, settings, services, monkeypatch):
    import fakeredis.aioredis

    from knowledgeos_core import Components, create_app
    from knowledgeos_core import redis as core_redis
    from knowledgeos_core.app import AppContext
    from knowledgeos_core.db import Database
    from routers import internal_router, jobs_router

    fake = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(core_redis.aioredis, "from_url", lambda *a, **k: fake)

    async def _bootstrap(ctx: AppContext) -> None:
        ctx.extras.update(services)

    application = create_app(
        settings=settings,
        components=Components(redis=True, auth=False),
        routers=[jobs_router, internal_router],
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
            permissions=permissions or ["automation:run", "automation:read"],
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


def job_payload(**overrides):  # type: ignore[no-untyped-def]
    from schemas import JobCreate

    defaults = {
        "book_id": BOOK_ID,
        "source_key": SOURCE_KEY,
        "original_filename": "deep-work.pdf",
    }
    defaults.update(overrides)
    return JobCreate(**defaults)

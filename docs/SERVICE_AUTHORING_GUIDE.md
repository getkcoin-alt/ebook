# Service Authoring Guide

**Read this before writing any service code.** Every KnowledgeOS Python service is
built on `packages/core-py` (`knowledgeos_core`). It supplies configuration, logging,
error handling, health checks, metrics, database sessions, Redis, storage, the event
bus, auth and graceful shutdown. Do not reimplement any of it.

---

## 1. Service skeleton

Every service uses this exact layout:

```
apps/<service>/
├── Dockerfile              # thin; delegates to the canonical build
├── requirements.txt        # service-only deps (core-py deps come automatically)
├── railway.json            # Railway service config
├── README.md               # what it owns, endpoints, env vars, runbook
├── .env.example            # every variable it reads
├── main.py                 # entrypoint: `python -m main`
├── settings.py             # Settings(ServiceSettings) subclass
├── models.py               # SQLAlchemy models (or models/ package)
├── schemas.py              # Pydantic request/response models
├── routers/                # one module per resource
│   ├── __init__.py
│   └── <resource>.py
├── services/               # domain logic — routers stay thin
├── migrations/             # Alembic, owned by this service alone
│   ├── env.py
│   └── versions/
├── alembic.ini
└── tests/
    ├── conftest.py
    └── test_<thing>.py
```

### `settings.py`

```python
from knowledgeos_core import ServiceSettings

class Settings(ServiceSettings):
    service_name: str = "books"
    database_schema: str = "books"   # THIS SERVICE'S SCHEMA — never another's
    port: int = 8002

    # service-specific config goes here
    max_upload_bytes: int = 100 * 1024 * 1024

settings = Settings()
```

### `main.py`

```python
from knowledgeos_core import Components, create_app, run

from routers import books_router, reviews_router
from settings import settings

app = create_app(
    settings=settings,
    components=Components(
        database=True,       # Postgres session + readiness probe
        redis=True,          # cache, rate limiter, idempotency store
        storage=True,        # S3/MinIO
        auth=True,           # verify inbound access tokens (needs JWKS_URL)
        events=True,         # publish domain events
        event_consumer="books",   # ALSO consume; the string is the group name
        service_clients=True,     # call sibling services
    ),
    routers=[books_router, reviews_router],
    description="Catalogue, reviews, bookmarks and reading progress.",
)

if __name__ == "__main__":
    run("main:app", settings)
```

You get `/health`, `/health/ready`, `/health/startup`, `/metrics`, `/`, CORS, gzip,
security headers, body limits, request ids, access logs and the error envelope for
free. **Do not add them yourself.**

### `Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1.7
# Build context is the REPOSITORY ROOT (needs packages/core-py).
#   docker build -f apps/books/Dockerfile -t knowledgeos/books .
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
# ... see infrastructure/docker/python-service.Dockerfile and mirror it,
# substituting SERVICE=books. Add any extra system packages this service needs.
```

Copy `infrastructure/docker/python-service.Dockerfile` and hard-code `SERVICE`.
Add extra apt packages only where genuinely needed (e.g. `poppler-utils` for
automation). Keep the runtime stage non-root.

---

## 2. The core-py API

### Errors — raise these, never `HTTPException`

```python
from knowledgeos_core import (
    BadRequestError,      # 400
    UnauthorizedError,    # 401
    PaymentRequiredError, # 402
    ForbiddenError,       # 403
    NotFoundError,        # 404
    ConflictError,        # 409
    ValidationError,      # 422
    RateLimitedError,     # 429
    UpstreamError,        # 502
    ServiceUnavailableError,  # 503
)

raise NotFoundError("Book not found.", details={"book_id": str(book_id)})
```

All produce the platform envelope:
`{"error": {"code": ..., "message": ..., "details": ..., "request_id": ...}}`.

Never put internal detail in `message` — it goes to the client. Put it in the log.

### Dependencies

```python
from knowledgeos_core.deps import (
    DbSession,       # AsyncSession, committed on success, rolled back on error
    CurrentUser,     # Principal — 401 if no valid token
    OptionalUser,    # Principal | None — anonymous allowed
    AdminUser,       # Principal — 403 unless admin/superadmin
    StaffUser,       # moderator/admin/superadmin
    Storage, Redis, Limiter, Idempotency, Ctx,
    InternalCaller,  # HMAC-verified service name, for /internal/* routes
    require_permission, require_roles, require_self_or_permission,
    rate_limit,
)

@router.get("/books/{book_id}")
async def get_book(book_id: UUID, session: DbSession, user: OptionalUser) -> BookOut:
    ...

@router.delete(
    "/books/{book_id}",
    dependencies=[Depends(require_permission("books:delete")), Depends(rate_limit("authenticated"))],
)
async def delete_book(book_id: UUID, session: DbSession) -> MessageResponse:
    ...
```

**Never call `session.commit()` in a handler.** The dependency commits on success and
rolls back on any exception, so a handler cannot leave a half-written aggregate.

### Models

```python
from knowledgeos_core import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

class Book(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "books"
    __table_args__ = (
        Index("ix_books_status_published_at", "status", "published_at"),
        {"schema": "books"},          # ALWAYS set the schema explicitly
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)  # minor units!
```

Rules:
- `{"schema": "<your schema>"}` on every table.
- Money is **integer minor units** (paise/cents). Never `Float`, never `Numeric` for
  transport.
- Index every foreign key and every column used in a `WHERE` or `ORDER BY`.
- Soft-delete user content; filter `deleted_at.is_(None)` **explicitly** in queries.

### Pagination

```python
from knowledgeos_core import Page, PageParams, apply_sorting, page_params, paginate

@router.get("/books")
async def list_books(
    session: DbSession,
    params: Annotated[PageParams, Depends(page_params)],
    sort_by: str = "created_at",
) -> Page[BookOut]:
    stmt = select(Book).where(Book.deleted_at.is_(None))
    stmt = apply_sorting(stmt, Book, sort_by=sort_by, allowed={"created_at", "title", "price_minor"})
    rows, total = await paginate(session, stmt, params)
    return Page.create([BookOut.model_validate(r) for r in rows], total=total, params=params)
```

`allowed` is a strict whitelist — user input selects from a declared set, it is never
interpolated into SQL. Use `CursorPage` + `encode_cursor`/`decode_cursor` for
infinite scroll; deep `OFFSET` on a large catalogue is a production incident.

### Events

```python
from knowledgeos_core import EventType

# publish (AFTER the transaction commits — i.e. after the handler returns is wrong;
# publish inside the handler only when the write is already durable, otherwise use a
# background task triggered post-commit)
await ctx.require_publisher().publish(
    EventType.BOOK_PUBLISHED, {"book_id": str(book.id), "slug": book.slug}
)

# consume — register in a startup hook
@ctx.consumer.on(EventType.PAYMENT_SUCCEEDED)
async def grant_access(event: Event) -> None:
    ...   # MUST be idempotent: delivery is at-least-once
```

### Storage

```python
storage = ctx.require_storage()

target = await storage.create_upload_target(          # browser PUTs directly
    category="book", owner_id=str(user.user_id),
    filename="x.pdf", content_type="application/pdf", visibility="private",
)
url = await storage.signed_download_url(key, expires_in=300)   # AFTER entitlement check
```

**Never stream a file through FastAPI.** Presigned URLs only.

### Calling another service

```python
client = ctx.require_services().get("auth")     # name matches <name>_service_url
data = await client.get_json(f"/internal/users/{user_id}")
```

Requests are HMAC-signed, correlation-propagated and circuit-broken automatically.
Expose your own cross-service endpoints under `/internal/*` guarded by
`InternalCaller`.

### Logging

```python
from knowledgeos_core import get_logger
logger = get_logger(__name__)

logger.info("book.published", book_id=str(book.id), price_minor=book.price_minor)
```

Event-name first (`noun.verb`), then structured key-values. Never f-strings. Never
log a token, password, card number or full request body.

---

## 3. API conventions

- **Versioned prefix**: every public route under `/v1/...`.
- **Plural nouns**: `/v1/books`, `/v1/books/{book_id}/reviews`.
- **Response models are mandatory** — `response_model=` or a typed return annotation.
  This is what generates the OpenAPI the TypeScript SDK is built from.
- **`summary` and `description`** on every endpoint; they become the API docs.
- **Status codes**: 201 + `Location` on create, 204 on delete, 200 otherwise.
- **`Idempotency-Key`** required on any endpoint that charges money or creates an
  order.
- **Rate limit** every public endpoint; use the named policies in
  `knowledgeos_core.ratelimit.POLICIES`.

Schemas subclass `BaseSchema` (from `knowledgeos_core`), which sets
`from_attributes=True` and `extra="forbid"` so a typo'd field is a 422 rather than
silently ignored.

---

## 4. Migrations

Each service owns its own Alembic chain. `migrations/env.py` **must** include:

```python
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    version_table_schema=settings.database_schema,
    include_schemas=True,
    # Without this filter, autogenerate emits DROP TABLE for every other
    # service's tables — it sees them on the connection but not in this
    # service's metadata. This single line prevents a catastrophic migration.
    include_object=lambda obj, name, type_, reflected, compare_to: (
        getattr(obj, "schema", None) == settings.database_schema
    ),
)
```

Also create the schema at the top of the first migration:
`op.execute("CREATE SCHEMA IF NOT EXISTS <schema>")`.

Every migration must have a working `downgrade()`.

---

## 5. Security requirements

Non-negotiable, and reviewed:

1. **Never trust client input for authorisation.** Ownership comes from the token, not
   from a body field.
2. **Parameterised queries only** — SQLAlchemy expressions, never f-string SQL.
3. **Validate uploads by content**, not by filename or client-declared MIME.
4. **Verify webhook signatures** before doing anything with the payload.
5. **Constant-time comparison** for any secret (`hmac.compare_digest`).
6. **No secrets in logs, errors, or URLs.**
7. **Rate limit** anything that authenticates, sends a message, or costs money.
8. **Signed URLs expire in minutes**, and only after an entitlement check.
9. **Identical responses** for "user not found" and "wrong password" — no enumeration.
10. **Audit-log** every privileged action (role change, refund, deletion).

---

## 6. Testing

```python
# tests/conftest.py
from knowledgeos_core.testing import authenticate_as, make_principal

# in a test
authenticate_as(app, make_principal(roles=["admin"], permissions=["books:delete"]))
```

Cover, for every service:
- the happy path of each endpoint
- 401 unauthenticated, 403 wrong role
- 404 for a missing resource
- 422 for invalid input
- the domain edge cases that actually matter (double purchase, expired coupon,
  concurrent refund)

Mark tests needing live infrastructure `@pytest.mark.integration`.

---

## 7. Definition of done

- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy` passes on the service package
- [ ] tests pass and cover the failure paths, not only the happy ones
- [ ] `README.md` documents what the service owns, its endpoints and its env vars
- [ ] `.env.example` lists **every** variable read, with comments
- [ ] `Dockerfile` builds; runtime stage is non-root
- [ ] `railway.json` present, healthcheck on `/health` (**not** `/health/ready`)
- [ ] Every table sets its schema; every FK is indexed
- [ ] Any manual setup (API keys, webhooks) added to `docs/MANUAL_SETUP.md`

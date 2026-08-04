# Build Status

What is actually built, tested and deployable — and what is not. Updated as phases
land. Nothing on this page is aspirational: if it says tested, the test count is real
and was run.

Last updated: 2026-08-04

## Summary

| | Status |
|---|---|
| Foundation (`packages/core-py`) | ✅ Complete · 63 tests |
| Auth service | ✅ Complete · 76 tests |
| API gateway | ✅ Complete · 39 tests |
| Book service | ✅ Complete · 48 tests · 46 endpoints |
| Payment service | ✅ Complete · 154 tests · 42 endpoints |
| Search service | ✅ Complete · 96 tests · 14 endpoints |
| AI · Notifications · Automation · Workers · Admin | ⬜ Not started |
| Frontend | 🟡 `types` + `config` packages only |
| Infrastructure, CI, docs | ✅ Complete |

**476 tests passing.** Ruff clean across everything committed.

---

## ✅ Foundation — `packages/core-py`

The shared runtime every Python service is built on. A service inherits its entire
operational layer from this and writes only domain logic.

| Module | Provides |
|---|---|
| `app.py` | `create_app()` factory, opt-in `Components`, lifespan, graceful shutdown |
| `config.py` | `ServiceSettings` base — identity, datastores, auth, discovery |
| `logging.py` | structlog, JSON/console, secret redaction, request-id propagation |
| `errors.py` | One error envelope; unhandled exceptions never leak internals |
| `health.py` | Separate liveness / readiness / startup probes |
| `metrics.py` | Prometheus series with bounded label cardinality |
| `middleware.py` | Correlation ids, access logs, body limits, security headers |
| `db.py` | Async SQLAlchemy, schema-per-service, dialect-aware engine |
| `redis.py` | Cache, distributed locks, counters |
| `events.py` | Redis Streams bus: consumer groups, claim recovery, dead letters |
| `ratelimit.py` | Sliding window via one atomic Lua script |
| `idempotency.py` | Idempotency-Key handling with request fingerprinting |
| `security.py` | RS256 verification, JWKS cache, bcrypt, HMAC internal signing |
| `storage.py` | S3/MinIO presigned upload and download |
| `http.py` | Circuit-broken service client |
| `tasks.py` | Celery foundation (optional `[tasks]` extra) |
| `pagination.py` | Offset and cursor pagination, safe sorting |

## ✅ Auth service — `apps/auth`

76 tests. RS256 tokens with `kid` rotation and JWKS; opaque refresh tokens with
**rotation + reuse detection**; TOTP 2FA with replay blocking and Fernet-encrypted
secrets; OAuth (Google, GitHub) with PKCE and signed state; RBAC; sessions; audit
log; Redis ban denylist.

Enumeration resistance is enforced and tested — identical responses *and* comparable
timing for unknown addresses.

Migration verified: upgrade creates all 10 tables, `alembic check` reports no drift,
downgrade is clean.

## ✅ API gateway — `apps/gateway`

35 tests. Declarative route table, offline token verification, ban denylist, rate
limiting, response cache with **proven cross-user isolation**, streaming reverse
proxy with hop-by-hop header stripping, per-upstream connection pools, aggregated
OpenAPI.

## ✅ Book service — `apps/books`

48 tests, 46 endpoints, 17 tables. Catalogue with cursor pagination and facets,
authors/publishers/categories, reviews with moderation, bookmarks, reading progress,
wishlists and collections. Downloads are gated on an **entitlement row** — checked
before a presigned URL is minted, never after.

Entitlements are granted by consuming `payment.succeeded` / `order.paid` from the
event bus rather than by a synchronous call from payment: if payment called us
directly mid-deploy, a customer would have paid for a book they cannot open. The
handler is idempotent twice over — a `ProcessedEvent` ledger row written in the same
transaction as the effect, plus a unique constraint on the grant itself.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ✅ Payment service — `apps/payment`

154 tests, 42 endpoints, 12 tables. Orders, Razorpay and Stripe gateways, refunds,
coupons, GST invoicing, subscriptions and affiliate commission.

Four rules govern it, and the tests exist to hold them:

- **Money is an integer of the currency's minor unit.** No float, no `Decimal`.
- **The client never sends a price.** Orders carry book ids; amounts come from the
  catalogue over a signed internal call.
- **Confirmations arrive more than once.** Settlement is idempotent through three
  independent mechanisms: a unique `(provider, provider_payment_id)`, a `mark_paid()`
  that returns `False` on a settled order, and downstream effects that are each
  idempotent on their own.
- **Nothing is hard-deleted.** A refund is a new row plus a status change.

Webhook signature verification runs against the real Razorpay and Stripe schemes and
is tested for tampering, replay, secret confusion and a missing secret. Unverified
deliveries are recorded and refused — a burst of them is an attack signal worth
seeing.

GST is computed as pure integer arithmetic: CGST+SGST intra-state, IGST inter-state,
zero-rated for export, rounded once on the order total. Invoice numbers are a
consecutive serial within the Indian financial year, as the law requires.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ✅ Search service — `apps/search`

96 tests, 14 endpoints, 4 tables. Full-text search with facets and cursor
pagination, autocomplete across three indexes in one round trip, related books,
time-decayed trending, and search analytics.

**The index is a cache.** Every document can be rebuilt from the books service, so
the `search` schema holds only the index ledger and analytics — nothing
authoritative. **A search outage is not a platform outage**: Meilisearch being down
produces clean 503s on the query path and nothing else, and index setup at boot is
best-effort so a dead engine cannot stop the service from starting.

Filters, sorts and page depth are validated against closed sets *before* reaching the
engine, so a crafted query is a 400 from us rather than a 400 from Meilisearch
surfacing as a 500. `status = "published"` is appended unconditionally.
`assert_filters_are_indexable()` runs at startup and fails fast if the API accepts a
filter the index never declared filterable.

Semantic search is a config flip behind a `SearchBackend` seam and is off by default;
a request that opts in without a configured embedder gets keyword results and an
honest `semantic: false`.

Meilisearch is faked at the **HTTP transport** in tests, not at the client, so every
request body and response mapping under test is the real one.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ⬜ Not yet started

AI, notifications, automation, workers, admin, and the frontend application. Their
directories exist; `apps/frontend` is empty apart from the shared `types` and
`config` packages.

---

## Verified vs unverified

Being precise about this matters more than a green checkmark.

**Verified — actually executed:**
- All 476 tests, on every commit
- `ruff check` and `ruff format --check`
- Auth, books, payment and search migrations: upgrade, `alembic check` (no drift), downgrade
- The auth service booting in `production` mode with real generated keys
- `scripts/generate-keys.sh` output loading through the real key ring and signing a
  token that verifies
- `docker compose config` parses

**Not verified — no environment available in the build sandbox:**
- **Docker images have never been built.** There is no Docker daemon here. The
  Dockerfiles follow a single reviewed pattern but are unexercised. CI builds them on
  the first push.
- **No live Postgres, Redis, MinIO or Meilisearch.** Test suites run on in-memory
  SQLite and `fakeredis` by design, so migrations are verified against SQLite rather
  than PostgreSQL. Run `alembic upgrade head` against a real Postgres before
  trusting production.
- **The frontend has never been installed or built** (`pnpm install` has not run).
- **No end-to-end run** with several services talking to each other.

## Suggested first steps on a real machine

```bash
pnpm stack:up                      # Postgres, Redis, MinIO, Meilisearch, Mailpit
cd apps/auth && alembic upgrade head   # against real Postgres this time
python -m main                     # :8001
curl localhost:8001/.well-known/jwks.json
```

Then bring up the gateway and confirm it proxies to auth.

## Fixes made to `core-py` while building on it

Each was found by a service failing against it, not by inspection:

1. **`Database` hardcoded Postgres pool and connect args**, so no service could be
   tested without a live Postgres. Now dialect-aware.
2. **UUID columns used the postgresql-only type**, blocking SQLite entirely.
   Replaced with `sqlalchemy.Uuid` via a shared `UUIDType`.
3. **`HealthState` never reset `shutting_down`**, so an app object started twice in
   one interpreter reported "draining" forever.
4. **Logging double-encoded** — JSON nested inside JSON, because structlog and the
   stdlib formatter both rendered.
5. **`passlib` imports the stdlib `crypt` module**, removed in Python 3.13. Replaced
   with `bcrypt` directly.
6. **`ListResponse` was defined but never exported.**
7. **`pydantic-settings` JSON-decoded every list-typed field before validators ran**,
   so `CORS_ORIGINS=http://localhost:3000` was a hard boot failure. Every test
   constructed `Settings(...)` in Python and bypassed the environment path entirely,
   so nothing caught it until the first real deploy. Fixed with `enable_decoding=False`
   plus 20 tests that go through the environment.
8. **Reading a server-defaulted column straight after a write raised
   `MissingGreenlet`.** Fixed platform-wide with `eager_defaults=True` on `Base`.
9. **`use_enum_values` coerces only the fields a client actually sent**, so
   `payload.field.value` worked on defaults and raised `AttributeError` the moment
   someone passed the field explicitly. This was a live 500 on the books upload
   endpoint, found while building payment. The trap is now documented on `BaseSchema`
   itself and there is a regression test.

Two more, found while building search — both from writing a router against a service
API that did not exist, with no test on the route:

10. **Every taxonomy list endpoint was a 500.** `/v1/authors`, `/v1/publishers` and
    `/v1/categories` called `list_*(session, q=..., cursor=..., limit=...)` while the
    service takes `params: PageParams` and returns a 2-tuple. Nothing covered those
    routes. They now use offset paging (correct for a small, near-static taxonomy)
    and have tests.
11. **The internal book listing returned no cursor**, so the search reconciler would
    have stopped after one page — an index silently containing only the newest few
    hundred books. `/internal/authors` and `/internal/categories` did not exist at
    all. All three now return `InternalPage` with a `next_cursor`.

And one design bug caught before it shipped: the gateway cached `/v1/search` for 60
seconds. A cached hit never reaches the search service, so a popular query would be
recorded once per TTL instead of once per search — undercounting exactly the queries
the zero-result report exists for, and inverting the trending ranking so that *less*
popular queries rank higher. Search is no longer cached; trending still is.

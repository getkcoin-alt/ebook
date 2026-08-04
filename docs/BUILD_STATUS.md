# Build Status

What is actually built, tested and deployable — and what is not. Updated as phases
land. Nothing on this page is aspirational: if it says tested, the test count is real
and was run.

Last updated: 2026-08-04

## Summary

| | Status |
|---|---|
| Foundation (`packages/core-py`) | ✅ Complete · 43 tests |
| Auth service | ✅ Complete · 76 tests |
| API gateway | ✅ Complete · 35 tests |
| Book service | 🟡 Models + schemas complete; service layer in progress |
| Search · AI | 🟡 In progress |
| Payment · Notifications · Automation · Workers · Admin | ⬜ Not started |
| Frontend | 🟡 `types` + `config` packages only |
| Infrastructure, CI, docs | ✅ Complete |

**154 tests passing.** Ruff clean across everything committed.

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

## 🟡 Book service — `apps/books`

`models.py` (17 tables, including a `ProcessedEvent` table for idempotent event
handling) and `schemas.py` (55 classes) are complete. Service layer, routers,
migrations and tests are being built.

## ⬜ Not yet started

Payment, notifications, automation, workers, admin, and the frontend application.
Their directories exist; `apps/frontend` is empty apart from the shared `types` and
`config` packages.

---

## Verified vs unverified

Being precise about this matters more than a green checkmark.

**Verified — actually executed:**
- All 154 tests, on every commit
- `ruff check` and `ruff format --check`
- Auth migration: upgrade, `alembic check` (no drift), downgrade
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

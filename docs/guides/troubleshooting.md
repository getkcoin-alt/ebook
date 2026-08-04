# Troubleshooting

Symptom → likely cause → fix. Organised by what you actually observe, not by which
component is at fault.

---

## Startup

### A service exits immediately with `DATABASE_URL is required`

`Components(database=True)` but no `DATABASE_URL`. Check `.env` exists and is being
loaded from the directory you launched from — `pydantic-settings` reads `.env`
relative to the **working directory**, not the module.

### `JWKS_URL must be set when auth is enabled`

The service declares `Components(auth=True)` but has no `JWKS_URL`. Either point it
at the auth service (`http://localhost:8001/.well-known/jwks.json`) or drop the auth
component if the service genuinely does not authenticate callers.

### `Auth service published an empty JWKS`

The auth service is running but has no signing keys. Generate them:

```bash
./scripts/generate-keys.sh
```

and set `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` on the auth service.

### Service starts, then `/health/ready` stays 503

A dependency probe is failing. The response body names which one:

```bash
curl -s localhost:8002/health/ready | jq
```

`{"database": {"status": "down", "error": "…"}}` tells you exactly what to fix.

Note that **storage is registered as optional** — a down MinIO shows in the body but
does not block readiness, because most endpoints do not touch it.

---

## Authentication

### Every request returns 401 with a valid-looking token

In order of likelihood:

1. **Token expired.** Access tokens live 15 minutes by design. Refresh.
2. **`iss`/`aud` mismatch.** `JWT_ISSUER` and `JWT_AUDIENCE` must be identical on the
   auth service and every verifier. A typo in one service produces exactly this.
3. **Clock skew.** More than 10 seconds of drift between containers breaks `exp`
   validation. The verifier allows 10s of leeway; beyond that, fix NTP.
4. **Sending a refresh token as a bearer token.** Rejected deliberately — the `typ`
   claim is checked. Refresh tokens are opaque and are not JWTs.

### `Token was signed with an unrecognised key`

The `kid` in the token is not in the cached JWKS. Usually a key was rotated and the
verifier's cache is stale — it force-refreshes on an unknown `kid`, rate-limited to
once per 10 seconds, so this resolves itself within seconds. If it persists, the auth
service is publishing a different key set than the one that signed the token: confirm
you are not running two auth instances with different `JWT_PRIVATE_KEY` values.

### Login succeeds but the next request is 401

The refresh cookie is not being sent. Check:

- `credentials: 'include'` on the fetch call
- The API and frontend origins are covered by `CORS_ORIGINS`
- `SameSite=Strict` blocks the cookie on genuine cross-site navigations — for a
  frontend and API on different registrable domains you need `SameSite=None; Secure`,
  which in turn requires HTTPS on both

### A user was banned but can still make requests

Their access token remains valid until it expires — that is inherent to stateless
JWTs. The gateway's Redis denylist is what closes the gap within seconds. Verify the
key exists:

```bash
docker exec -it kos-redis redis-cli GET "kos:denylist:user:<user_id>"
```

If it is absent, the ban path is not writing to the denylist.

---

## Database

### `too many connections`

Total connections = services × replicas × `db_pool_size` (+ overflow). One Postgres
has a global `max_connections`, and the schema-per-service topology shares it.

Immediate fix: lower `DB_POOL_SIZE`. Real fix beyond ~10 replicas per service:
pgbouncer in transaction mode. `statement_cache_size=0` is already set on the asyncpg
connection, which is what makes that safe — asyncpg's prepared-statement cache is
per-connection and breaks under transaction pooling.

### Alembic autogenerate wants to drop other services' tables

**Do not apply that migration.** The `include_object` filter in `migrations/env.py` is
missing or wrong. It must be:

```python
include_object=lambda obj, name, type_, reflected, compare_to: (
    getattr(obj, "schema", None) == settings.database_schema
),
```

Without it, Alembic sees every schema on the connection, finds them absent from this
service's metadata, and generates `DROP TABLE` for all of them.

### A query got slow after a data import

Statistics are stale. `ANALYZE` the table. If it persists, the planner has likely
switched to a sequential scan because a new index is missing — check with
`EXPLAIN (ANALYZE, BUFFERS)`.

Postgres logs anything over 200ms locally:

```bash
docker compose -f infrastructure/docker/docker-compose.yml logs postgres | grep duration
```

### A list endpoint issues hundreds of queries

Classic N+1: a relationship is lazy-loading per row. Use `selectinload`:

```python
stmt = select(Book).options(selectinload(Book.authors), selectinload(Book.categories))
```

---

## Redis and events

### Rate limits are not being enforced

The limiter **fails open** by design — a Redis outage degrading to "unlimited" beats
a Redis outage taking down every endpoint. Look for `ratelimit.backend_unavailable`
in the logs; that confirms it is the fallback and not a logic bug.

### A consumer is not receiving events

```bash
docker exec -it kos-redis redis-cli
> XLEN kos:events                 # are events being published at all?
> XINFO GROUPS kos:events         # is the group registered? what is `pending`?
```

- `XLEN` is 0 → the producer is not publishing. Check for `event.published` in its logs.
- The group is missing → the consumer never started. `Components(event_consumer="…")`
  must be set.
- `pending` is high and growing → the handler is throwing. Look for
  `event.handler_failed`.

### Events are being processed twice

Expected. Delivery is **at-least-once** and handlers must be idempotent. Key the
handler's side effect on `event.id`.

### Events land in the dead letter stream

They exhausted five attempts. Inspect them before replaying:

```bash
docker exec -it kos-redis redis-cli XRANGE kos:events:dead - + COUNT 10
```

The `error` field holds the exception. Fix the handler, then replay.

### A consumer lagged and permanently missed events

The stream is trimmed at ~500,000 entries. A consumer down long enough for its
position to be trimmed away skips those events. Monitor group lag; for a long
outage, reconcile from the source tables rather than the stream.

---

## Files and storage

### Presigned URL works in tests, 404s in the browser

`S3_PUBLIC_ENDPOINT_URL` is set to the internal hostname. The API signs with the
internal endpoint and rewrites the host before returning the URL — the public value
must be what a browser can actually resolve.

### `SignatureDoesNotMatch` on upload

Usually one of:

- The URL expired (default 15 minutes)
- The client sent a different `Content-Type` than the one the policy was signed for
- A proxy modified the request

Presigned **POST** (which is what `create_upload_target` issues) enforces content
type and a size range in the policy; a mismatch on either produces this.

### Uploads fail with 413

Two independent limits: `MAX_REQUEST_BODY_BYTES` on the API, and the `max_bytes` in
the presigned policy. Direct-to-storage uploads only hit the second.

---

## Payments

### A webhook arrives but the order stays unpaid

1. **Signature verification failed.** The most common cause is the wrong
   `*_WEBHOOK_SECRET` — providers issue a different one per endpoint, and the Stripe
   CLI issues yet another for local forwarding.
2. **Raw body was modified before verification.** The signature covers the exact
   bytes; any middleware that reparses and re-serialises JSON breaks it.
3. The provider is retrying against an unreachable URL — check the provider dashboard's
   delivery log.

### Duplicate charges

The `Idempotency-Key` header is missing on the order-creation call. It is required,
and it is what makes a double-click safe.

### Refund succeeded at the provider but access was not revoked

The `order.refunded` event failed to publish or its consumer errored. Check the dead
letter stream. The payment service's database is the source of truth; entitlements
can be reconciled from it.

---

## Automation

### Jobs sit in `queued` forever

No worker is consuming that queue. Check the worker is running **and** listening to
the right queue:

```bash
celery -A apps.workers.celery_app worker -Q automation,ai
celery -A apps.workers.celery_app inspect active
```

### A task runs twice

Either `visibility_timeout` (3600s) is shorter than the task, so Redis redelivered it
while it was still running — raise the timeout for genuinely longer tasks — or the
task is not idempotent and a retry re-ran a completed side effect.

### Workers are OOM-killed during PDF processing

Image and PDF libraries leak native memory. `worker_max_memory_per_child=512000` (KiB)
recycles a process before that becomes fatal. If it still happens, the container needs
more than 2GB, or the file is genuinely too large — cap accepted sizes.

### AI stages are being skipped

By design when no provider key is set or `AI_MONTHLY_BUDGET_USD` is exhausted. Books
publish with metadata only rather than blocking the pipeline. Check
`knowledgeos_ai_cost_usd_total`.

---

## Frontend

### Hydration mismatch

Server and client rendered different HTML. Almost always `Date.now()`, `Math.random()`
or `localStorage` read during render. Move it into `useEffect`.

### Theme flashes the wrong colour on load

The inline theme script must run **before** hydration, in `<head>`. If it is in a
component, it runs too late.

### `pnpm build` fails but `pnpm dev` works

Dev is more permissive. The build type-checks the whole graph and runs static
generation — a page calling a browser API at module scope works in dev and fails at
build.

### Requests fail with CORS errors

The origin is not in `CORS_ORIGINS`. Note that `CORS_ORIGINS` must be exact origins
(scheme + host + port); `*` is invalid alongside credentials and is rejected by
browsers.

---

## Still stuck

Every response carries `X-Request-ID`, and every log line produced while handling it
carries the same value. Grep it across all services and you have the complete path,
including which hop failed:

```bash
curl -i https://api.yourdomain.com/v1/books 2>&1 | grep -i x-request-id
```

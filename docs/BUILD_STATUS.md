# Build Status

What is actually built, tested and deployable — and what is not. Updated as phases
land. Nothing on this page is aspirational: if it says tested, the test count is real
and was run.

Last updated: 2026-08-05

## Summary

| | Status |
|---|---|
| Foundation (`packages/core-py`) | ✅ Complete · 63 tests |
| Auth service | ✅ Complete · 81 tests |
| API gateway | ✅ Complete · 39 tests |
| Book service | ✅ Complete · 48 tests · 66 endpoints |
| Payment service | ✅ Complete · 156 tests · 42 endpoints |
| Search service | ✅ Complete · 96 tests · 15 endpoints |
| Notification service | ✅ Complete · 69 tests · 28 endpoints |
| AI service | ✅ Complete · 48 tests · 12 endpoints |
| Automation service | ✅ Complete · 130 tests · 17 endpoints |
| Workers service | ✅ Complete · 46 tests · 7 endpoints |
| Admin service | ✅ Complete · 57 tests · 11 endpoints |
| Frontend | 🟡 `types` + `config` packages only |
| Infrastructure, CI, docs | ✅ Complete |

**833 tests passing.** Ruff clean across everything committed.

Three numbers above correct earlier revisions of this page. The gateway and payment
test counts said 42 and 154; the real figures are 39 and 156. The book service's
endpoint count said 46; counting its OpenAPI schema gives 66. All of these were
measured, not estimated.

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

48 tests, 66 endpoints, 17 tables. Catalogue with cursor pagination and facets,
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

## ✅ Notification service — `apps/notifications`

69 tests, 28 endpoints, 8 tables. Email, SMS, WhatsApp, push and in-app messages
with stored templates, per-category preferences, delivery tracking, retries and
suppression.

Driven by **events, not callers**: a user registers, an order is paid, a refund is
issued, and this service decides those facts deserve a message. The auth service does
not know what a welcome email says and the payment service does not know a receipt
exists — otherwise changing a subject line is a five-service deploy.

One rule outranks everything: **an address on the suppression list is never contacted
again**, transactional or not. Mailing an address that issued a spam complaint costs
the sending domain its reputation, and that takes password resets down with it.
Removal is an operator action, never automatic.

Rendering is `{{name}}` substitution and nothing else — templates are editable
through the admin API, so a template language with arbitrary evaluation would be
remote code execution behind an admin token. Missing variables abort the send: a
customer receiving `Hi {{first_name}},` is an apology, a 422 is a bug report.

A permanent failure is never retried — the receiving server already said the mailbox
does not exist, and asking again looks like a dictionary attack.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ✅ AI service — `apps/ai`

48 tests. Generated book copy, SEO, tagging, catalogue-grounded chat, moderation and
embeddings, behind a **hard daily cost ceiling** — reaching it returns 503 rather than
continuing to spend. Checked before every model call, never after, from a single
indexed row rather than a SUM over the ledger; spend recorded with a conditional
UPDATE so concurrent requests cannot both claim the same headroom. Second per-user
ceiling so one account cannot consume the platform budget.

Two-tier cache (Redis over a table) keyed on everything affecting the output. Prompts
are server-owned per task kind and user content only ever occupies the user turn.
Moderation fails closed. Chat is grounded in real catalogue results and verifies
entitlement before any book text reaches a prompt. Every outcome — cached, blocked and
failed included — is recorded in the generation ledger.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ✅ Automation service — `apps/automation`

130 tests. The thirteen-stage ingestion pipeline, each stage checkpointed to
`job_stages` so a job that dies at stage 11 resumes at stage 11 rather than redoing
ten minutes of CPU and three dollars of tokens.

Failures are sorted into three categories, and the distinction is the whole policy:
**skipped** (nothing to do — recorded, pipeline continues), **terminal** (the input is
wrong and will be wrong next time — no retry), and **transient** (checkpointed,
requeued with jittered backoff, resumes at that stage). Enrichment stages never block
publication; a book with no AI tags is publishable.

`AUTO_PUBLISH` is off by default. A finished job leaves the book ready for a human.

The untrusted-input handling is the part worth reviewing: zip bombs caught on declared
size inside the entry loop, archive path traversal refused, XML entity declarations
refused, image dimensions checked before decode, encrypted PDFs distinguished from
permissions-only ones. No PDF renderer is installed — covers are extracted from
embedded images rather than rasterised — deliberately trading a capability for a
smaller attack surface.

Test documents are real: actual PDFs with content streams, spec-shaped EPUB zips, and
a genuine small archive declaring a huge expansion.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ✅ Workers service — `apps/workers`

46 tests. The platform scheduler: thirteen jobs across seven services, driven by
Celery beat.

**It calls HTTP, never a table.** Every schedule entry names a service and one of its
`/internal/maintenance/*` routes. A scheduler with direct database access to every
schema is how a microservice platform quietly re-couples — the sweep that expires
orders would end up encoding the payment service's state machine, and the constraint
that makes schema-per-service worth anything (ADR 0002) would be broken by the one
process nobody thinks of as a service.

Runs are single-flight through a Redis lock, because beat fires on every replica.
Losing that lock is recorded as its own outcome and does not count as a failure — a
three-replica deployment must not look like it is failing two runs in three. So is a
timeout, which very likely means the sweep completed and we stopped waiting.

The run-history table answers the question logs are worst at: **has beat stopped
firing?** A job that is not running produces no logs and no failures, so it is
invisible in every signal except the absence of recent runs.

This work also added the auth service's first cleanup sweep. Its token, session and
audit tables gained a row per login and nothing ever deleted from them. Expired refresh
tokens are kept through a grace window rather than dropped at expiry — deleting one the
instant it expires destroys the evidence reuse detection depends on — and
security-relevant audit actions are exempt from retention entirely.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ✅ Admin service — `apps/admin`

57 tests. The operator dashboard and the home of feature flags.

**It owns almost nothing.** Every number comes from the service that produced it, read
through the same admin endpoint an operator could call directly — which is what keeps
the figure on this page identical to the one on the owning service's own screen. A
second copy here would be a second copy that drifts, and nobody looking at it would
know there were two.

**It forwards the operator's own bearer token** when it fans out. Calling siblings
under its own HMAC identity would make it a confused deputy: it holds a key that opens
every internal route, so anyone past *its* permission check would receive data from
every service regardless of what they may see there. Each service instead applies its
own check against the real operator.

Every panel carries its own status and its own timeout, fetched concurrently. One
service restarting produces one unavailable card, not a red page. Upstream error text
never reaches the browser, and panel payloads are flattened and bounded so this
service's response size is not a function of somebody else's schema.

The two tables it does own are feature flags and their audit history, because every
service reads flags and none owns them. Evaluation returns **decisions, not rules** —
handing back a rollout percentage would make each service implement the bucketing, and
two hash implementations diverge into a user who has a feature on one page and not the
next. Bucketing is a stable hash of flag key and user id, so raising a rollout only
ever adds people, and the first cohort differs per flag.

Migration verified: upgrade, `alembic check` (no drift), downgrade.

## ⬜ Not yet started

The frontend application. `apps/frontend` is empty apart from the shared `types` and
`config` packages; `docs/FRONTEND_LOVABLE_PROMPT.md` is the brief for building it
against the deployed API.

---

## Verified vs unverified

Being precise about this matters more than a green checkmark.

**Verified — actually executed:**
- All 833 tests, on every commit
- `ruff check` and `ruff format --check`
- Every service's migrations — auth, books, payment, search, notifications, ai,
  automation: upgrade, `alembic check` (no drift), downgrade
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

Two more, from the notification work, both in `core-py` and both affecting every
service:

12. **A keyset cursor matched every row on SQLite.** `TimestampMixin.created_at` was
    written by the database clock (`CURRENT_TIMESTAMP`, no microseconds) but compared
    against Python datetimes (with microseconds). SQLite compares timestamps as
    strings, so the stored value sorted before every bound value and
    `WHERE created_at < :cursor` returned page one forever. Postgres compares real
    timestamps and is unaffected — which is exactly why it was invisible until a
    paging test ran against the dialect the whole suite uses. Fixed with a
    client-side default alongside the server default; books, payment, search and
    notifications all share the mixin, and payment now has a test that fails without
    the fix.
13. **`use_enum_values` bit for the third time**, here on a request field that arrives
    as a string when sent and as an enum when defaulted — so `channel is
    NotificationChannel.EMAIL` silently never matched, which looks exactly like
    "email is disabled".

And one design bug caught before it shipped: the gateway cached `/v1/search` for 60
seconds. A cached hit never reaches the search service, so a popular query would be
recorded once per TTL instead of once per search — undercounting exactly the queries
the zero-result report exists for, and inverting the trending ranking so that *less*
popular queries rank higher. Search is no longer cached; trending still is.

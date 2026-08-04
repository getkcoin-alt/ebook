<div align="center">

# KnowledgeOS

**An AI-first e-book commerce platform.**

Eleven independently deployable services, one Railway project, one monorepo.

</div>

---

## What this is

A production-grade e-book storefront and reading platform. Books are ingested through
an automated pipeline that extracts metadata, generates descriptions and SEO copy with
an LLM, produces thumbnails and alternate formats, watermarks the files, uploads them
to object storage and indexes them for search — then publishes. Readers browse, buy,
and read in the browser.

It is built to be operated: every service ships structured logs, Prometheus metrics,
liveness/readiness probes and graceful shutdown, because the second year of running a
system costs more than the first year of writing it.

## Architecture at a glance

```
                          ┌──────────────┐
     browser ────────────▶│   frontend   │  Next.js 15 · React 19 · SSR + PWA
                          └──────┬───────┘
                                 │ HTTPS
                          ┌──────▼───────┐
                          │   gateway    │  the only public API ingress
                          └──────┬───────┘  rate limits · auth · cache · routing
                                 │
   ┌──────┬──────┬──────┬────────┼────────┬──────────┬──────────┬───────┐
   ▼      ▼      ▼      ▼        ▼        ▼          ▼          ▼       ▼
 auth  books  search   ai    payment  notifications automation admin  workers
   │      │      │      │        │        │            │         │       │
   └──────┴──────┴──────┴────────┴────────┴────────────┴─────────┴───────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          ▼                      ▼                      ▼
     PostgreSQL              Redis                MinIO / S3
   schema per service   cache · queues ·      book files · covers
                        rate limits · events
                                 │
                            Meilisearch
```

Services communicate two ways and only two ways: **HTTP** for synchronous reads, and
**Redis Streams** for asynchronous facts. The frontend never touches a database.

### Decisions worth knowing before you read the code

| Decision | Short version | Detail |
|---|---|---|
| One database, schema per service | Railway bills per plugin; schema isolation is enforceable with `GRANT` and splits out cleanly later | [ADR 0002](docs/adr/0002-database-topology.md) |
| RS256 tokens + JWKS | Only `auth` can mint tokens; everyone else verifies offline with no network call | [ADR 0003](docs/adr/0003-authentication.md) |
| Redis **Streams**, not Pub/Sub | Pub/Sub silently drops messages during a deploy — unacceptable for `payment.succeeded` | [ADR 0004](docs/adr/0004-event-driven-communication.md) |
| Money as integer minor units | Floats cannot represent 0.1; float money produces invoices that do not reconcile | [ADR 0002](docs/adr/0002-database-topology.md) |
| Files never proxied through the API | Presigned upload/download URLs; a 40MB PDF must not occupy a worker | `core-py/storage.py` |
| `/health` ≠ `/health/ready` | Pointing a restart-triggering healthcheck at dependency state turns a blip into an outage | `core-py/health.py` |

All records: [`docs/adr/`](docs/adr/).

## Repository layout

```
apps/
  frontend        Next.js 15 · React 19 · Tailwind · shadcn/ui
  gateway         reverse proxy, rate limiting, edge auth, response cache
  auth            identity, JWT/JWKS, OAuth, 2FA, RBAC, sessions
  books           catalogue, authors, reviews, bookmarks, reading progress
  automation      the ingestion pipeline (Celery)
  ai              LLM provider abstraction (Anthropic/OpenAI/Google/OpenRouter/Ollama)
  search          Meilisearch indexing and querying
  payment         Razorpay + Stripe, invoices, refunds, coupons, subscriptions
  notifications   email, SMS, WhatsApp, push
  admin           analytics, moderation, system health
  workers         Celery workers and scheduled jobs

packages/
  core-py         shared Python runtime — every service is built on this
  types           TypeScript domain types, generated from the OpenAPI specs
  api-client      typed HTTP client for the frontend
  auth-sdk        token storage, refresh, React hooks
  ui              shared React components
  config          shared ESLint/Tailwind/TS config
  logger          structured logging for TypeScript
  utils           shared helpers

infrastructure/
  docker          Dockerfiles and the local compose stack
  railway         deployment topology and per-service config
  monitoring      Prometheus rules and Grafana dashboards
  nginx           optional edge config for non-Railway hosting

docs/             architecture, ADRs, API reference, runbooks
scripts/          operational tooling
```

### `packages/core-py` is the thing to read first

Nine FastAPI services share one runtime library. It provides configuration, structured
logging with automatic secret redaction, the platform error envelope, health and
metrics endpoints, the middleware stack, database sessions, Redis, object storage,
the event bus, rate limiting, idempotency, JWT verification and graceful shutdown.

A service is then roughly this:

```python
from knowledgeos_core import Components, ServiceSettings, create_app

class Settings(ServiceSettings):
    service_name: str = "books"
    database_schema: str = "books"

app = create_app(
    settings=Settings(),
    components=Components(database=True, redis=True, auth=True, events=True),
    routers=[books_router, reviews_router],
)
```

Everything operational is inherited. Service code is domain logic.

## Getting started

Requirements: Docker, Node 22+, pnpm 10+, Python 3.11+.

```bash
git clone https://github.com/getkcoin-alt/ebook.git knowledgeos
cd knowledgeos

cp .env.example .env          # defaults match the local compose stack
pnpm install                  # TypeScript workspace
pnpm stack:up                 # Postgres, Redis, MinIO, Meilisearch, Mailpit

python -m venv .venv && source .venv/bin/activate
pip install -e "packages/core-py[dev,tasks]"
```

Local endpoints:

| | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API gateway | http://localhost:8000 · docs at `/docs` |
| MinIO console | http://localhost:9001 |
| Meilisearch | http://localhost:7700 |
| Mailpit (catches all outbound email) | http://localhost:8025 |
| Grafana (`--profile monitoring`) | http://localhost:3001 |

Full walkthrough: [`docs/guides/local-development.md`](docs/guides/local-development.md).

## Things you must configure yourself

API keys, OAuth credentials and signing keys cannot be generated for you. Every
placeholder is listed, with instructions and links, in
**[`docs/MANUAL_SETUP.md`](docs/MANUAL_SETUP.md)**.

The platform runs locally with no external accounts at all — payments, AI and SMS
degrade gracefully when unconfigured, so you can develop the catalogue and reader
without a single API key.

## Testing

```bash
pytest                        # Python: unit + integration
pnpm test                     # TypeScript: Vitest
pnpm --filter frontend test:e2e   # Playwright
pnpm lint && pnpm typecheck
ruff check . && mypy packages/core-py
```

## Deployment

One Railway project, every service inside it, private networking between them.
See [`infrastructure/railway/README.md`](infrastructure/railway/README.md) for the
topology, environment strategy, volumes, deploy order and scaling guidance.

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | System design, data flow, sequence diagrams |
| [ADRs](docs/adr/) | Decisions, alternatives rejected, consequences accepted |
| [Manual setup](docs/MANUAL_SETUP.md) | **Every value you must supply** |
| [Local development](docs/guides/local-development.md) | Running the stack |
| [Railway deployment](infrastructure/railway/README.md) | Production topology |
| [API reference](docs/api/) | Endpoints, schemas, error codes |
| [Troubleshooting](docs/guides/troubleshooting.md) | Symptom → cause → fix |

## Licence

Proprietary. All rights reserved.

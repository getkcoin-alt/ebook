# Local Development

Getting KnowledgeOS running on your machine, and the workflow once it is.

## Prerequisites

| | Version | Why |
|---|---|---|
| Docker | 24+ | Postgres, Redis, MinIO, Meilisearch, Mailpit |
| Node | 22+ | Next.js 15 needs it |
| pnpm | 10+ | workspace protocol; npm/yarn will not resolve `workspace:*` |
| Python | 3.11+ | services target 3.12 in Docker, 3.11 works locally |

## First run

```bash
git clone https://github.com/getkcoin-alt/ebook.git knowledgeos
cd knowledgeos

cp .env.example .env          # defaults already match the compose stack
pnpm install
pnpm stack:up                 # Postgres · Redis · MinIO · Meilisearch · Mailpit

python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e "packages/core-py[dev,tasks]"

# each service's own dependencies
for r in apps/*/requirements.txt; do pip install -r "$r"; done
```

**No API keys are required to start.** Payments, AI, SMS and OAuth all degrade
gracefully when unconfigured, so the catalogue, reader and most of the app work
immediately. See [MANUAL_SETUP.md](../MANUAL_SETUP.md) when you need one of them.

### Migrations

Each service owns its own Alembic chain and migrates independently:

```bash
cd apps/auth  && alembic upgrade head && cd ../..
cd apps/books && alembic upgrade head && cd ../..
# …and so on for each service with a migrations/ directory
```

Order does not matter — schemas are independent.

### Running services

Backend services run **on the host**, not in Docker, so uvicorn's reloader works.
Each needs its own terminal:

```bash
cd apps/auth    && python -m main     # :8001
cd apps/books   && python -m main     # :8002
cd apps/gateway && python -m main     # :8000
```

Frontend:

```bash
pnpm --filter @knowledgeos/frontend dev    # :3000
```

Celery workers, when working on automation or notifications:

```bash
celery -A apps.workers.celery_app worker --loglevel=info -Q automation,ai,notifications
celery -A apps.workers.celery_app beat --loglevel=info      # scheduled jobs
```

You rarely need all eleven. Run the gateway plus whichever service you are changing;
the gateway returns a clean 503 for anything not running.

## Where things are

| | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API gateway | http://localhost:8000 · aggregated docs at `/docs` |
| auth · books · search · ai | :8001 · :8002 · :8003 · :8004 |
| payment · notifications · automation · admin | :8005 · :8006 · :8007 · :8008 |
| MinIO console | http://localhost:9001 — `knowledgeos` / `knowledgeos_dev_password` |
| Meilisearch | http://localhost:7700 |
| **Mailpit** — every outbound email lands here | http://localhost:8025 |
| Grafana (`--profile monitoring`) | http://localhost:3001 — `admin` / `admin` |

Mailpit is worth knowing about specifically: **nothing can escape to a real inbox**,
which matters the first time a seed script loops over 500 users.

## Daily workflow

```bash
pnpm stack:up                 # start infrastructure
pnpm stack:logs               # tail it
pnpm stack:down               # stop, keep data
pnpm stack:reset              # destroy volumes, start clean
```

Before pushing:

```bash
ruff check . && ruff format .
mypy packages/core-py
pytest

pnpm lint && pnpm typecheck && pnpm test && pnpm build
```

CI runs exactly these.

## Working on a service

Read [SERVICE_AUTHORING_GUIDE.md](../SERVICE_AUTHORING_GUIDE.md) first — it is the
contract. The short version:

- Raise `NotFoundError`, never `HTTPException`
- Never call `session.commit()`; the dependency handles it
- Money is integer minor units
- Set `{"schema": "…"}` on every table
- `logger.info("book.published", book_id=…)` — event name first, structured values,
  never f-strings

### Adding a migration

```bash
cd apps/<service>
alembic revision --autogenerate -m "add reading streaks"
```

**Review the generated file before applying it.** Autogenerate does not detect
column renames — it emits a DROP plus an ADD, which silently destroys the data. It
also misses `server_default` changes and check constraints.

Verify the migration only touches your own schema. `env.py` has an `include_object`
filter that enforces this; if you see another service's tables in the diff, that
filter is broken and applying the migration would drop them.

Every migration needs a working `downgrade()`.

### Adding an endpoint

1. Pydantic schemas in `schemas.py`, subclassing `BaseSchema`
2. Route in `routers/<resource>.py` — thin; domain logic goes in `services/`
3. Typed return annotation or `response_model=` (this generates the OpenAPI the
   TypeScript SDK is built from)
4. `summary=` and `description=` — they become the public API docs
5. Rate limit it
6. Tests: happy path **and** 401/403/404/422

## Debugging

**Follow one request across services.** Every response carries `X-Request-ID`, and
every log line inside that request carries it too:

```bash
curl -i http://localhost:8000/v1/books | grep -i x-request-id
# then grep that value across every service's output
```

**Find slow queries.** Postgres logs anything over 200ms locally:

```bash
docker compose -f infrastructure/docker/docker-compose.yml logs postgres | grep duration
```

**Inspect the event bus:**

```bash
docker exec -it kos-redis redis-cli
> XLEN kos:events                     # stream depth
> XINFO GROUPS kos:events             # per-consumer lag — watch `pending`
> XRANGE kos:events:dead - + COUNT 10 # what failed permanently
```

**Watch metrics without Grafana:**

```bash
curl -s localhost:8002/metrics | grep knowledgeos_http_requests_total
```

## Tests

```bash
pytest                                    # everything
pytest apps/books/tests -v                # one service
pytest -m "not integration"               # skip tests needing live infrastructure
pytest --cov --cov-report=html            # coverage → htmlcov/index.html

pnpm test                                 # Vitest
pnpm --filter @knowledgeos/frontend test:e2e   # Playwright
```

Authenticating in a test does not require minting a real token:

```python
from knowledgeos_core.testing import authenticate_as, make_principal

authenticate_as(app, make_principal(roles=["admin"], permissions=["books:delete"]))
```

## Common problems

**`pnpm install` fails on `workspace:*`** — you are not using pnpm. npm and yarn do
not understand the workspace protocol.

**`connection refused` to Postgres/Redis** — the stack is not up, or is still
starting. `docker compose ps` shows health status; wait for `healthy`.

**`JWKS_URL` errors on startup** — the auth service is not running. Start it first,
or start the service you are working on without `Components(auth=True)`.

**Presigned URL works in tests but 404s in the browser** — `S3_PUBLIC_ENDPOINT_URL`
is set to the internal hostname. It must be what a browser can resolve.

**Alembic autogenerate wants to drop other services' tables** — the `include_object`
filter in `env.py` is missing or wrong. Do not apply that migration.

**Port already in use** — something else is on 8000/3000/5432. Override with
`POSTGRES_PORT=5433 pnpm stack:up`, or change the service's `port` setting.

More in [troubleshooting.md](troubleshooting.md).

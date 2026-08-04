# Railway Deployment

One Railway **project** contains every KnowledgeOS service plus its managed
datastores. Services talk to each other over Railway's private network and only the
gateway and frontend are exposed publicly.

## Topology

```
                    ┌─────────────── public internet ───────────────┐
                    │                                               │
              frontend.up.railway.app                    api.up.railway.app
                    │                                               │
              ┌─────▼─────┐                                  ┌──────▼──────┐
              │ frontend  │──────── HTTPS ──────────────────▶│   gateway   │
              │ (Next.js) │                                  │  (FastAPI)  │
              └───────────┘                                  └──────┬──────┘
                                                                    │
   ═══════════════ Railway private network (*.railway.internal) ════╪═════════════
                                                                    │
   ┌──────────┬──────────┬───────────┬──────────┬─────────┬─────────┴──┬──────────┐
   │   auth   │  books   │  search   │    ai    │ payment │notification│  admin   │
   └────┬─────┴────┬─────┴─────┬─────┴────┬─────┴────┬────┴──────┬─────┴────┬─────┘
        │          │           │          │          │           │          │
   ┌────▼──────────▼───────────▼──────────▼──────────▼───────────▼──────────▼────┐
   │  Postgres        Redis          MinIO           Meilisearch                 │
   └──────────────────────────────────────────────────────────────────────────────┘
                                     ▲
                          ┌──────────┴──────────┐
                          │ automation │ workers│   (Celery — no public ingress)
                          └─────────────────────┘
```

## Why one project rather than one per service

Railway's private network is **project-scoped**. Splitting services across projects
would force every internal call out over the public internet — slower, billed as
egress, and requiring each service to be publicly reachable. One project keeps
`auth.railway.internal` resolvable and lets the datastores stay private.

## Private networking

Railway gives every service a private DNS name: `<service-name>.railway.internal`.
Set the discovery variables to those names so no internal traffic leaves the project:

```
AUTH_SERVICE_URL=http://auth.railway.internal:8000
BOOKS_SERVICE_URL=http://books.railway.internal:8000
SEARCH_SERVICE_URL=http://search.railway.internal:8000
AI_SERVICE_URL=http://ai.railway.internal:8000
PAYMENT_SERVICE_URL=http://payment.railway.internal:8000
NOTIFICATION_SERVICE_URL=http://notifications.railway.internal:8000
AUTOMATION_SERVICE_URL=http://automation.railway.internal:8000
ADMIN_SERVICE_URL=http://admin.railway.internal:8000
```

Two things to know about it:

- Private networking is **IPv6 only**. Services must bind `::` or `0.0.0.0` — the
  platform binds dual-stack by default via `HOST=0.0.0.0`, which Railway maps
  correctly. Binding `127.0.0.1` makes a service unreachable from its siblings.
- It takes a few seconds to become available after a deploy. Services that call a
  sibling during startup must tolerate a brief failure — which is why dependency
  checks live in `/health/ready` (retried) rather than in the startup path (fatal).

## Environment variables

Railway supports **reference variables**, which are the correct way to wire shared
config. Set the value once on the datastore and reference it everywhere:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
```

Shared application secrets go in a **shared variable** at the project level so
rotating one value updates every service:

```
INTERNAL_API_SECRET=${{shared.INTERNAL_API_SECRET}}
JWT_PRIVATE_KEY=${{shared.JWT_PRIVATE_KEY}}
```

`DATABASE_URL` arrives as `postgres://…`; `ServiceSettings` rewrites the scheme to
`postgresql+asyncpg://` automatically, so paste Railway's value unchanged.

See [`../../docs/MANUAL_SETUP.md`](../../docs/MANUAL_SETUP.md) for the full list of
values you must supply yourself (API keys, OAuth credentials, signing keys).

## Per-service configuration

Every service directory contains a `railway.json`. Point the Railway service at it
with **Config as code** → path `apps/<service>/railway.json`, and set the service's
root directory to the repository root so the Docker build can see `packages/core-py`.

```jsonc
{
  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "apps/books/Dockerfile"   // context is the repo root
  },
  "deploy": {
    "healthcheckPath": "/health",               // liveness, not readiness
    "healthcheckTimeout": 30,
    "restartPolicyType": "ON_FAILURE",
    "numReplicas": 2
  }
}
```

### Why `/health` and not `/health/ready`

Railway restarts a container that fails its healthcheck. `/health/ready` reports
dependency state, so pointing the healthcheck at it means a 30-second Postgres
failover restarts every container in the project — turning a brief degradation into
a full outage. `/health` answers only "is this process alive", which is the
question a restart can actually fix.

## Volumes

Only stateful services need one:

| Service | Mount | Purpose |
|---|---|---|
| `minio` | `/data` | Book files, covers, generated assets |
| `postgres` | managed | Railway-managed volume |
| `redis` | managed | AOF persistence for queued jobs |
| `meilisearch` | `/meili_data` | Search index (rebuildable from Postgres) |

Application services are stateless and must stay that way — anything written to a
container's filesystem is lost on redeploy. The automation pipeline writes
intermediate files to `/tmp` and uploads results to object storage before the task
acknowledges.

## Deploy order (first time only)

Datastores must be healthy before services boot, and the auth service must publish
its JWKS before any service can verify a token:

1. `postgres`, `redis`, `minio`, `meilisearch`
2. `auth` — run migrations, confirm `/.well-known/jwks.json` responds
3. `books`, `search`, `ai`, `payment`, `notifications`, `automation`, `admin`
4. `workers` (Celery)
5. `gateway`
6. `frontend`

Subsequent deploys have no ordering requirement: services degrade gracefully when a
dependency is briefly unavailable rather than crash-looping.

## Scaling

Railway scales horizontally by replica count. Recommended starting points and the
signal to scale on:

| Service | Replicas | Scale when |
|---|---|---|
| gateway | 2–4 | p95 latency, it fronts everything |
| frontend | 2–3 | SSR CPU |
| auth | 2 | login latency; bcrypt is deliberately CPU-bound |
| books | 2–4 | the highest-traffic API |
| search | 2 | query volume |
| ai | 1–2 | usually blocked on provider latency, not CPU |
| payment | 2 | keep ≥2 so webhooks are never dropped during a deploy |
| notifications | 1–2 | queue depth |
| automation | 1–2 | HTTP surface only; the work happens in workers |
| workers | 2–6 | **`knowledgeos_queue_depth`** — the real signal |
| admin | 1 | low traffic |

Scale workers on queue depth, not CPU: a worker waiting on an OpenAI response looks
idle while the automation backlog grows.

Vertical sizing matters most for `workers` — PDF conversion and image processing are
memory-hungry, and `worker_max_memory_per_child` recycles a process at 512MB, so
give the container at least 2GB.

## Cost control

- Meilisearch and MinIO can run as Railway services from their public images; both
  need a volume.
- Set `numReplicas: 1` for everything in a staging environment.
- Railway bills for active CPU/memory, so the biggest lever is not over-provisioning
  worker replicas that sit idle. Prefer fewer replicas with higher Celery
  concurrency for IO-bound queues (`ai`, `notifications`), and more replicas with
  low concurrency for CPU-bound ones (`automation`).

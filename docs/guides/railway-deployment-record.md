# Railway Deployment Record

The live deployment of this repository, and the problems hit getting there. Kept
because every one of these cost real debugging time and none is obvious from the
Railway docs.

## The project

| | |
|---|---|
| Project | `knowledgeos` |
| Workspace | Project KGF |
| Environment | `production` |
| Repo / branch | `getkcoin-alt/ebook` · `claude/knowledgeos-ebook-platform-by63ba` |
| Public URL | `https://gateway-production-c3e0.up.railway.app` |

## Services

| Service | Source | Purpose |
|---|---|---|
| `Postgres` | `ghcr.io/railwayapp-templates/postgres-ssl:17` | One database, schema per service |
| `redis` | `redis:7-alpine` | Cache, rate limits, event bus, Celery broker |
| `auth` | repo · `apps/auth/Dockerfile` | Identity, tokens, JWKS |
| `gateway` | repo · `apps/gateway/Dockerfile` | **The only public ingress** |

Everything else reaches its dependencies over `*.railway.internal`. Only `gateway`
has a public domain.

## Variable strategy

**Project-level shared variables** (set once, referenced everywhere): the service
discovery URLs, `INTERNAL_API_SECRET`, `JWT_ISSUER`/`JWT_AUDIENCE`, `JWKS_URL`,
`ENVIRONMENT=production`, `LOG_FORMAT=json`.

**Per-service**, using Railway's reference syntax so a rotated password propagates
without editing every service:

```
DATABASE_URL = ${{Postgres.DATABASE_URL}}
REDIS_URL    = ${{redis.REDIS_URL}}
```

`JWT_PRIVATE_KEY` is set on **`auth` only**. It is the one secret whose leak would
compromise the entire platform.

## Problems hit, and the fixes

### 1. `bitnami/redis:7.4` does not exist

Deployment failed at `CREATE_CONTAINER`:
`The image "docker.io/bitnami/redis:7.4" could not be found.`

Bitnami deprecated their public Docker Hub images. Worse, after changing the service's
image in config, Railway kept redeploying the **old** image reference — the staged
source change never took effect on a redeploy.

**Fix:** create a fresh service with `redis:7-alpine` rather than fight the stale
config. Changing a service's source image in place is unreliable; recreating is not.

A leftover broken `Redis` (capital R) service remains in the project. There is no
delete-service API in this toolset — **remove it from the Railway dashboard.**

### 2. Dockerfiles failed to *parse* — the expensive one

Symptom: `BUILD_IMAGE` failed after **two seconds** with a completely empty build
log. Only `scheduling build on Metal builder` appeared.

That signature — instant failure, no output — means the Dockerfile never parsed.
A Dockerfile that fails to build produces logs; one that fails to parse does not.

Cause: three BuildKit frontend features Railway's builder does not accept:

```dockerfile
# syntax=docker/dockerfile:1.7               ← rejected
RUN --mount=type=cache,target=/root/.cache/pip …   ← rejected
RUN python - <<'PY'                          ← heredoc, rejected
```

The heredoc was extracting dependencies from `pyproject.toml` to install them before
copying source, preserving the layer cache.

**Fix:** plain Dockerfile syntax, with the same caching benefit achieved differently
— copy only the manifests, create a **stub package** so pip can resolve core-py's
dependencies without its source, then copy the real source and reinstall:

```dockerfile
COPY packages/core-py/pyproject.toml packages/core-py/
COPY apps/auth/requirements.txt apps/auth/
RUN mkdir -p packages/core-py/knowledgeos_core \
    && touch packages/core-py/knowledgeos_core/__init__.py
RUN pip install ./packages/core-py && pip install -r apps/auth/requirements.txt

COPY packages/core-py/ packages/core-py/
RUN pip install --no-deps --force-reinstall ./packages/core-py
```

hatchling needs the package directory to exist, not to be complete.

### 3. Service config must be set before the first deploy

`create-deployment` triggers a build **immediately**, before there is any chance to
set `dockerfilePath`. That first build always fails on a monorepo, because Railway
falls back to auto-detection and finds no project manifest at the root.

Harmless — but it means the first `FAILED` on a new service is expected. Set the
config, then redeploy.

### 4. Build context must be the repo root

The Dockerfiles copy both `apps/<service>/` and `packages/core-py/`, so the context
has to be the repository root. **Leave the service's root directory unset.** Setting
it to `apps/auth` puts `packages/core-py` outside the context and the `COPY` fails.

`dockerfilePath` is therefore repo-relative: `apps/auth/Dockerfile`.

## Still to do on this deployment

- Delete the leftover broken `Redis` service in the dashboard.
- Add MinIO and Meilisearch services when the books and search services deploy.
- Run `alembic upgrade head` against the real Postgres — the auth service's
  `railway.json` does this in its `startCommand`, but it has not yet been observed
  succeeding against PostgreSQL (migrations were verified against SQLite only).
- Set `CORS_ORIGINS` and `TRUSTED_HOSTS` to real hostnames once a frontend domain
  exists. They are currently permissive.
- Supply the third-party keys in [`../MANUAL_SETUP.md`](../MANUAL_SETUP.md) — OAuth,
  payments, AI and email are all unconfigured, so those features degrade off.

## Verifying a deploy

```bash
curl https://gateway-production-c3e0.up.railway.app/health
curl https://gateway-production-c3e0.up.railway.app/health/ready   # per-upstream state
curl https://gateway-production-c3e0.up.railway.app/docs           # aggregated API docs
```

`/health/ready` naming each upstream is the fastest way to see which service is down.

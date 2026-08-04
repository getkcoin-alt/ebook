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

**Current state:** `gateway` and `redis` deploy successfully. `auth` cannot start
because Postgres is not accepting connections — see problem 5 below, which needs a
dashboard action.

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

### 5. Postgres never started — no volume attached (UNRESOLVED, needs you)

`ghcr.io/railwayapp-templates/postgres-ssl` **refuses to start without a volume**
mounted at exactly `/var/lib/postgresql/data`. Without one it loops:

```
Railway volume not mounted to the correct path, expected /var/lib/postgresql/data but got
Please update the volume mount path to the expected path and redeploy the service
```

The deployment still reports **SUCCESS**, because the container did start — it is
just refusing to run Postgres. That is a genuinely misleading status: the only way to
notice is to read the logs or watch dependent services time out.

Downstream symptom, from the auth service:

```
sqlalchemy.exc.OperationalError: (psycopg.errors.ConnectionTimeout) connection timeout expired
- host: 'postgres.railway.internal', port: 5432 : connection timeout expired
```

DNS resolved fine (both an IPv6 and IPv4 address came back) — nothing was listening.

**Attempted fix that did not work.** Setting `volumeMounts` through the API persists
in the service config, and `get-service-config` reports it correctly, but the volume
is never actually provisioned: `hasVolume` stays `false` and the container still sees
no mount across repeated redeploys.

**Action required in the Railway dashboard:**

1. Open the `Postgres` service → **Variables/Settings → Volumes → Add Volume**
2. Mount path: `/var/lib/postgresql/data` (10GB is ample)
3. Redeploy

`PGDATA` is already set to `/var/lib/postgresql/data/pgdata`, a subdirectory of the
mount — that is deliberate and correct. Postgres refuses to initialise into a
directory containing `lost+found`, which a fresh volume root has.

Confirm success by looking for `database system is ready to accept connections` in
the Postgres logs, after which `auth` will start on its next deploy.

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

## A note on verification from a sandbox

The build environment's egress proxy blocks `*.up.railway.app`:

```
connect_rejected — gateway answered 403 to CONNECT (policy denial)
```

So the public URL could not be curled from where this was built. The evidence that
`gateway` is serving is Railway's own healthcheck against `/health` passing, which is
what promotes a deployment to SUCCESS — a real signal, but Railway's assertion rather
than a direct observation. **Check the URL from a browser to confirm.**

## Verifying a deploy

```bash
curl https://gateway-production-c3e0.up.railway.app/health
curl https://gateway-production-c3e0.up.railway.app/health/ready   # per-upstream state
curl https://gateway-production-c3e0.up.railway.app/docs           # aggregated API docs
```

`/health/ready` naming each upstream is the fastest way to see which service is down.

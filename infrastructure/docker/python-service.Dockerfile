# syntax=docker/dockerfile:1.7
#
# Canonical build for every KnowledgeOS Python service.
#
# Build context is the REPOSITORY ROOT, because a service needs both its own source
# and the shared `packages/core-py` library. Select the service with a build arg:
#
#   docker build -f infrastructure/docker/python-service.Dockerfile \
#                --build-arg SERVICE=books -t knowledgeos/books .
#
# Each service also has its own thin apps/<svc>/Dockerfile that delegates here, so
# Railway can point one service at one Dockerfile path and deploy it independently.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: builder — compile wheels into a virtualenv.
# Kept separate so compilers and headers never reach the runtime image.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ARG SERVICE
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Build into a self-contained venv that stage 3 copies wholesale.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Dependency manifests first, source later: editing a .py file must not invalidate
# the (slow) dependency layer.
COPY packages/core-py/pyproject.toml packages/core-py/
COPY apps/${SERVICE}/requirements.txt apps/${SERVICE}/

# Install the core package's dependency set without the package itself, so the
# resolved wheel layer stays cached across core source edits.
RUN --mount=type=cache,target=/root/.cache/pip \
    python - <<'PY' > /tmp/core-deps.txt
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("packages/core-py/pyproject.toml").read_text())
print("\n".join(data["project"]["dependencies"]))
PY

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel \
    && pip install -r /tmp/core-deps.txt \
    && pip install -r apps/${SERVICE}/requirements.txt

# Now the sources. `--no-deps` because everything is already resolved above.
COPY packages/core-py/ packages/core-py/
RUN pip install --no-deps ./packages/core-py

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim, non-root, no build tooling.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG SERVICE
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="knowledgeos-${SERVICE}" \
      org.opencontainers.image.source="https://github.com/getkcoin-alt/ebook" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    SERVICE_NAME=${SERVICE} \
    SERVICE_VERSION=${GIT_SHA}

# Runtime libraries only:
#   libpq5      - Postgres client (psycopg for Alembic)
#   libmagic1   - real content-type sniffing for uploads
#   curl        - container healthcheck
# Services needing more (poppler, ffmpeg, calibre) add them in their own Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libmagic1 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --shell /usr/sbin/nologin --create-home app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app packages/core-py/ /app/packages/core-py/
COPY --chown=app:app apps/${SERVICE}/ /app/apps/${SERVICE}/

# Prometheus multiprocess needs a writable dir when more than one worker runs.
RUN mkdir -p /tmp/prometheus /app/tmp && chown -R app:app /tmp/prometheus /app/tmp

# Never run as root: a container escape should not start from uid 0.
USER app

ENV PYTHONPATH=/app/apps/${SERVICE}:/app \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    PORT=8000

EXPOSE 8000

# Liveness only — deliberately not /health/ready, or a brief Postgres blip would
# make Docker restart every container instead of just draining them.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# tini reaps zombies and forwards SIGTERM, which is what makes the app's graceful
# shutdown path actually run on `docker stop` and on a Railway redeploy.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "main"]

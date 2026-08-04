# syntax=docker/dockerfile:1.7
#
# Authentication service.
#
# Build context is the REPOSITORY ROOT — the image needs both this service and the
# shared packages/core-py library:
#
#   docker build -f apps/gateway/Dockerfile -t knowledgeos/gateway .

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: builder. Compilers and headers stay here and never reach runtime.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Dependency manifests first: editing a .py file must not invalidate the slow
# dependency layer.
COPY packages/core-py/pyproject.toml packages/core-py/
COPY apps/gateway/requirements.txt apps/gateway/

RUN --mount=type=cache,target=/root/.cache/pip \
    python - <<'PY' > /tmp/core-deps.txt
import pathlib, tomllib
data = tomllib.loads(pathlib.Path("packages/core-py/pyproject.toml").read_text())
print("\n".join(data["project"]["dependencies"]))
PY

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel \
    && pip install -r /tmp/core-deps.txt \
    && pip install -r apps/gateway/requirements.txt

COPY packages/core-py/ packages/core-py/
RUN pip install --no-deps ./packages/core-py

# ---------------------------------------------------------------------------
# Stage 2: runtime. Slim, non-root, no build tooling.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="knowledgeos-gateway" \
      org.opencontainers.image.description="Public API gateway: routing, rate limiting, edge auth and caching" \
      org.opencontainers.image.source="https://github.com/getkcoin-alt/ebook" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    SERVICE_NAME=gateway \
    SERVICE_VERSION=${GIT_SHA} \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --shell /usr/sbin/nologin --create-home app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app packages/core-py/ /app/packages/core-py/
COPY --chown=app:app apps/gateway/ /app/apps/gateway/

RUN mkdir -p /tmp/prometheus && chown -R app:app /tmp/prometheus

# Never run as root: a container escape should not begin at uid 0. This matters
# more here than anywhere else — this process holds the token-signing key.
USER app

WORKDIR /app/apps/gateway
ENV PYTHONPATH=/app/apps/gateway:/app \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

EXPOSE 8000

# Liveness only. Pointing this at /health/ready would make a brief Postgres blip
# restart every replica instead of just draining them.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# tini reaps zombies and forwards SIGTERM, which is what makes the app's graceful
# shutdown actually run on `docker stop` and on a Railway redeploy.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "main"]

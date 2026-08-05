# AI service.
#
# Build context is the REPOSITORY ROOT — the image needs both this service and the
# shared packages/core-py library:
#
#   docker build -f apps/ai/Dockerfile -t knowledgeos/ai .
#
# Deliberately plain Dockerfile syntax: no heredocs, no `--mount=type=cache`, no
# `# syntax=` directive. Those require BuildKit frontend features that not every
# builder enables — Railway's rejects them, and a Dockerfile that fails to *parse*
# dies in two seconds with an empty build log, which is a miserable thing to debug.

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

# Dependency manifests first, so editing a .py file does not invalidate the slow
# dependency layer. A stub package directory lets pip resolve and install
# core-py's dependencies before its source exists — hatchling needs the package
# directory to be present, but not to be complete.
COPY packages/core-py/pyproject.toml packages/core-py/
COPY apps/ai/requirements.txt apps/ai/
RUN mkdir -p packages/core-py/knowledgeos_core \
    && touch packages/core-py/knowledgeos_core/__init__.py

RUN pip install --upgrade pip setuptools wheel \
    && pip install ./packages/core-py \
    && pip install -r apps/ai/requirements.txt

# Now the real source. --no-deps because everything is resolved above;
# --force-reinstall replaces the stub with the actual package.
COPY packages/core-py/ packages/core-py/
RUN pip install --no-deps --force-reinstall ./packages/core-py

# ---------------------------------------------------------------------------
# Stage 2: runtime. Slim, non-root, no build tooling.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="knowledgeos-ai" \
      org.opencontainers.image.description="Generated copy, catalogue-grounded chat, moderation and embeddings" \
      org.opencontainers.image.source="https://github.com/getkcoin-alt/ebook" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    SERVICE_NAME=ai \
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
COPY --chown=app:app apps/ai/ /app/apps/ai/

RUN mkdir -p /tmp/prometheus && chown -R app:app /tmp/prometheus

# Never run as root: a container escape should not begin at uid 0. This container
# holds the model provider API keys, which are billed per token — a stolen one is a
# bill that grows until someone notices.
USER app

WORKDIR /app/apps/ai
ENV PYTHONPATH=/app/apps/ai:/app \
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

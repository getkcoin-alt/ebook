"""FastAPI application factory.

Every KnowledgeOS service is created through :func:`create_app`, which guarantees a
uniform runtime: structured logging, the platform error envelope, the middleware
stack, health/readiness/metrics endpoints, graceful shutdown, and lazily-constructed
handles for Postgres, Redis, object storage and sibling services.

A service opts into the dependencies it needs::

    app = create_app(
        settings=settings,
        components=Components(database=True, redis=True, storage=True),
        routers=[books_router, reviews_router],
    )
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI

from .config import ServiceSettings
from .db import Database
from .errors import install_exception_handlers
from .events import EventConsumer, EventPublisher
from .health import HealthState, build_health_router
from .http import ServiceRegistry
from .idempotency import IdempotencyStore
from .logging import configure_logging, get_logger
from .metrics import build_timestamp_seconds, service_info
from .middleware import install_middleware
from .ratelimit import RateLimiter
from .redis import RedisClient
from .security import TokenVerifier
from .storage import ObjectStorage

logger = get_logger(__name__)

LifecycleHook = Callable[["AppContext"], Awaitable[None]]


@dataclass(slots=True)
class Components:
    """Which shared dependencies this service needs."""

    database: bool = False
    redis: bool = False
    storage: bool = False
    auth: bool = False  # verify inbound access tokens
    events: bool = False  # publish domain events
    event_consumer: str | None = None  # consumer group name; enables consumption
    service_clients: bool = False  # call sibling services


@dataclass
class AppContext:
    """Runtime handles, reachable from any request via ``request.app.state.ctx``."""

    settings: ServiceSettings
    health: HealthState
    database: Database | None = None
    redis: RedisClient | None = None
    storage: ObjectStorage | None = None
    verifier: TokenVerifier | None = None
    publisher: EventPublisher | None = None
    consumer: EventConsumer | None = None
    services: ServiceRegistry | None = None
    limiter: RateLimiter | None = None
    idempotency: IdempotencyStore | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    # Accessors that fail loudly rather than returning None into a handler, so a
    # misconfigured service surfaces at the call site instead of as an AttributeError.
    def require_db(self) -> Database:
        if self.database is None:
            raise RuntimeError("This service was not created with Components(database=True).")
        return self.database

    def require_redis(self) -> RedisClient:
        if self.redis is None:
            raise RuntimeError("This service was not created with Components(redis=True).")
        return self.redis

    def require_storage(self) -> ObjectStorage:
        if self.storage is None:
            raise RuntimeError("This service was not created with Components(storage=True).")
        return self.storage

    def require_publisher(self) -> EventPublisher:
        if self.publisher is None:
            raise RuntimeError("This service was not created with Components(events=True).")
        return self.publisher

    def require_services(self) -> ServiceRegistry:
        if self.services is None:
            raise RuntimeError(
                "This service was not created with Components(service_clients=True)."
            )
        return self.services


def create_app(
    *,
    settings: ServiceSettings,
    components: Components | None = None,
    routers: Sequence[APIRouter] = (),
    on_startup: Sequence[LifecycleHook] = (),
    on_shutdown: Sequence[LifecycleHook] = (),
    description: str | None = None,
    configure: Callable[[FastAPI, AppContext], None] | None = None,
) -> FastAPI:
    """Build a fully-wired FastAPI application."""
    components = components or Components()

    configure_logging(
        service_name=settings.service_name,
        level=settings.log_level,
        fmt=settings.log_format,
        version=settings.service_version,
        environment=settings.environment,
    )

    health_state = HealthState()
    ctx = AppContext(settings=settings, health=health_state)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = time.perf_counter()
        # Clear any state left by a previous run of this same app object.
        health_state.mark_starting()
        logger.info(
            "service.starting",
            service=settings.service_name,
            version=settings.service_version,
            environment=settings.environment,
        )

        if components.database:
            ctx.database = Database(settings)
            await ctx.database.ensure_schema()
            health_state.register("database", ctx.database.healthcheck)

        if components.redis or components.events or components.event_consumer:
            ctx.redis = RedisClient(settings)
            health_state.register("redis", ctx.redis.healthcheck)
            ctx.limiter = RateLimiter(ctx.redis.client, service_name=settings.service_name)
            ctx.idempotency = IdempotencyStore(ctx.redis.client, service_name=settings.service_name)

        if components.storage:
            ctx.storage = ObjectStorage(settings)
            await ctx.storage.ensure_bucket()
            # Storage being down should not pull the whole API out of rotation —
            # most endpoints do not touch it. Marked optional for readiness.
            health_state.register("storage", ctx.storage.healthcheck, required=False)

        if components.auth:
            if not settings.jwks_url:
                raise RuntimeError(
                    f"{settings.service_name}: JWKS_URL must be set when auth is enabled."
                )
            ctx.verifier = TokenVerifier(
                jwks_url=settings.jwks_url,
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
                algorithms=(settings.jwt_algorithm,),
                cache_ttl=settings.jwks_cache_ttl,
            )

        if components.events and ctx.redis:
            ctx.publisher = EventPublisher(ctx.redis.client, source=settings.service_name)

        if components.event_consumer and ctx.redis:
            ctx.consumer = EventConsumer(ctx.redis.client, group=components.event_consumer)

        if components.service_clients:
            ctx.services = ServiceRegistry(settings)

        app.state.ctx = ctx
        if configure is not None:
            configure(app, ctx)

        for hook in on_startup:
            await hook(ctx)

        # Consumers start last so handlers only run against a fully-initialised app.
        if ctx.consumer is not None:
            await ctx.consumer.start()

        service_info.labels(
            service=settings.service_name,
            version=settings.service_version,
            environment=settings.environment,
        ).set(1)
        build_timestamp_seconds.labels(service=settings.service_name).set(time.time())

        health_state.mark_started()
        logger.info("service.started", boot_ms=round((time.perf_counter() - started) * 1000, 2))

        try:
            yield
        finally:
            # --- graceful shutdown -------------------------------------
            # Fail readiness first and pause, so the load balancer stops routing new
            # requests to this instance before we tear anything down. Skipping this
            # is what turns a rolling deploy into a burst of 502s.
            health_state.begin_shutdown()
            if settings.drain_delay_seconds > 0 and settings.is_production:
                await asyncio.sleep(settings.drain_delay_seconds)

            for hook in on_shutdown:
                try:
                    await hook(ctx)
                except Exception:
                    logger.exception("service.shutdown_hook_failed")

            if ctx.consumer is not None:
                await ctx.consumer.stop()
            if ctx.services is not None:
                await ctx.services.aclose()
            if ctx.redis is not None:
                await ctx.redis.close()
            if ctx.database is not None:
                await ctx.database.dispose()
            logger.info("service.stopped", uptime_seconds=round(health_state.uptime, 2))

    app = FastAPI(
        title=f"KnowledgeOS {settings.service_name.replace('-', ' ').title()}",
        description=description or f"KnowledgeOS {settings.service_name} service API.",
        version=settings.service_version,
        lifespan=lifespan,
        # No custom response class: FastAPI >=0.140 serialises route returns to JSON
        # bytes directly through Pydantic, which is faster than routing them through
        # ORJSONResponse and is the path it now optimises.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        root_path=settings.root_path,
        # Trailing-slash redirects break signed POSTs and leak query strings through
        # the Location header, so route matching is exact.
        redirect_slashes=False,
        contact={"name": "KnowledgeOS Platform", "url": str(settings.frontend_url)},
        license_info={"name": "Proprietary"},
    )

    install_middleware(app, settings)
    install_exception_handlers(app)

    app.include_router(
        build_health_router(
            state=health_state,
            service_name=settings.service_name,
            version=settings.service_version,
            environment=settings.environment,
            metrics_enabled=settings.metrics_enabled,
        )
    )
    for router in routers:
        app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "service": settings.service_name,
            "version": settings.service_version,
            "status": "ok",
            "docs": "/docs" if settings.docs_enabled else None,
        }

    return app


def run(app_path: str, settings: ServiceSettings) -> None:
    """Entrypoint used by each service's ``__main__``.

    Uvicorn is invoked programmatically so timeouts and the signal set come from
    settings rather than a CLI string duplicated across eleven Dockerfiles.
    """
    import uvicorn

    uvicorn.run(
        app_path,
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "local",
        log_config=None,  # structlog owns logging
        access_log=False,  # our middleware emits richer access lines
        timeout_graceful_shutdown=settings.graceful_shutdown_timeout,
        # Railway's proxy terminates TLS and sets X-Forwarded-*; without this the
        # app sees the proxy's IP and http:// scheme, breaking rate limits and
        # redirect URLs.
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,  # do not advertise the server version
        date_header=True,
    )


def install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """For non-HTTP processes (Celery-adjacent loops) that need SIGTERM handling."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler is not implemented on Windows; local dev there falls
        # back to KeyboardInterrupt, which is fine for a non-deployed platform.
        with contextlib.suppress(NotImplementedError):  # pragma: no cover
            loop.add_signal_handler(sig, shutdown_event.set)

"""Configuration primitives shared by every KnowledgeOS service.

Every service subclasses :class:`ServiceSettings` and adds its own fields. The base
class owns everything the platform guarantees: identity, database, redis, telemetry,
auth and inter-service trust.

Railway injects most of these as environment variables; ``.env`` is only read for
local development.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, BeforeValidator, Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _split_csv(value: Any) -> Any:
    """Allow list-typed settings to be provided as comma-separated env strings."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):  # already JSON
            return value
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], BeforeValidator(_split_csv)]


class ServiceSettings(BaseSettings):
    """Base settings every service inherits.

    Subclasses set ``service_name`` as a class-level default and may override
    ``model_config`` to point at a different ``env_prefix``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- identity -------------------------------------------------------
    service_name: str = "knowledgeos-service"
    service_version: str = "0.1.0"
    environment: Environment = "local"
    debug: bool = False

    # ---- http server ----------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - containers must bind all interfaces
    port: int = 8000
    root_path: str = ""
    #: Seconds to keep serving in-flight requests after SIGTERM.
    graceful_shutdown_timeout: int = 25
    #: Seconds to keep failing readiness before shutdown, so load balancers drain.
    drain_delay_seconds: float = 3.0

    # ---- observability --------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: Literal["json", "console"] = "json"
    metrics_enabled: bool = True
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None

    # ---- datastores -----------------------------------------------------
    database_url: str | None = None
    #: Postgres schema this service owns. Enforces service data isolation.
    database_schema: str = "public"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_echo: bool = False

    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50

    # ---- auth -----------------------------------------------------------
    #: Public JWKS endpoint of the auth service; used to verify RS256 access tokens.
    jwks_url: str | None = None
    jwt_issuer: str = "knowledgeos-auth"
    jwt_audience: str = "knowledgeos-api"
    jwt_algorithm: str = "RS256"
    jwks_cache_ttl: int = 600

    # ---- inter-service trust -------------------------------------------
    #: Shared secret used to sign service-to-service calls (never exposed publicly).
    internal_api_secret: str = Field(default="dev-internal-secret-change-me")
    internal_request_timeout: float = 15.0

    # ---- networking -----------------------------------------------------
    cors_origins: CsvList = Field(default_factory=lambda: ["http://localhost:3000"])
    cors_allow_credentials: bool = True
    trusted_hosts: CsvList = Field(default_factory=lambda: ["*"])
    #: Max request body accepted before returning 413, in bytes.
    max_request_body_bytes: int = 25 * 1024 * 1024

    # ---- object storage -------------------------------------------------
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "knowledgeos"
    s3_use_ssl: bool = True
    s3_signature_ttl: int = 900

    # ---- service discovery ---------------------------------------------
    # On Railway these resolve over the private network, e.g.
    #   http://auth.railway.internal:8000
    auth_service_url: str = "http://localhost:8001"
    books_service_url: str = "http://localhost:8002"
    search_service_url: str = "http://localhost:8003"
    ai_service_url: str = "http://localhost:8004"
    payment_service_url: str = "http://localhost:8005"
    notification_service_url: str = "http://localhost:8006"
    automation_service_url: str = "http://localhost:8007"
    admin_service_url: str = "http://localhost:8008"

    frontend_url: AnyHttpUrl | str = "http://localhost:3000"

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalise_driver(cls, value: str | None) -> str | None:
        """Railway/Heroku hand out ``postgres://``; SQLAlchemy async needs asyncpg."""
        if not value:
            return value
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str | None:
        """Alembic and Celery beat need a blocking driver."""
        if not self.database_url:
            return None
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def docs_enabled(self) -> bool:
        """Swagger is closed in production; the gateway serves an aggregated spec."""
        return not self.is_production


@functools.lru_cache(maxsize=1)
def get_settings(cls: type[ServiceSettings] = ServiceSettings) -> ServiceSettings:
    """Process-wide settings singleton. Cached so env parsing happens once."""
    return cls()

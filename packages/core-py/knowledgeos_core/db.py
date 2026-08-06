"""Async SQLAlchemy engine, session factory and declarative base.

Topology: one PostgreSQL database, one **schema per service**. Services never read
each other's tables — cross-service data is fetched over HTTP or consumed from a
Redis Stream. The schema boundary makes that rule enforceable with grants rather
than convention.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, MetaData, Uuid, func, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import ServiceSettings
from .errors import ServiceUnavailableError
from .logging import get_logger

logger = get_logger(__name__)

# Deterministic constraint names keep Alembic autogenerate diffs stable and make
# `ALTER TABLE ... DROP CONSTRAINT` scriptable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base. Each service sets ``metadata.schema`` at import time."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Fetch server-generated values (``server_default``, ``onupdate=func.now()``) as
    # part of the INSERT/UPDATE via RETURNING, instead of expiring the attribute and
    # reloading it on next access.
    #
    # Without this, reading ``updated_at`` straight after a write raises
    # `MissingGreenlet: greenlet_spawn has not been called` — the expired attribute
    # wants a lazy SELECT, and under asyncio that emits IO from a synchronous
    # context, typically during response serialisation. PostgreSQL supports
    # RETURNING, so this costs nothing.
    # ClassVar so ruff does not read this as a dataclass-style mutable default; it is
    # SQLAlchemy configuration, and subclasses that override it replace it wholesale.
    # `ClassVar` and mypy disagree here and cannot both be satisfied: ruff reads a
    # bare dict as a mutable class default (RUF012) and wants the annotation, while
    # `DeclarativeBase` declares `__mapper_args__` as an instance variable, so
    # narrowing it to a ClassVar is an illegal override. The annotation is the more
    # accurate of the two — this is configuration read off the class, never per
    # instance — so it stays and the override check is silenced.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}  # type: ignore[misc]


#: Dialect-agnostic UUID column type.
#:
#: ``sqlalchemy.Uuid`` renders as PostgreSQL's native ``UUID`` in production and as
#: ``CHAR(32)`` on SQLite. Using the postgresql-specific type here instead would make
#: every model unusable on SQLite, which in turn would mean no service could be
#: tested without a live database — a cost paid on every CI run and every laptop.
UUIDType = Uuid(as_uuid=True)


class UUIDPrimaryKeyMixin:
    """UUIDv4 primary keys, generated application-side.

    Generating in Python (rather than ``gen_random_uuid()``) means an object has its
    identity before it is flushed, which lets us publish events and build URLs inside
    the same transaction that creates the row.
    """

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=uuid.uuid4)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """``created_at`` / ``updated_at``.

    Both carry a **client-side default as well as a server default**. The server
    default is the safety net for rows written outside the ORM (a migration, a
    manual fix); the client-side default is what the ORM actually uses, and it is
    there for two reasons:

    * The value exists on the object before it is flushed, so an event payload or
      a response can carry it inside the same transaction that created the row —
      the same rationale as generating UUID primary keys in Python.
    * It makes the stored representation match the one later comparisons bind.
      SQLite stores ``CURRENT_TIMESTAMP`` as ``2026-08-04 21:05:24`` and binds a
      Python datetime as ``2026-08-04 21:05:24.000000``; since SQLite compares
      timestamps as strings, the stored value sorts *before* every bound value and
      a keyset cursor (``WHERE created_at < :cursor``) matches every row —
      returning page one forever. Postgres compares real timestamps and is
      unaffected, which is exactly why this was invisible until a paging test ran
      against the dialect the whole test suite uses.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Soft deletes. Queries must filter ``deleted_at IS NULL`` explicitly."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class Database:
    """Owns the engine and session factory for one service."""

    def __init__(self, settings: ServiceSettings) -> None:
        if not settings.database_url:
            raise RuntimeError(
                f"{settings.service_name}: DATABASE_URL is required but was not set."
            )
        self._settings = settings
        url = settings.database_url

        # PostgreSQL is the only supported deployment target. SQLite is used by the
        # test suites and to author migrations offline, and it is close enough to run
        # this code — which is exactly the danger: a `DATABASE_URL` typo in production
        # would boot cleanly, serve traffic, and lose every schema, JSONB column,
        # partial index and CHECK constraint the platform depends on, with the first
        # symptom arriving days later as corrupt data rather than an error.
        if settings.is_production and not url.startswith("postgresql"):
            raise RuntimeError(
                f"{settings.service_name}: DATABASE_URL must be a PostgreSQL URL in "
                f"production (got '{url.split('://')[0]}://…'). SQLite is for tests "
                "and offline migration work only."
            )
        options: dict[str, Any] = {"echo": settings.db_echo}

        # Connection pooling and driver options are dialect-specific. SQLite (used by
        # service test suites so they need no live database) rejects the pool
        # arguments outright, and `server_settings` is an asyncpg concept that means
        # nothing to any other driver.
        if url.startswith("postgresql"):
            options.update(
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_recycle=settings.db_pool_recycle,
                # Verifies a pooled connection before handing it out. Essential on
                # Railway, where the Postgres plugin can drop idle connections.
                pool_pre_ping=True,
                connect_args={
                    "server_settings": {
                        "application_name": settings.service_name,
                        "jit": "off",
                    },
                    # asyncpg caches prepared statements per connection; pgbouncer in
                    # transaction mode breaks that, so keep the cache off.
                    "statement_cache_size": 0,
                },
            )

        self._engine: AsyncEngine = create_async_engine(url, **options)
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker

    async def session(self) -> AsyncIterator[AsyncSession]:
        """FastAPI dependency: one session per request, committed on success.

        The handler never calls ``commit()`` itself. If it raises, the transaction is
        rolled back as a unit — a handler cannot leave a half-written aggregate.
        """
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def healthcheck(self) -> dict[str, Any]:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            pool = self._engine.pool
            return {
                "status": "up",
                "pool": {
                    "size": getattr(pool, "size", lambda: None)(),
                    "checked_out": getattr(pool, "checkedout", lambda: None)(),
                },
            }
        except Exception as exc:
            logger.warning("db.healthcheck_failed", error=str(exc))
            return {"status": "down", "error": str(exc)}

    async def ensure_schema(self) -> None:
        """Create this service's schema if it does not exist.

        Alembic owns tables; the schema itself has to exist before the first
        migration runs, and only the owning service knows its name.
        """
        schema = self._settings.database_schema
        if schema == "public":
            return
        # SQLite has no schemas. Test suites ATTACH a database under the service's
        # schema name instead, so there is nothing to create here.
        if self._engine.dialect.name != "postgresql":
            return
        async with self._engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        logger.info("db.schema_ready", schema=schema)

    async def dispose(self) -> None:
        await self._engine.dispose()
        logger.info("db.disposed")


def require_db(db: Database | None) -> Database:
    if db is None:
        raise ServiceUnavailableError("Database is not configured for this service.")
    return db

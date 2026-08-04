"""Async SQLAlchemy engine, session factory and declarative base.

Topology: one PostgreSQL database, one **schema per service**. Services never read
each other's tables — cross-service data is fetched over HTTP or consumed from a
Redis Stream. The schema boundary makes that rule enforceable with grants rather
than convention.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
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
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}


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


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database clock."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
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

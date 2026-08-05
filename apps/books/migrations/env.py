"""Alembic environment for the books service.

The `include_object` filter below is the single most important line in this file.
Alembic sees every schema on the connection; without the filter, autogenerate finds
the books/payment/automation tables absent from *this* service's metadata and emits
`DROP TABLE` for all of them. One `alembic upgrade head` would then destroy the
platform.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import models  # noqa: E402, F401  - imported for its side effect of registering tables
from knowledgeos_core import Base  # noqa: E402
from settings import settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic runs synchronously, so swap asyncpg for psycopg.
database_url = settings.sync_database_url or ""
if not database_url:
    raise RuntimeError("DATABASE_URL must be set to run migrations.")
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata

SCHEMA = settings.database_schema


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Restrict autogenerate to this service's schema.

    Removing this makes `alembic revision --autogenerate` generate a migration that
    drops every other service's tables.
    """
    if type_ == "table":
        return getattr(obj, "schema", None) == SCHEMA
    # Indexes and constraints follow their table.
    parent_schema = getattr(getattr(obj, "table", None), "schema", None)
    return parent_schema in (None, SCHEMA)


def _configure(connection=None, url=None) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=include_object,
        # Keeps this service's revision pointer inside its own schema, so services
        # migrate independently with no shared lock.
        version_table_schema=SCHEMA,
        compare_type=True,
        compare_server_default=True,
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"} if url is not None else {},
    )


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        from sqlalchemy import text

        # The schema must exist before the version table can be created in it.
        if connection.dialect.name == "postgresql":
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        elif connection.dialect.name == "sqlite":
            # SQLite has no schemas and is never a deployment target — it is only
            # used to author and verify migrations offline, without a live Postgres.
            # ATTACHing a sibling file stands in for the schema and, unlike
            # ':memory:', survives the connection so `alembic check` can inspect it.
            main_path = Path(connection.engine.url.database or ":memory:")
            attached = (
                ":memory:"
                if str(main_path) == ":memory:"
                else str(main_path.with_name(f"{main_path.stem}.{SCHEMA}{main_path.suffix}"))
            )
            connection.execute(text(f"ATTACH DATABASE '{attached}' AS {SCHEMA}"))
        connection.commit()

        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

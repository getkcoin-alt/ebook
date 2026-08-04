-- Bootstrap the local development database.
--
-- Runs once, on first `docker compose up`, when the postgres data volume is empty.
-- Production schema creation happens in each service's own startup path
-- (Database.ensure_schema) plus its Alembic migrations — this file only makes the
-- local database look like production from the first boot.

-- One schema per service. Cross-service reads are an architectural violation, and
-- separate schemas make that enforceable with GRANTs rather than code review.
CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS books;
CREATE SCHEMA IF NOT EXISTS payment;
CREATE SCHEMA IF NOT EXISTS automation;
CREATE SCHEMA IF NOT EXISTS notifications;
CREATE SCHEMA IF NOT EXISTS ai;
CREATE SCHEMA IF NOT EXISTS admin;
CREATE SCHEMA IF NOT EXISTS search;

-- pgcrypto: gen_random_uuid() as a database-side fallback. Application code
-- generates UUIDs in Python so an object has identity before it is flushed.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- pg_trgm powers trigram indexes for fuzzy ILIKE matching on admin screens.
-- Catalogue search goes through Meilisearch; this is for internal lookups only.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- unaccent lets "Beyonce" match "Beyoncé" in author name lookups.
CREATE EXTENSION IF NOT EXISTS unaccent;

-- btree_gin allows a single GIN index to mix scalar columns with array/jsonb ones,
-- e.g. (status, tags) on the books table.
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- Track slow queries in development so N+1s surface before they reach production.
-- Requires shared_preload_libraries; silently skipped when unavailable.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_stat_statements unavailable (needs shared_preload_libraries); skipping.';
END
$$;

-- A dedicated, non-superuser application role. Local development should not run as
-- the postgres superuser, or permission bugs only appear in production.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'knowledgeos_app') THEN
        CREATE ROLE knowledgeos_app LOGIN PASSWORD 'knowledgeos_dev_password';
    END IF;
END
$$;

GRANT USAGE, CREATE ON SCHEMA auth, books, payment, automation, notifications, ai, admin, search
    TO knowledgeos_app;
GRANT ALL PRIVILEGES ON DATABASE knowledgeos TO knowledgeos_app;

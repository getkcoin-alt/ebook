# ADR 0002 — One database, one schema per service

**Status:** Accepted · **Date:** 2026-08-04

## Context

Microservice orthodoxy says each service owns a private database. That guarantees
isolation but multiplies cost and operational surface. Railway bills per database
plugin; nine Postgres instances is neither affordable at launch nor operable by a
small team.

## Decision

**One PostgreSQL database. One schema per service. Each service owns its own Alembic
migration chain.**

```
knowledgeos (database)
├── auth.*           ← auth service only
├── books.*          ← books service only
├── payment.*        ← payment service only
├── automation.*     ← automation service only
├── notifications.*  ← notification service only
├── ai.*             ← ai service only
└── admin.*          ← admin service only
```

### The rule this preserves

**No service reads another service's schema.** Cross-service data is fetched over
HTTP or consumed from a Redis Stream. A schema boundary is not merely a naming
convention — it is enforceable with `GRANT`, so the isolation is real even though
the server is shared.

### How migrations stay independent

Each service configures Alembic with its own `version_table_schema`, so
`auth.alembic_version` and `books.alembic_version` are separate rows in separate
schemas. Services migrate on their own schedule with no shared lock and no ordering
requirement between them.

```python
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    version_table_schema=settings.database_schema,
    include_schemas=True,
    # Without this, autogenerate emits DROP TABLE for every other service's tables,
    # because it sees them in the connection and not in this service's metadata.
    include_object=lambda obj, name, type_, reflected, compare_to: (
        getattr(obj, "schema", None) == settings.database_schema
    ),
)
```

That `include_object` filter is the single most important line in the setup. Without
it, the first `alembic revision --autogenerate` in any service generates a migration
that drops the entire platform.

## Consequences

**Good.** One connection string, one backup, one failover. Cross-schema joins remain
*possible* for genuine emergencies (a data-repair script) without being routine.
Migration chains are fully independent. Costs one Railway plugin instead of nine.

**Costs.**

- **Shared connection limit.** One Postgres has a global `max_connections`. Nine
  services × replicas × pool size adds up fast, so pool sizes are deliberately small
  (`db_pool_size=10`) and `pool_pre_ping` is on. Beyond ~10 replicas per service,
  introduce pgbouncer in transaction mode — at which point `statement_cache_size=0`
  (already set) becomes mandatory rather than merely correct.
- **Noisy neighbour.** A sequential scan in `admin` consumes IO that `books` needs.
  Mitigated by `log_min_duration_statement` and by keeping analytical queries in the
  admin service where they can be moved to a read replica later.
- **Shared blast radius.** One database means one thing to lose. Accepted: at this
  scale, nine databases means nine things to forget to back up.

## When to revisit

Split a service to its own database when **any** of these becomes true:

1. It needs a different Postgres version, extension set, or failover posture.
2. Its write volume alone saturates the shared instance.
3. Compliance requires physical isolation (most likely `payment` first).

Because no service reads another's schema, that split is a `pg_dump` of one schema
and a connection-string change — not a rewrite. **That is the entire point of paying
the schema-discipline cost now.**

## Related decisions

**UUIDv4 primary keys, generated in Python.** Application-side generation gives an
object its identity before it is flushed, which lets us build URLs and publish events
inside the same transaction that creates the row. UUIDv4's random distribution does
cause btree index fragmentation; if insert throughput on `books` becomes a
bottleneck, UUIDv7 (time-ordered) is a drop-in replacement that restores locality.

**Money as integer minor units.** Every amount is an integer of the currency's
smallest unit (paise, cents). Floats cannot represent 0.1 exactly; a float price
column loses money at scale and produces invoices that do not reconcile.

**Soft deletes on user-facing content.** `deleted_at IS NULL` filtering is explicit
rather than a global query filter, because an implicit filter is invisible at the
call site and eventually someone writes the report that silently omits half the rows.

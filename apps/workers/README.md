# Workers service

The platform's scheduler. It calls each service's maintenance sweeps on a schedule
and records what happened.

- **Port** 8009 · **Schema** `workers` · **7 endpoints** · **46 tests**

---

## It calls HTTP, never a table

This is the whole design, and it is the reason this service is worth having rather
than a cron container running `psql`.

A scheduler with direct database access to every schema is how a microservice platform
quietly re-couples. The sweep that expires unpaid orders would end up encoding the
payment service's order state machine — so those two deployables must then change
together forever, and the change that breaks it is a change nobody made to this
repository's scheduler. Worse, the constraint that makes schema-per-service worth
anything ([ADR 0002](../../docs/adr/0002-database-topology.md): only one service writes
to a schema) gets broken by the one process nobody thinks of as a service.

So every entry in `schedule.py` names a **service and an HTTP path**. Those paths are
the `/internal/maintenance/*` routes each owning service already exposes and already
tests. Adding a sweep is a row here plus a route there; it is never a migration in
someone else's schema.

Nothing here interprets what a sweep did, either. The response is recorded and that is
all. The moment this service starts reasoning about order states, it has taken on the
payment service's domain.

---

## The schedule

Thirteen jobs across seven services, in `schedule.py`. Two rules shape the cadences.

**Nothing expensive runs on the hour.** Every fixed-minute entry is offset — `:13`,
`:17`, `:23`, `:29`, `:37`, `:41`, `:47`, `:53`, `:59`. Six services all waking at
`:00` produce a six-way load spike once an hour and a flat sixty minutes either side,
and the spike is what sizes the database.

**Frequency follows the cost of being late, not the cost of running.** Expiring unpaid
orders is cheap, and being late means holding stock that is not being sold — so it runs
every five minutes. Pruning last year's per-user AI budget rows is also cheap, but
nobody notices if it is a day late, so it runs nightly.

`DISABLED_JOBS` switches entries off by name. Use it: a deployment with no Meilisearch
or no AI provider should not run those sweeps at all. A job that fails every hour by
design teaches everyone to ignore the failure count, and then the one that matters goes
unnoticed too. A name in `DISABLED_JOBS` that matches nothing is logged as a warning at
startup, because a typo there silently disables nothing.

---

## Single-flight

Every deployment runs more than one replica, and beat fires on all of them. Without a
lock, `payment.expire-orders` runs three times a minute against a service that is
idempotent but not free, and `search.reconcile` starts three simultaneous catalogue
walks.

Each run takes a Redis lock keyed on the job name and holds it for the job's
`lock_ttl` — which is always **longer than that job's timeout**, or a slow run releases
its own lock and the next tick starts a second copy on top of it. There is a test that
enforces this across the whole schedule.

**Losing the lock is a normal outcome, not a failure.** It is recorded as
`skipped_locked`, it does not count against a job's health, and it does not reset a
failure streak. Conflating it with failure makes a three-replica deployment look like
it is failing two runs in three.

The one exception is `POST /jobs/{name}/run`. An operator pressing "run now" wants it
to run now, not to be told another replica might already be doing it — and every sweep
on this schedule is idempotent, so the cost of an overlap is duplicated work, not wrong
data.

---

## Four outcomes, and why they are four

| Outcome | Meaning |
|---|---|
| `succeeded` | 2xx from the owning service. |
| `failed` | A 4xx/5xx, or the service was unreachable. |
| `timed_out` | No response inside the job's budget. |
| `skipped_locked` | Another replica took this run. |
| `disabled` | Switched off for this deployment. |

**A response is not a success.** The shared client only raises for transport failures,
so a 500 from a sweep comes back as an ordinary response object. Recording that as a
successful run is how a sweep that has been broken for a week shows up green on the
dashboard.

**A timeout is not a failure.** A sweep that exceeded our patience has very likely
completed on the other side; we stopped waiting. Treating it as failed invites a retry
that duplicates whatever it did.

Retries are deliberately shallow. Every job runs again on its own schedule, so a failed
run is a delay, not data loss — and a worker that retries hard turns a struggling
sibling into a service under sustained attack from its own platform, at exactly the
moment it can least take it.

---

## The history table exists for one question nobody can answer from logs

*Did the nightly reconcile run last night?* That question is answerable from logs only
by grepping hundreds of megabytes across however many replicas, which is why nobody
asks it until something has been wrong for a week. One row per run makes it a `SELECT`.

The harder question is the one this table is really for: **has beat stopped firing?**
A job that is not running produces no log lines and no failures. It is invisible in
every signal except the absence of recent runs — so `stale_jobs` on
`GET /v1/admin/workers/health` is the field worth alerting on.

Staleness is "no run in three of the job's own intervals". Three rather than one,
because a single missed tick is a deploy or a restart, and alerting on that teaches
everyone to ignore the alert. A job that has *never* run is only stale once the
scheduler has been recording anything for longer than that job's window — otherwise a
fresh install reports its whole schedule as broken thirty seconds after boot.

Health is measured as **consecutive failures since the last success**, not failures in
a window. "Three failures today" is also true of a job that failed three times this
morning and has been fine since, and that job does not need anyone's attention.

---

## Endpoints

### Operator (`/v1/admin/workers`)
`GET /jobs` · `GET /jobs/{name}` · `POST /jobs/{name}/run` · `GET /runs` ·
`GET /health`

Reading needs `analytics:read`; triggering needs `settings:write`. There is no public
surface — a customer has no reason to know the scheduler exists, and a run record names
internal paths on sibling services.

### Internal (HMAC-signed)
`POST /internal/maintenance/prune` · `GET /internal/health`

The prune is this service's own sweep, driven by its own schedule. A history table that
grows forever is exactly the problem this service exists to solve for everyone else.

---

## Running it

Three processes, and they are genuinely separate:

```bash
cp .env.example .env
alembic upgrade head

python -m main                                     # operator API, :8009
celery -A tasks.celery_app beat                    # fires the schedule
celery -A tasks.celery_app worker -Q maintenance   # executes it
```

Running the API without beat is a valid deployment — you simply have no scheduler,
which `/v1/admin/workers/health` will say clearly rather than leaving someone to
discover it a week later.

`INTERNAL_API_SECRET` must match every other service's exactly. It is the credential
that opens their `/internal/*` routes, and a mismatch means every scheduled job fails
with a 401 — which reads as a platform outage rather than a configuration error.

Tests need no broker and no sibling services:

```bash
PYTHONPATH=. pytest tests/ -q
```

The sibling stub sits at the `ServiceRegistry` boundary — the same seam the production
runner calls through — so locking, timeout classification, status handling and history
recording are all the real code. It can be told to return any status, to time out, to
be unreachable, and to **suspend for a configurable delay**: without a real suspension
the whole run completes without yielding to the event loop, and a single-flight test
would pass for the wrong reason.

---

## Things worth knowing before you change this

**The task takes a job name, not a job.** A `ScheduledJob` sent through the broker
would be a snapshot of the schedule as it was when beat started, so a corrected timeout
or a newly disabled job would keep firing with its old configuration until beat
restarted.

**One generic task, not one per job.** The schedule is data, so a new sweep is a row
rather than a function, a registered task name, a beat entry and a deploy. The
alternative accumulates thirteen nearly identical functions differing only in a URL, and
the fourteenth is always copy-pasted with one line unchanged.

**Beat entries carry `expires`.** If beat was down when a job should have fired, it runs
once on recovery rather than replaying every missed tick — a worker returning after an
hour must not immediately fire twelve copies of a five-minute sweep.

**A job's `service` must resolve to a `<name>_service_url` setting.** `ServiceRegistry`
looks it up by attribute, so a typo fails at runtime with "No URL configured", which
reads as an outage. There is a test over the whole schedule for exactly this.

**`_interval_seconds` is a deliberately rough cron reader.** It feeds a threshold that
is already multiplied by three, so the difference between "hourly" and "every 57
minutes" changes nothing — and a real cron parser would be a dependency carried for a
number that is then rounded away.

**Timestamps are normalised to UTC on read.** PostgreSQL returns these tz-aware and
SQLite returns them naive, so an unguarded comparison raises `TypeError` on the backend
the tests run against and works fine in production — the worst possible arrangement.

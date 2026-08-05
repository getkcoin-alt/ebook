# Admin service

The operator's view of the platform, and the home of feature flags.

- **Port** 8008 · **Schema** `admin` · **11 endpoints** · **57 tests**

---

## It owns almost nothing

The dashboard is assembled from the services that own the numbers. Revenue is the
payment service's figure; index health is the search service's; spend is the AI
service's. Nothing here is computed.

That restraint is the design. A second copy of revenue in this schema is a second copy
that drifts, and when the two disagree nobody can say which is right — least of all the
person looking at the dashboard, who has no way to know there are two. So this service
reads the same admin endpoints an operator could call directly, which is what keeps the
number on this page identical to the number on the owning service's own screen.

There is no orders table here, no book table, and **no moderation queue** — reviews
belong to the books service, and a queue here would duplicate state that service
already has to keep correct.

The two tables it does own are **feature flags** and their audit history. Every service
reads flags and none owns them, so putting them in any one service's schema would make
that service a dependency of every other for a reason unrelated to its domain.

---

## It forwards your token

This is the decision worth understanding before changing anything here.

The obvious implementation calls sibling services under this service's own HMAC
identity. That makes admin a **confused deputy**: it holds a key that opens every
`/internal/*` route on the platform, so anyone who gets past *its* permission check
receives data from every service regardless of what they are allowed to see *in those
services*. One over-broad role here, and the blast radius is everything.

Instead, the caller's bearer token is forwarded on every panel request. Each service
runs its own `require_permission` against the real operator, and this service grants no
privilege it was not itself given. An operator who may not see revenue gets a card
saying so, next to the six panels they may see.

The one exception is the AI spend panel, whose source is an HMAC-only internal route —
so the permission check for that panel lives on this service's own endpoint, and the
token is deliberately not sent somewhere that has no use for it.

---

## Every panel fails on its own

A dashboard that returns one status for the whole page goes red because one optional
service is restarting, and then nobody can see the seven panels that are fine. So each
panel carries its own status:

| Status | Meaning |
|---|---|
| `ok` | The service answered. |
| `error` | It answered with a 4xx/5xx — including a 403, which is a real answer about permissions, not an outage. |
| `timeout` | No response inside the panel budget. |
| `unavailable` | Not reachable, or not configured on this deployment. |

Panels are fetched **concurrently and bounded twice** — per panel and for the page.
Serially the page costs the sum of every service's latency; concurrently but unbounded
it costs the slowest service on its worst day. There is a test that fails if the fetch
ever becomes serial, and it asserts on elapsed time rather than on outcomes, because
outcomes look identical either way.

**Upstream error messages never reach the browser.** They can carry internal hostnames
and query fragments, so a failed panel gets a message this service wrote.

Panel data is **flattened and bounded** on the way through: scalars pass, lists become
counts, long strings are dropped. A card shows numbers, and passing an upstream's whole
response through would make this service's response size a function of somebody else's
schema — including fields they add later.

The dashboard is cached briefly, keyed on the window and **not on the caller**. Every
panel is aggregate platform data with no per-user component, so caching per operator
would multiply the fan-out by the number of people watching a deploy — which is exactly
when the load matters.

---

## Feature flags

**This service returns the decision, never the rule.** A sibling asking "is
`new_checkout` on for this user?" gets `true` or `false`. Handing back the rollout
percentage instead would make every service implement the bucketing itself, and two
implementations of a hash bucket diverge eventually — which shows up as a user who has
the feature on one page and not on the next, and is nearly impossible to diagnose from
either side.

**Bucketing is a stable hash of the flag key and the user id.** Two properties follow,
and both matter:

- Raising a rollout only ever *adds* users. A rollout that reshuffles takes the feature
  away from people who already had it, which reads to them as a bug in the feature
  rather than in the flag.
- The cohort differs per flag. Hashing the user alone would make the same unlucky 5%
  the first cohort for every flag on the platform — so a small group would see every
  half-finished feature, and early rollouts would test one unrepresentative slice over
  and over.

**An unknown flag is off, and says so.** Defaulting it to on would mean a typo in a
service's flag name silently enables an unfinished feature — the worst possible
direction for that mistake to fail. Unknown keys are *also* reported, so the typo is
visible instead of looking like "off".

**A partial rollout is off for an anonymous caller.** They cannot be bucketed stably,
and a random bucket makes the same page flicker between variants on reload.

Every change is audited with **both sides**. "Enabled payments" is a far less useful
record than "rollout went from 5% to 100%", and only before-and-after separates them.
The audit survives the flag's deletion — otherwise the record of a flag that was on
during an incident goes with it.

---

## Endpoints

### Operator (`/v1/admin`)
`GET /dashboard` · `GET /health-board` · `GET /flags` · `POST /flags` ·
`GET /flags/{key}` · `PATCH /flags/{key}` · `DELETE /flags/{key}` ·
`GET /flags/{key}/audit`

Reading needs `analytics:read`; changing a flag needs `settings:write`.

`/health-board` reads each service's `/health`, **not** `/health/ready`. Readiness goes
red when a dependency is briefly slow, which is correct for a load balancer and useless
on a status board — an operator wants to know which processes are alive, not which ones
would currently decline traffic.

### Internal (HMAC-signed)
`POST /internal/flags/evaluate` · `GET /internal/flags/{key}` ·
`POST /internal/maintenance/prune-audits`

`GET /internal/flags/{key}` returns a `reason` alongside the decision — allowlisted,
disabled, which bucket. It exists for the conversation that starts "the flag is not
working", which is otherwise unanswerable from either side.

---

## Running it

```bash
cp .env.example .env
alembic upgrade head
python -m main                # http://localhost:8008/docs
```

The dashboard works with sibling services absent — every missing one is an unavailable
card, which is the point. Without Redis it still works but fans out live on every
render, and logs a warning saying so at startup.

```bash
PYTHONPATH=. pytest tests/ -q
```

The sibling stub records **headers** as well as calls, because token forwarding is the
security property this service turns on and it is invisible in a stub that only records
paths. It also honours the timeout it is handed rather than sleeping through it — a
stub that ignores the budget makes every timeout test pass by never timing out.

---

## Things worth knowing before you change this

**`panel.status == PanelStatus.OK`, never `is`.** `BaseSchema` sets
`use_enum_values`, so a validated model's enum field is a plain string. An identity
check is never true, and the version of this code that used one reported the dashboard
incomplete even when every panel had answered. SQLAlchemy columns are the opposite —
they hand back enum members — which is why `is` is correct against a model and wrong
against a schema.

**The flag SAVEPOINT opens before `session.add`.** `begin_nested()` autoflushes pending
state, so adding first emits the INSERT outside the savepoint and the `IntegrityError`
escapes the `try`, taking the surrounding transaction with it.

**Flag keys are constrained to `[a-z][a-z0-9_.-]{1,80}`.** The key is embedded in cache
keys and read by every service; one with a colon or a space in it produces a cache
collision that is very hard to see.

**Cache invalidation on a flag change is best effort.** A cache that cannot be cleared
means the flag takes up to `FLAG_CACHE_TTL` to take effect — a delay. Refusing the write
because Redis is unavailable would be an outage.

**Adding a panel means adding a row to `PANEL_SOURCES` and a name to `PANELS`.** If the
new source is an HMAC-only route, add it to `_INTERNAL_PANELS` too, or an operator token
gets sent somewhere that will ignore it and the panel will fail on permissions instead.

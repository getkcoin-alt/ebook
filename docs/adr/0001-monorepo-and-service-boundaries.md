# ADR 0001 — Monorepo layout and service boundaries

**Status:** Accepted · **Date:** 2026-08-04

## Context

KnowledgeOS is eleven deployable units (one frontend, nine HTTP services, one worker
fleet) that share a domain model, an error contract and an auth scheme. They must
deploy independently but stay consistent.

## Decision

**One repository, Turborepo for the TypeScript half, a path-installed shared package
for the Python half.**

```
apps/          deployable units — each with a Dockerfile, README, .env.example
packages/      shared libraries — TS packages plus core-py
infrastructure/ docker, railway, nginx, monitoring
docs/          architecture, ADRs, runbooks, API docs
scripts/       operational tooling
```

### Why a monorepo

The alternative — eleven repositories — makes a change to the error envelope or the
`Book` schema an eleven-PR coordination exercise with a version-skew window in the
middle. One repository makes cross-cutting changes atomic and lets CI verify that
every consumer still compiles.

The usual monorepo objection is build times. Turborepo's content-based caching means
a frontend-only change does not rebuild shared packages, and the CI `paths-filter`
job skips the Python matrix entirely.

### Why `packages/core-py` rather than copy-paste

Nine FastAPI services need identical logging, health checks, error handling, auth
verification, pagination and graceful shutdown. Duplicating that is how services
drift until "the same" 500 error looks different in three of them.

`core-py` is installed into each service image as a path dependency, so there is no
private package registry to run, and a change to the shared runtime is visible in
the same commit as the services that consume it.

### Why not Nx

Nx is stronger for large TypeScript-only workspaces with heavy code generation.
Turborepo's task graph is simpler and this repository's TypeScript surface is one
Next.js app plus seven small libraries. Nx's advantages would not pay for its
configuration overhead here.

## Service boundaries

Boundaries follow **rate of change and failure isolation**, not entity count:

| Service | Owns | Why it is separate |
|---|---|---|
| `gateway` | routing, rate limiting, edge auth | The only public ingress; scales on total traffic |
| `auth` | identity, sessions, tokens | Security-critical; deploys on its own cadence; holds the signing key |
| `books` | catalogue, reviews, progress | Highest read traffic; scales independently |
| `automation` | ingestion pipeline | Long-running CPU work that must never block an API request |
| `ai` | LLM provider abstraction | Slow, expensive, and fails in ways nothing else should inherit |
| `search` | Meilisearch indexing/querying | Swappable; a search outage must not take down the catalogue |
| `payment` | orders, providers, invoices | Regulated, audited, smallest possible blast radius |
| `notifications` | email/SMS/WhatsApp/push | Pure fan-out; bursty |
| `admin` | analytics, moderation | Internal traffic only; different auth posture |
| `workers` | Celery execution | Scales on queue depth, not HTTP traffic |

`ai` is separate from `automation` specifically because provider latency is measured
in tens of seconds and quota errors are routine. Embedding that in the pipeline
service would make one provider outage look like an ingestion outage.

## Consequences

**Good.** Atomic cross-cutting changes; one CI pipeline; shared runtime guarantees;
independent deploys and scaling.

**Costs.** Contributors clone everything (~mitigated by sparse checkout). Turborepo
caching must be configured correctly or CI gets slow. `core-py` becomes a coupling
point — a breaking change there breaks every service at once, which is why it has
its own contract test suite.

**Rejected: one Postgres database per service.** Railway bills per database plugin
and eleven of them is neither affordable nor operable at this stage. See ADR 0002.

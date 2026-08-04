# Architecture Decision Records

Each ADR captures one significant decision: the context, what was chosen, what was
rejected and why, and the consequences we accepted.

| # | Decision | Status |
|---|---|---|
| [0001](0001-monorepo-and-service-boundaries.md) | Monorepo layout and service boundaries | Accepted |
| [0002](0002-database-topology.md) | One database, one schema per service | Accepted |
| [0003](0003-authentication.md) | RS256 access tokens with a JWKS endpoint | Accepted |
| [0004](0004-event-driven-communication.md) | Redis Streams for inter-service events | Accepted |

## Writing a new one

Copy the structure of an existing record: **Context** (the forces), **Decision**
(what we do), **Consequences** (good and bad, honestly). Record what was rejected
and why — that is usually the most useful part six months later.

An ADR is immutable once accepted. To change a decision, write a new ADR that
supersedes it and update the old one's status.

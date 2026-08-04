# ADR 0004 — Redis Streams for inter-service events

**Status:** Accepted · **Date:** 2026-08-04

## Context

Services must react to each other's state changes without synchronous coupling. When
a payment succeeds, the books service must grant access, notifications must send a
receipt, search must reindex, and admin must update revenue. Making the payment
service call four APIs synchronously means a notification outage fails a payment
that already took the customer's money.

## Decision

**Redis Streams** as the event bus, with one global stream and a consumer group per
service.

### Why not Redis Pub/Sub

Pub/Sub is fire-and-forget. A subscriber that is restarting, deploying or briefly
overloaded silently loses every message published during that window. For
`payment.succeeded` — the event that grants a paying customer access to their book —
that is data loss with a refund attached.

Streams give a durable log, consumer groups, explicit acknowledgement, and
`XAUTOCLAIM` for recovering messages from a replica that died mid-processing.

### Why not Kafka / RabbitMQ / SQS

Kafka is the right answer at a scale we do not have, and it is another cluster to
operate and pay for. RabbitMQ adds a second broker alongside the Redis we already run
for cache, rate limits and Celery. SQS ties the platform to AWS, which conflicts with
running on Railway.

Redis is already a hard dependency. Streams make it do one more job well. If event
volume ever justifies Kafka, the `EventPublisher`/`EventConsumer` interface is the
seam to swap behind.

### Design

```
kos:events           one stream, ~500k entries, approximate trimming
├── group:books           at-least-once, ack after handling
├── group:search
├── group:notifications
└── group:admin

kos:events:dead      poison messages, parked after 5 failed attempts
```

**One stream, not one per event type.** Ordering is preserved across types, which
matters for sequences like `order.created` → `payment.succeeded` →
`entitlement.granted`. Consumers filter by type; the cost is reading events they do
not care about, which is far cheaper than reasoning about cross-stream ordering.

**Delivery is at-least-once**, so **every handler must be idempotent**. Handlers
receive the event id and should use it as an idempotency key. Exactly-once delivery
does not exist in a distributed system; idempotent handlers are the actual solution.

**Poison messages are dead-lettered, not retried forever.** After 5 attempts an event
moves to `kos:events:dead` and is acknowledged, so one malformed payload cannot block
its consumer group indefinitely. Operators replay from the dead stream after a fix.

### Publish timing and the outbox question

Events are published **after** the database transaction commits. The alternative —
publishing inside the transaction — can announce an order that then rolls back.

This leaves a real gap: if the process dies between commit and publish, the event is
lost. The general fix is the transactional outbox pattern (write the event to a table
in the same transaction, relay it asynchronously).

We have **not** implemented an outbox, deliberately:

- For most events the consequence of a rare loss is recoverable — search reindexing
  is reconciled by a nightly sweep, analytics are recomputed from source tables.
- For the one case where loss is unacceptable — payment — the provider's own webhook
  redelivery is an independent reconciliation channel, and the payment service treats
  its database as the source of truth rather than the event.

**Revisit this** the moment a second flow appears where a lost event means lost money
and no external system can reconcile it. The `EventPublisher` interface is where an
outbox would slot in.

## Event catalogue

Naming: `<aggregate>.<past-tense-verb>`. Events are **facts that already happened**,
never commands — `book.published`, not `publish.book`. A consumer may ignore a fact;
a command implies an obligation and recreates the coupling events exist to remove.

| Event | Producer | Consumers |
|---|---|---|
| `user.registered` | auth | notifications (welcome), admin |
| `user.verified` | auth | books (entitlements), admin |
| `book.published` | books | search (index), notifications, admin |
| `book.updated` | books | search (reindex) |
| `order.paid` | payment | books (grant), notifications (receipt), admin |
| `payment.succeeded` | payment | books, notifications, admin |
| `payment.failed` | payment | notifications, admin |
| `automation.job_completed` | automation | books, search, notifications |
| `automation.job_failed` | automation | admin, notifications |
| `review.created` | books | search, admin (moderation queue) |

## Consequences

**Good.** Services stay decoupled and independently deployable. A consumer can be
down for minutes and catch up. New consumers add themselves without touching
producers. Replay is possible within the retention window.

**Costs.** Eventual consistency is now user-visible — a purchased book may take a
moment to appear in the library, so the UI must show pending state rather than assume
immediacy. Handlers must be idempotent, which is a real discipline. Debugging spans
services, which is why every event carries the originating `correlation_id`. Redis
memory is bounded by `MAXLEN ~500000`; a consumer lagging beyond that window loses
events, so consumer lag needs monitoring (`XINFO GROUPS`).

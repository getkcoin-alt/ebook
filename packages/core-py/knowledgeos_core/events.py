"""Inter-service event bus built on Redis Streams.

**Why Streams over Pub/Sub.** Pub/Sub is fire-and-forget: a subscriber that is
restarting, deploying or briefly overloaded silently loses every message published
during that window. For events like ``payment.succeeded`` — which grants a user
access to a book they paid for — that is data loss with a refund attached.

Streams give us a durable log, consumer groups (each service gets every event, but
only one replica within a service handles it), explicit acknowledgement, and
``XAUTOCLAIM`` to recover messages from a replica that died mid-processing.

Delivery is **at-least-once**, so every handler must be idempotent. Handlers get the
event id; use it as the idempotency key.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import orjson
from redis.asyncio.client import Redis
from redis.exceptions import ResponseError

from .logging import get_logger, request_id_ctx

logger = get_logger(__name__)

#: Global stream key. One stream keeps ordering across event types, which matters
#: for sequences like order.created -> payment.succeeded -> entitlement.granted.
STREAM_KEY = "kos:events"

#: Trim the stream to roughly this many entries. At ~1KB/event this caps memory at
#: a few hundred MB while retaining hours of replay for a lagging consumer.
STREAM_MAX_LEN = 500_000

DEAD_LETTER_STREAM = "kos:events:dead"


class EventType:
    """Canonical event names. Format: ``<aggregate>.<past-tense-verb>``."""

    USER_REGISTERED = "user.registered"
    USER_VERIFIED = "user.verified"
    USER_DELETED = "user.deleted"
    USER_LOGGED_IN = "user.logged_in"
    PASSWORD_RESET_REQUESTED = "user.password_reset_requested"  # noqa: S105 - event name, not a credential

    BOOK_CREATED = "book.created"
    BOOK_UPDATED = "book.updated"
    BOOK_PUBLISHED = "book.published"
    BOOK_UNPUBLISHED = "book.unpublished"
    BOOK_DELETED = "book.deleted"
    BOOK_VIEWED = "book.viewed"

    REVIEW_CREATED = "review.created"
    REVIEW_DELETED = "review.deleted"

    ORDER_CREATED = "order.created"
    ORDER_PAID = "order.paid"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_REFUNDED = "order.refunded"

    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    SUBSCRIPTION_ACTIVATED = "subscription.activated"
    SUBSCRIPTION_CANCELLED = "subscription.cancelled"

    ENTITLEMENT_GRANTED = "entitlement.granted"
    ENTITLEMENT_REVOKED = "entitlement.revoked"

    AUTOMATION_JOB_QUEUED = "automation.job_queued"
    AUTOMATION_STAGE_COMPLETED = "automation.stage_completed"
    AUTOMATION_JOB_COMPLETED = "automation.job_completed"
    AUTOMATION_JOB_FAILED = "automation.job_failed"

    SEARCH_REINDEX_REQUESTED = "search.reindex_requested"
    NOTIFICATION_REQUESTED = "notification.requested"


@dataclass(slots=True)
class Event:
    """An immutable fact that already happened."""

    type: str
    payload: dict[str, Any]
    id: str = ""
    source: str = ""
    correlation_id: str | None = None
    occurred_at: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.occurred_at:
            self.occurred_at = datetime.now(UTC).isoformat()
        if self.correlation_id is None:
            self.correlation_id = request_id_ctx.get()

    def to_fields(self) -> dict[bytes, bytes]:
        return {
            b"id": self.id.encode(),
            b"type": self.type.encode(),
            b"source": self.source.encode(),
            b"version": str(self.version).encode(),
            b"occurred_at": self.occurred_at.encode(),
            b"correlation_id": (self.correlation_id or "").encode(),
            b"payload": orjson.dumps(self.payload),
        }

    @classmethod
    def from_fields(cls, fields: dict[bytes, bytes]) -> Event:
        def get(key: str, default: str = "") -> str:
            return fields.get(key.encode(), b"").decode() or default

        raw_payload = fields.get(b"payload", b"{}")
        try:
            payload = orjson.loads(raw_payload)
        except orjson.JSONDecodeError:
            payload = {"_malformed": raw_payload.decode(errors="replace")}
        return cls(
            id=get("id"),
            type=get("type"),
            source=get("source"),
            version=int(get("version", "1")),
            occurred_at=get("occurred_at"),
            correlation_id=get("correlation_id") or None,
            payload=payload,
        )


class EventPublisher:
    """Publishes domain events.

    Publishing happens *after* the database transaction commits. Publishing inside
    the transaction risks announcing an order that then rolls back; publishing after
    risks a lost event if the process dies in between, which the outbox pattern
    solves — see ``docs/adr/0004-event-driven-communication.md`` for when to reach
    for it. Today only ``payment`` needs that guarantee and it uses provider webhooks
    as its own reconciliation channel.
    """

    def __init__(self, redis: Redis, *, source: str) -> None:
        self._redis = redis
        self._source = source

    async def publish(self, event_type: str, payload: dict[str, Any], **kwargs: Any) -> str:
        event = Event(type=event_type, payload=payload, source=self._source, **kwargs)
        try:
            message_id = await self._redis.xadd(
                STREAM_KEY,
                event.to_fields(),  # type: ignore[arg-type]
                maxlen=STREAM_MAX_LEN,
                approximate=True,  # ~ trimming is far cheaper than exact
            )
        except Exception:
            # A failed publish must not roll back work that already committed.
            # The event is logged so it can be replayed from the log if needed.
            logger.exception("event.publish_failed", event_type=event_type, event_id=event.id)
            raise
        logger.info(
            "event.published",
            event_type=event_type,
            event_id=event.id,
            stream_id=message_id.decode() if isinstance(message_id, bytes) else str(message_id),
        )
        return event.id


EventHandler = Callable[[Event], Awaitable[None]]


class EventConsumer:
    """Consumer-group reader with retry, claim-recovery and a dead letter stream."""

    def __init__(
        self,
        redis: Redis,
        *,
        group: str,
        consumer: str | None = None,
        block_ms: int = 5_000,
        batch_size: int = 20,
        max_attempts: int = 5,
        claim_idle_ms: int = 60_000,
    ) -> None:
        self._redis = redis
        self._group = group
        self._consumer = consumer or f"{group}-{uuid.uuid4().hex[:8]}"
        self._block_ms = block_ms
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._claim_idle_ms = claim_idle_ms
        self._handlers: dict[str, list[EventHandler]] = {}
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def on(self, event_type: str) -> Callable[[EventHandler], EventHandler]:
        """Decorator registering a handler. ``*`` subscribes to every event."""

        def decorator(handler: EventHandler) -> EventHandler:
            self._handlers.setdefault(event_type, []).append(handler)
            return handler

        return decorator

    async def _ensure_group(self) -> None:
        try:
            # mkstream so the consumer can start before any producer has published.
            await self._redis.xgroup_create(STREAM_KEY, self._group, id="0", mkstream=True)
            logger.info("event.group_created", group=self._group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _dispatch(self, event: Event) -> None:
        handlers = [*self._handlers.get(event.type, []), *self._handlers.get("*", [])]
        if not handlers:
            return
        for handler in handlers:
            await handler(event)

    async def _handle_message(self, message_id: bytes, fields: dict[bytes, bytes]) -> None:
        event = Event.from_fields(fields)
        try:
            await self._dispatch(event)
            await self._redis.xack(STREAM_KEY, self._group, message_id)
        except Exception as exc:
            pending = await self._redis.xpending_range(
                STREAM_KEY, self._group, min=message_id, max=message_id, count=1
            )
            attempts = int(pending[0]["times_delivered"]) if pending else 1
            logger.warning(
                "event.handler_failed",
                event_type=event.type,
                event_id=event.id,
                attempt=attempts,
                error=str(exc),
            )
            if attempts >= self._max_attempts:
                # Park it and acknowledge, so a poison message cannot block the group
                # forever. Operators replay from the dead stream after a fix.
                await self._redis.xadd(
                    DEAD_LETTER_STREAM,
                    # redis-py types the field mapping as str-or-bytes keyed, but
                    # declares the value union without `bytes` on the key side, so a
                    # wholly-bytes mapping — which is what the wire format is — does
                    # not satisfy it. The runtime accepts it; the annotation is wrong.
                    {
                        **event.to_fields(),  # type: ignore[dict-item]
                        b"error": str(exc).encode()[:2000],
                        b"group": self._group.encode(),
                    },
                    maxlen=50_000,
                    approximate=True,
                )
                await self._redis.xack(STREAM_KEY, self._group, message_id)
                logger.error("event.dead_lettered", event_type=event.type, event_id=event.id)
            # Otherwise leave it unacknowledged; XAUTOCLAIM redelivers it.

    async def _reclaim_stalled(self) -> None:
        """Recover messages a crashed replica left pending."""
        try:
            _, messages, _ = await self._redis.xautoclaim(
                STREAM_KEY,
                self._group,
                self._consumer,
                min_idle_time=self._claim_idle_ms,
                count=self._batch_size,
            )
        except ResponseError:
            return
        for message_id, fields in messages:
            if fields:
                await self._handle_message(message_id, fields)

    async def _loop(self) -> None:
        await self._ensure_group()
        logger.info("event.consumer_started", group=self._group, consumer=self._consumer)
        ticks = 0
        while self._running:
            try:
                # Periodically sweep for messages orphaned by a dead replica.
                ticks += 1
                if ticks % 12 == 0:
                    await self._reclaim_stalled()

                response = await self._redis.xreadgroup(
                    self._group,
                    self._consumer,
                    {STREAM_KEY: ">"},
                    count=self._batch_size,
                    block=self._block_ms,
                )
                if not response:
                    continue
                for _stream, messages in response:
                    for message_id, fields in messages:
                        await self._handle_message(message_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("event.consumer_error", group=self._group)
                await asyncio.sleep(2)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"events-{self._group}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            # Cancelling is the expected path here, not an error.
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # Remove this replica from the group so its name does not accumulate.
        # Best-effort: if Redis is already gone we are shutting down anyway, and a
        # stale consumer name is harmless — XAUTOCLAIM reclaims its pending entries.
        try:
            await self._redis.xgroup_delconsumer(STREAM_KEY, self._group, self._consumer)
        except Exception as exc:
            logger.debug("event.delconsumer_failed", error=str(exc))
        logger.info("event.consumer_stopped", group=self._group)

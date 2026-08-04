"""Event consumption: turning a completed payment into an entitlement.

Redis Streams deliver **at least once**. A redelivery happens whenever a handler is
slow, a replica is redeployed mid-batch, or `XAUTOCLAIM` recovers a message from a
crashed worker — none of which are unusual. So this handler is idempotent twice
over:

1. ``books.processed_events`` records the event id, in the *same transaction* as the
   effect. A second delivery hits the primary key and does nothing.
2. ``entitlements`` has a unique constraint over
   ``(user_id, book_id, source, external_ref)``, so even a grant that somehow ran
   twice converges on one row.

Belt and braces is deliberate: the ledger keeps the handler cheap on redelivery, the
constraint is what actually guarantees correctness.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledgeos_core import Event, EventConsumer, EventType, get_logger
from models import ProcessedEvent
from schemas import EntitlementSource
from services.entitlements import EntitlementService
from settings import Settings

logger = get_logger(__name__)

#: Events that grant access. Both are consumed because the two producers differ in
#: what they know: the payment service confirms money moved, the order service
#: knows which books the money was for. Whichever arrives first does the work; the
#: other is then a no-op for the same order because ``external_ref`` matches.
ENTITLEMENT_EVENTS: tuple[str, ...] = (EventType.PAYMENT_SUCCEEDED, EventType.ORDER_PAID)


@dataclass(slots=True)
class GrantRequest:
    """The parts of a payment/order event this service actually needs."""

    user_id: uuid.UUID
    book_ids: list[uuid.UUID]
    external_ref: str
    order_id: uuid.UUID | None


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _collect_book_ids(payload: dict[str, Any]) -> list[uuid.UUID]:
    """Pull book ids out of whichever shape the producer used.

    Producers are independently deployed services; accepting both the flat
    ``book_ids`` list and the richer ``items`` array means a payload change on their
    side does not silently stop granting access on ours.
    """
    candidates: list[Any] = []
    raw_ids = payload.get("book_ids")
    if isinstance(raw_ids, Iterable) and not isinstance(raw_ids, str | bytes):
        candidates.extend(raw_ids)
    if single := payload.get("book_id"):
        candidates.append(single)
    items = payload.get("items")
    if isinstance(items, Iterable) and not isinstance(items, str | bytes):
        for item in items:
            if isinstance(item, dict) and (book_id := item.get("book_id")):
                candidates.append(book_id)

    seen: dict[uuid.UUID, None] = {}
    for candidate in candidates:
        parsed = _as_uuid(candidate)
        if parsed is not None:
            seen.setdefault(parsed, None)
    return list(seen)


def parse_grant(payload: dict[str, Any]) -> GrantRequest | None:
    """Normalise an event payload, or ``None`` when it grants nothing."""
    user_id = _as_uuid(payload.get("user_id"))
    if user_id is None:
        return None
    book_ids = _collect_book_ids(payload)
    if not book_ids:
        return None
    order_id = _as_uuid(payload.get("order_id"))
    # The reference that makes the grant idempotent across *different* event ids:
    # a retried webhook produces a new event id but the same order.
    external_ref = str(
        payload.get("order_id") or payload.get("payment_id") or payload.get("reference") or ""
    )[:255]
    return GrantRequest(
        user_id=user_id, book_ids=book_ids, external_ref=external_ref, order_id=order_id
    )


class EntitlementEventHandler:
    """Grants entitlements from ``payment.succeeded`` / ``order.paid``."""

    def __init__(self, settings: Settings, entitlements: EntitlementService) -> None:
        self._settings = settings
        self._entitlements = entitlements

    async def claim(self, session: AsyncSession, event: Event) -> bool:
        """Record the event id. ``False`` means it was already processed.

        The row is written *before* the effect and committed *with* it, so either
        both land or neither does. Writing it afterwards would drop the grant when
        the process dies in between, and committing it separately would drop the
        grant when the effect fails.
        """
        existing = await session.get(ProcessedEvent, event.id)
        if existing is not None:
            return False
        session.add(ProcessedEvent(event_id=event.id, event_type=event.type))
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # Two replicas racing the same redelivery. The loser stops here.
            logger.info("event.duplicate", event_id=event.id, event_type=event.type)
            return False
        return True

    async def handle(self, session: AsyncSession, event: Event) -> int:
        """Process one event. Returns the number of entitlements newly created.

        Does not commit — the caller owns the transaction, which is what keeps the
        ledger row and the grants atomic.
        """
        if not await self.claim(session, event):
            logger.info("event.skipped_duplicate", event_id=event.id, event_type=event.type)
            return 0

        grant = parse_grant(event.payload)
        if grant is None:
            # Recorded as processed regardless: an event we cannot act on will not
            # become actionable on redelivery, and leaving it unacknowledged would
            # have it retried five times and then dead-lettered.
            logger.warning(
                "event.unusable_payload", event_id=event.id, event_type=event.type
            )
            return 0

        created = 0
        for book_id in grant.book_ids:
            book = await self._entitlements.load_grantable_book(session, book_id)
            if book is None:
                logger.warning(
                    "entitlement.grant_skipped_unknown_book",
                    event_id=event.id,
                    book_id=str(book_id),
                )
                continue
            _, was_created = await self._entitlements.grant(
                session,
                user_id=grant.user_id,
                book_id=book_id,
                source=EntitlementSource.PURCHASE,
                external_ref=grant.external_ref,
                order_id=grant.order_id,
                can_download=True,
            )
            created += int(was_created)

        logger.info(
            "entitlement.granted_from_event",
            event_id=event.id,
            event_type=event.type,
            user_id=str(grant.user_id),
            books=len(grant.book_ids),
            created=created,
        )
        return created


def register_consumers(
    consumer: EventConsumer,
    sessionmaker: async_sessionmaker[AsyncSession],
    handler: EntitlementEventHandler,
) -> None:
    """Wire the handler onto the consumer, one session per event."""

    async def _grant(event: Event) -> None:
        async with sessionmaker() as session:
            try:
                await handler.handle(session, event)
                await session.commit()
            except Exception:
                await session.rollback()
                # Re-raised so the consumer leaves the message unacknowledged and
                # XAUTOCLAIM redelivers it; the ledger row rolled back with it, so
                # the retry is not mistaken for a duplicate.
                raise

    for event_type in ENTITLEMENT_EVENTS:
        consumer.on(event_type)(_grant)

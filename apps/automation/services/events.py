"""Event consumption.

The pipeline is normally started by an explicit call — an editor uploads a file and
the books service asks for it to be processed. This handler covers the other route:
a `book.created` carrying a source key, which is how a bulk upload path can drop
files and let the catalogue drive the work.

**Idempotency is not optional here.** Redis Streams deliver at least once, so a
redelivered `book.created` must not queue a second pipeline run against the same
file — two runs would both write derived artefacts under different job keys and both
try to publish, leaving one set of objects orphaned in the bucket forever.

Two guards, and both are needed:

* the `processed_events` row, written **in the same transaction** as the job. Written
  afterwards it is lost when the process dies in between; committed separately it
  lets a redelivery through.
* the partial unique index on `automation_jobs`, which catches the case the ledger
  cannot: two *different* events asking for the same file.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledgeos_core import ConflictError, Event, EventConsumer, EventType, get_logger
from models import ProcessedEvent
from schemas import JobCreate, JobOptions, SourceKind
from services.jobs import JobService
from settings import Settings

logger = get_logger(__name__)

#: Events that can start a pipeline run. Deliberately short — an event that starts
#: expensive work is a way for any producer on the platform to spend this service's
#: CPU, so the list of who can do that stays small enough to read.
HANDLED = (EventType.BOOK_CREATED,)


class AutomationEventHandler:
    def __init__(self, settings: Settings, jobs: JobService) -> None:
        self._settings = settings
        self._jobs = jobs

    async def handle(self, session: AsyncSession, event: Event) -> None:
        if event.type not in HANDLED:
            return

        payload = event.payload or {}
        source_key = payload.get("source_key") or payload.get("pdf_key")
        book_id = payload.get("book_id") or payload.get("id")
        if not source_key or not book_id:
            # A book created through the admin UI without a file. Normal, and not
            # something to retry — there is nothing to process.
            return

        if not await self._claim(session, event):
            logger.info("automation.duplicate_event_ignored", event_id=event.id)
            return

        try:
            await self._jobs.create(
                session,
                JobCreate(
                    book_id=uuid.UUID(str(book_id)),
                    source_key=str(source_key),
                    original_filename=str(source_key).rsplit("/", 1)[-1],
                    source=SourceKind.EVENT,
                    options=JobOptions(),
                ),
                correlation_id=event.correlation_id,
            )
        except ConflictError:
            # The file already has a live job. The ledger row stays committed: this
            # event has been handled, and handling it again would reach the same
            # conflict.
            logger.info(
                "automation.event_job_already_running",
                event_id=event.id,
                source_key=str(source_key),
            )

    async def _claim(self, session: AsyncSession, event: Event) -> bool:
        """Insert the ledger row. False means this event was already handled.

        A SAVEPOINT opened **before** `session.add`, because `begin_nested()`
        autoflushes pending state — adding first emits the INSERT outside the
        savepoint, so the `IntegrityError` escapes this `try` and takes the
        surrounding transaction with it.
        """
        savepoint = await session.begin_nested()
        session.add(ProcessedEvent(event_id=event.id, event_type=event.type))
        try:
            await session.flush()
        except IntegrityError:
            await savepoint.rollback()
            return False
        await savepoint.commit()
        return True


def register_consumers(
    consumer: EventConsumer,
    sessionmaker: async_sessionmaker[AsyncSession],
    handler: AutomationEventHandler,
) -> Callable[[Event], Awaitable[None]]:
    """Subscribe the handler, with a session per event."""

    async def _handle(event: Event) -> None:
        async with sessionmaker() as session:
            try:
                await handler.handle(session, event)
                await session.commit()
            except Exception:
                await session.rollback()
                # Re-raised so the consumer leaves it unacknowledged and XAUTOCLAIM
                # redelivers. The ledger row rolled back with it, so the retry is not
                # mistaken for a duplicate and dropped.
                raise

    for event_type in HANDLED:
        consumer.on(event_type)(_handle)
    return _handle

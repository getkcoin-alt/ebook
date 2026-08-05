"""Bulk catalogue import.

The one operation in this service that creates hundreds of rows from a single
request, which is why **`dry_run` defaults to true**. An import is typically a
spreadsheet an editor exported and edited by hand; the errors in it are systematic
(a shifted column, a decimal point in the price) and they are in every row. Finding
that out from a validation report costs nothing. Finding it out from four hundred
wrong books in the catalogue costs an afternoon and a lot of trust.

**Rows are independent.** One bad row does not abort the other three hundred and
ninety-nine — it is reported with its line number from the uploaded file and the rest
proceed. An all-or-nothing import means a single typo on line 287 sends an editor
back to the start.

**Prices arrive as minor units** and are validated as integers, like everywhere else
on this platform. An import that accepted `499.00` and multiplied by 100 would be
the one place float money got in.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import get_logger
from models import ImportBatch
from schemas import ImportCreate, ImportRow, ImportStatus, JobCreate, SourceKind
from services.clients import PipelineClients
from services.jobs import JobService
from settings import Settings

logger = get_logger(__name__)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, max_length: int = 200) -> str:
    """A URL slug from a title.

    NFKD-normalised and stripped to ASCII first, so "Café Society" becomes
    `cafe-society` rather than `caf-society` — dropping accented characters entirely
    mangles exactly the titles that need the transliteration most.
    """
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")[:max_length].strip("-")
    # A title of nothing but non-ASCII characters slugifies to an empty string, and
    # an empty slug is a 422 from the books service on an otherwise valid row.
    return slug or f"book-{uuid.uuid4().hex[:10]}"


@dataclass(slots=True)
class RowResult:
    line: int | None
    title: str
    ok: bool
    error: str | None = None
    book_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


@dataclass(slots=True)
class ImportOutcome:
    batch: ImportBatch
    rows: list[RowResult] = field(default_factory=list)

    @property
    def job_ids(self) -> list[uuid.UUID]:
        return [row.job_id for row in self.rows if row.job_id is not None]


class ImportService:
    def __init__(self, settings: Settings, jobs: JobService, clients: PipelineClients) -> None:
        self._settings = settings
        self._jobs = jobs
        self._clients = clients

    async def run(
        self,
        session: AsyncSession,
        payload: ImportCreate,
        *,
        requested_by: uuid.UUID | None = None,
    ) -> ImportOutcome:
        batch = ImportBatch(
            filename=payload.filename,
            status=ImportStatus.RUNNING,
            dry_run=payload.dry_run,
            total=len(payload.rows),
            requested_by=requested_by,
            errors=[],
        )
        session.add(batch)
        await session.commit()

        results: list[RowResult] = []
        seen_slugs: set[str] = set()

        for row in payload.rows:
            result = await self._process(
                session,
                row,
                batch=batch,
                dry_run=payload.dry_run,
                options=payload.options,
                seen_slugs=seen_slugs,
            )
            results.append(result)

        succeeded = sum(1 for result in results if result.ok)
        failed = len(results) - succeeded

        batch.succeeded = succeeded
        batch.failed = failed
        batch.errors = [
            {"line": result.line, "title": result.title, "error": result.error}
            for result in results
            if not result.ok
        ][:500]
        # Three statuses, not two. "Completed" on an import where a fifth of the rows
        # failed hides the work an operator still has to do.
        if failed == 0:
            batch.status = ImportStatus.COMPLETED
        elif succeeded == 0:
            batch.status = ImportStatus.FAILED
        else:
            batch.status = ImportStatus.PARTIAL
        batch.finished_at = datetime.now(UTC)
        await session.commit()

        logger.info(
            "automation.import_finished",
            import_id=str(batch.id),
            dry_run=payload.dry_run,
            total=batch.total,
            succeeded=succeeded,
            failed=failed,
        )
        return ImportOutcome(batch=batch, rows=results)

    async def _process(
        self,
        session: AsyncSession,
        row: ImportRow,
        *,
        batch: ImportBatch,
        dry_run: bool,
        options: object,
        seen_slugs: set[str],
    ) -> RowResult:
        result = RowResult(line=row.line, title=row.title, ok=False)

        try:
            self._validate(row, seen_slugs)
        except ValueError as exc:
            result.error = str(exc)
            return result

        if dry_run:
            result.ok = True
            return result

        try:
            book_id = await self._create_book(row)
        except Exception as exc:
            # Per row, not per import: one rejected title must not take the other
            # three hundred and ninety-nine with it.
            result.error = f"catalogue rejected the row: {exc}"[:500]
            logger.warning("automation.import_row_failed", line=row.line, error=str(exc)[:300])
            return result

        result.book_id = book_id
        result.ok = True

        # A row without a file is a catalogue entry, not a pipeline job. Plenty of
        # imports are metadata-only backfills for books whose files arrive later.
        if row.source_key:
            try:
                job = await self._jobs.create(
                    session,
                    JobCreate(
                        book_id=book_id,
                        source_key=row.source_key,
                        original_filename=row.source_key.rsplit("/", 1)[-1],
                        source=SourceKind.IMPORT,
                        options=options,  # type: ignore[arg-type]
                    ),
                    requested_by=batch.requested_by,
                    import_id=batch.id,
                )
                result.job_id = job.id
            except Exception as exc:
                # The book exists; only its processing failed to queue. Reported so
                # an operator can requeue it, not treated as a failed row — deleting
                # the book to "undo" would be worse than leaving it unprocessed.
                result.error = f"book created but processing could not be queued: {exc}"[:500]

        return result

    def _validate(self, row: ImportRow, seen_slugs: set[str]) -> None:
        if not row.title.strip():
            raise ValueError("title is required")

        slug = slugify(row.title)
        if slug in seen_slugs:
            # Caught here rather than at the books service, because a duplicate title
            # within one spreadsheet is almost always a copy-paste mistake and the
            # editor wants to know which line.
            raise ValueError(f"duplicate title in this import (slug '{slug}')")
        seen_slugs.add(slug)

        if row.isbn13:
            digits = "".join(char for char in row.isbn13 if char.isdigit())
            if len(digits) != 13:
                raise ValueError(f"isbn13 must be 13 digits, got {len(digits)}")

        if row.price_minor and row.price_minor < 0:
            raise ValueError("price_minor cannot be negative")
        # A non-zero price below one whole currency unit is almost always a
        # major-unit value nobody converted. The threshold is deliberately at 100
        # and not higher: a book priced at ₹4.99 is unusual but real, and a
        # validator that guesses at "too cheap" would reject legitimate rows while
        # still missing the ones that happen to land above whatever line it drew.
        # Free (0) is allowed — plenty of catalogues carry free titles.
        if 0 < row.price_minor < 100:
            raise ValueError(
                f"price_minor={row.price_minor} is under one whole currency unit; "
                "prices are in paise/cents, so this looks like an unconverted value"
            )

    async def _create_book(self, row: ImportRow) -> uuid.UUID:
        payload = {
            "title": row.title.strip(),
            "subtitle": row.subtitle,
            "slug": slugify(row.title),
            "description": row.description,
            "language": row.language,
            "price_minor": row.price_minor,
            "currency": row.currency,
        }
        if row.isbn13:
            payload["isbn13"] = "".join(char for char in row.isbn13 if char.isdigit())

        created = await self._clients.create_book(payload)
        return uuid.UUID(str(created["id"]))

    # ---- reading --------------------------------------------------------

    async def get(self, session: AsyncSession, import_id: uuid.UUID) -> ImportBatch | None:
        return await session.get(ImportBatch, import_id)

    async def list_batches(
        self, session: AsyncSession, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ImportBatch], int]:
        total = int((await session.execute(select(func.count(ImportBatch.id)))).scalar_one())
        stmt = (
            select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(limit).offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all()), total

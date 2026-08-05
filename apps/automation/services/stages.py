"""The thirteen stages.

Each stage is an async function taking a :class:`StageContext` and returning the
document it contributes to the job's accumulated result. That shape is the point:

* **A stage reads its inputs from the accumulated result, not from parameters.** That
  is what lets the runner start at stage 11 without reconstructing the arguments the
  ten skipped stages would have passed down a call chain.
* **A stage returns what it learned; it does not decide what happens next.** Ordering,
  retries, skipping and failure policy all live in the runner. A stage that knows it
  is stage 7 of 13 is a stage that cannot be re-run on its own.

**Every stage must be idempotent.** `acks_late` means a worker killed mid-job gets its
task redelivered, and the checkpoint may not have landed — so a stage can and will run
twice against the same input. The derived storage keys are therefore **content- and
job-addressed rather than random**: re-running the thumbnail stage overwrites the same
object instead of leaking a second one and leaving the first orphaned in the bucket
forever.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from knowledgeos_core import BadRequestError, get_logger
from knowledgeos_core.storage import ObjectStorage
from schemas import DocumentInfo, JobOptions, Stage
from services import documents, media
from services.clients import PipelineClients
from services.documents import UnprocessableDocument
from settings import Settings

logger = get_logger(__name__)


class StageSkipped(Exception):
    """Raised by a stage that has correctly decided there is nothing to do.

    Not an error path. A PDF whose first page carries no extractable image has no
    cover to derive, and recording that as a failure would put a red mark on a job
    that did exactly the right thing — and would eventually train whoever reads the
    dashboard to ignore red marks.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class StageContext:
    """Everything a stage may touch."""

    job_id: uuid.UUID
    book_id: uuid.UUID
    source_key: str
    original_filename: str | None
    options: JobOptions
    settings: Settings
    storage: ObjectStorage | None
    clients: PipelineClients
    #: The accumulated output of every stage so far. A stage reads its inputs here.
    result: dict[str, Any]
    #: The source bytes, held for the life of one run. Loaded once by COLLECT: every
    #: subsequent stage would otherwise re-download a 300MB file from object storage,
    #: turning a ten-stage pipeline into ten downloads.
    payload: bytes | None = None

    def require_payload(self) -> bytes:
        if self.payload is None:
            # Reachable when a job resumes from a stage after COLLECT — the bytes
            # live in process memory, not in the checkpoint, so a resumed run has to
            # fetch them again. The runner handles this by always running COLLECT;
            # this is the assertion that says so out loud.
            raise RuntimeError("Source bytes are not loaded; COLLECT must run first.")
        return self.payload

    def artefact_key(self, name: str, extension: str) -> str:
        """A deterministic key for a derived artefact.

        Job-addressed, not random. Re-running a stage after a redelivery must
        overwrite the object it wrote last time — a random key would leave the first
        one orphaned in the bucket with nothing referencing it and nothing to clean
        it up.
        """
        return f"private/derived/{self.book_id}/{self.job_id}/{name}.{extension}"

    def public_key(self, name: str, extension: str) -> str:
        """Covers and thumbnails go under `public/`, which is world-readable.

        Only images derived for the listing belong here. The book itself never does:
        an object under this prefix needs no signature and no entitlement.
        """
        return f"public/books/{self.book_id}/{name}.{extension}"


# ---------------------------------------------------------------------------
# 1-3: ingest
# ---------------------------------------------------------------------------


async def collect(ctx: StageContext) -> dict[str, Any]:
    """Fetch the source object into memory.

    The size is checked with a HEAD **before** the body is read. Reading first and
    checking after means the memory is already gone by the time the limit is
    consulted, and the worker that OOMs takes every other job on it down with it.
    """
    if ctx.storage is None:
        raise RuntimeError("Object storage is not configured.")

    head = await ctx.storage.head(ctx.source_key)
    if head.size > ctx.settings.max_source_bytes:
        raise UnprocessableDocument(
            "That file is larger than this service will process.",
            code="source_too_large",
            details={"size_bytes": head.size, "max_bytes": ctx.settings.max_source_bytes},
        )
    if head.size == 0:
        raise UnprocessableDocument("That file is empty.", code="source_empty")

    ctx.payload = await ctx.storage.get_bytes(ctx.source_key)
    return {
        "source": {
            "size_bytes": len(ctx.payload),
            "content_type": head.content_type,
            "etag": head.etag,
        }
    }


async def validate(ctx: StageContext) -> dict[str, Any]:
    """Identify the format and refuse anything the pipeline cannot handle.

    Before any expensive stage, deliberately. Discovering at the conversion stage
    that the "PDF" is a zip means every stage before it spent CPU and tokens on a
    file that was never going to publish.
    """
    payload = ctx.require_payload()
    detected = documents.sniff_format(payload)
    accepted = ctx.settings.accepted_format_set

    if detected not in accepted:
        raise UnprocessableDocument(
            f"This service does not process {detected} files.",
            code="unsupported_format",
            details={"detected": detected, "accepted": accepted},
        )

    claimed = documents.extension_of(ctx.original_filename)
    warnings: list[str] = []
    if claimed and claimed != detected:
        # Not fatal. Plenty of legitimate files are misnamed, and the header is the
        # evidence — but a pipeline that never notices will one day hand a zip to a
        # PDF parser and report a stack trace instead of an answer.
        warnings.append(f"extension_mismatch:{claimed}!={detected}")

    if detected == "epub":
        documents.assert_archive_safe(
            payload,
            max_decompressed=ctx.settings.max_decompressed_bytes,
            max_entries=ctx.settings.max_archive_entries,
        )

    return {
        "validation": {
            "format": detected,
            "claimed_extension": claimed or None,
            "checksum": documents.checksum(payload),
            "warnings": warnings,
        }
    }


async def extract_metadata(ctx: StageContext) -> dict[str, Any]:
    """Read the document: metadata, page count, and an excerpt.

    The excerpt is what every AI stage after this works from, which makes this stage
    the one that decides whether they produce anything useful. A book with no
    extractable text — a scan — is recorded as such rather than passed silently to a
    model that will then describe a book from its title alone.
    """
    payload = ctx.require_payload()
    fmt = (ctx.result.get("validation") or {}).get("format") or documents.sniff_format(payload)

    if fmt == "epub":
        inspection = documents.inspect_epub(
            payload,
            excerpt_chars=ctx.settings.excerpt_chars,
            max_pages=ctx.settings.excerpt_max_pages,
        )
    else:
        inspection = documents.inspect_pdf(
            payload,
            excerpt_chars=ctx.settings.excerpt_chars,
            max_pages=ctx.settings.excerpt_max_pages,
        )

    info = DocumentInfo(
        format=inspection.format,
        size_bytes=inspection.size_bytes,
        checksum=inspection.checksum,
        page_count=inspection.page_count,
        title=inspection.title,
        author=inspection.author,
        language=inspection.language,
        isbn13=inspection.isbn13,
        publication_date=inspection.publication_date,
        encrypted=inspection.encrypted,
        excerpt=inspection.excerpt,
        text_empty=inspection.text_empty,
    )
    if inspection.warnings:
        logger.info(
            "automation.inspection_warnings",
            job_id=str(ctx.job_id),
            warnings=inspection.warnings,
        )
    return {"document": info.model_dump(mode="json"), "warnings": inspection.warnings}


# ---------------------------------------------------------------------------
# 4-6: enrichment
# ---------------------------------------------------------------------------


def _ai_context(ctx: StageContext) -> dict[str, Any]:
    """The book as the AI service wants to see it.

    Built from the catalogue record where one exists and topped up from the file.
    The catalogue wins on title and authors: a publisher's PDF metadata is frequently
    the name of the InDesign template, and describing a book as "Untitled-3" is worse
    than describing it from an editor's title alone.
    """
    document = ctx.result.get("document") or {}
    book = ctx.result.get("book") or {}

    authors = [
        contributor.get("name")
        for contributor in (book.get("authors") or [])
        if contributor.get("name")
    ]
    if not authors and document.get("author"):
        authors = [document["author"]]

    return {
        "title": book.get("title") or document.get("title") or "Untitled",
        "subtitle": book.get("subtitle"),
        "authors": authors[:20],
        "categories": [
            category.get("name")
            for category in (book.get("categories") or [])
            if category.get("name")
        ][:20],
        "language": book.get("language") or document.get("language") or "en",
        "excerpt": document.get("excerpt") or "",
        "existing_description": book.get("description"),
        "page_count": document.get("page_count"),
    }


async def _enrich(ctx: StageContext, kinds: list[str], key: str) -> dict[str, Any]:
    document = ctx.result.get("document") or {}
    if document.get("text_empty") and not (ctx.result.get("book") or {}).get("description"):
        # Nothing to work from. A model handed a title and no text writes plausible
        # fiction about the book, which is worse than an empty field an editor can
        # see is empty.
        raise StageSkipped("no_source_text")

    output = await ctx.clients.generate(
        kinds=kinds,
        context=_ai_context(ctx),
        book_id=ctx.book_id,
        force_refresh=ctx.options.force_ai_refresh,
    )
    if output is None:
        raise StageSkipped("ai_unavailable")
    if output.empty:
        raise StageSkipped("ai_returned_nothing")

    return {
        key: {
            "description": output.description,
            "summary": output.summary,
            "meta_title": output.meta_title,
            "meta_description": output.meta_description,
            "keywords": output.keywords,
            "tags": output.tags,
            "cost_usd": output.cost_usd,
            "failed_kinds": output.failed,
        }
    }


async def generate_description(ctx: StageContext) -> dict[str, Any]:
    """Description and summary."""
    return await _enrich(ctx, ["description", "summary"], "copy")


async def seo(ctx: StageContext) -> dict[str, Any]:
    """Meta title, meta description and keywords."""
    return await _enrich(ctx, ["seo"], "seo")


async def tags(ctx: StageContext) -> dict[str, Any]:
    """Topical tags."""
    return await _enrich(ctx, ["tags"], "taxonomy")


# ---------------------------------------------------------------------------
# 7-10: transform
# ---------------------------------------------------------------------------


async def thumbnail(ctx: StageContext) -> dict[str, Any]:
    """Derive a cover and a catalogue thumbnail from the source.

    Two sizes, not one: a thumbnail scaled in the browser from a 1000px cover still
    transfers the 1000px file, on every card of every listing page.
    """
    payload = ctx.require_payload()
    fmt = (ctx.result.get("validation") or {}).get("format")
    if fmt != "pdf":
        raise StageSkipped("cover_extraction_supports_pdf_only")

    extracted = media.render_pdf_cover(payload)
    if extracted is None:
        # Normal: a typeset title page has no embedded cover artwork. The book keeps
        # whatever cover was uploaded alongside it.
        raise StageSkipped("no_cover_image_in_source")

    cover = media.resize(
        extracted.data,
        width=ctx.settings.cover_width,
        height=ctx.settings.cover_height,
        quality=ctx.settings.image_quality,
    )
    thumb = media.resize(
        extracted.data,
        width=ctx.settings.thumbnail_width,
        height=ctx.settings.thumbnail_height,
        quality=ctx.settings.image_quality,
    )

    return {
        "images": {
            "cover": {"bytes": cover.size, "width": cover.width, "height": cover.height},
            "thumbnail": {"bytes": thumb.size, "width": thumb.width, "height": thumb.height},
        },
        # Held in the run rather than written here: UPLOAD owns every write to
        # storage, so there is one place to look when an artefact is missing.
        "_pending_uploads": {
            ctx.public_key("cover", "jpg"): ("image/jpeg", cover.data),
            ctx.public_key("thumbnail", "jpg"): ("image/jpeg", thumb.data),
        },
    }


async def convert(ctx: StageContext) -> dict[str, Any]:
    """Produce the alternative artefacts the store sells or gives away.

    Format *conversion* proper — PDF to EPUB and back — is deliberately not done
    here. It needs a full layout engine, it produces a reflowed file that a publisher
    has not approved, and a bad automatic EPUB of a design-heavy book is worse for a
    customer than no EPUB at all. What is safe and genuinely useful is a **sample**:
    the first pages of the book, which is the same file the customer would get, just
    less of it.
    """
    payload = ctx.require_payload()
    fmt = (ctx.result.get("validation") or {}).get("format")
    if fmt != "pdf":
        raise StageSkipped("sample_generation_supports_pdf_only")

    sample = media.build_sample_pdf(payload)
    if sample is None:
        # Short books get no sample: a ten-page preview of a twelve-page pamphlet is
        # a giveaway, not a preview.
        raise StageSkipped("book_too_short_for_sample")

    return {
        "sample": {"bytes": len(sample)},
        "_pending_uploads": {ctx.artefact_key("sample", "pdf"): ("application/pdf", sample)},
    }


async def compress(ctx: StageContext) -> dict[str, Any]:
    """Shrink the distribution copy, or decide not to."""
    payload = ctx.require_payload()
    fmt = (ctx.result.get("validation") or {}).get("format")
    if fmt != "pdf":
        raise StageSkipped("compression_supports_pdf_only")

    outcome = media.compress_pdf(payload, min_gain=ctx.settings.min_compression_gain)
    if not outcome.applied:
        raise StageSkipped("compression_gain_below_threshold")

    # The compressed bytes become the working copy for the stages after this, so the
    # watermark is applied to the file that will actually ship rather than to a
    # version that is then thrown away.
    ctx.payload = outcome.data
    return {
        "compression": {
            "original_bytes": outcome.original_size,
            "compressed_bytes": outcome.size,
            "ratio": round(outcome.ratio, 4),
        }
    }


async def watermark(ctx: StageContext) -> dict[str, Any]:
    """Stamp provenance into the distribution copy."""
    enabled = (
        ctx.options.watermark
        if ctx.options.watermark is not None
        else ctx.settings.watermark_enabled
    )
    if not enabled:
        raise StageSkipped("watermarking_disabled")

    payload = ctx.require_payload()
    fmt = (ctx.result.get("validation") or {}).get("format")
    if fmt != "pdf":
        raise StageSkipped("watermarking_supports_pdf_only")

    stamped = media.watermark_pdf(payload, text=ctx.settings.watermark_text)
    if stamped == payload:
        raise StageSkipped("watermark_not_applied")

    ctx.payload = stamped
    return {"watermark": {"applied": True, "bytes": len(stamped)}}


# ---------------------------------------------------------------------------
# 11-13: publish
# ---------------------------------------------------------------------------


async def upload(ctx: StageContext) -> dict[str, Any]:
    """Write every derived artefact, then the distribution copy.

    The single place this pipeline writes to storage. Spreading writes across the
    stages that produce them reads more naturally, but it means an artefact that
    never appeared has five possible stages to check and no single audit point.

    The distribution copy is written **beside** the source, never over it. The
    uploader's original is the only thing here that cannot be regenerated; the moment
    a compression bug is discovered, the master is what makes it recoverable.
    """
    if ctx.storage is None:
        raise RuntimeError("Object storage is not configured.")

    pending: dict[str, tuple[str, bytes]] = dict(ctx.result.get("_pending_uploads") or {})
    written: dict[str, int] = {}

    fmt = (ctx.result.get("validation") or {}).get("format") or "pdf"
    transformed = bool(ctx.result.get("compression")) or bool(ctx.result.get("watermark"))
    if transformed and ctx.payload is not None:
        pending[ctx.artefact_key("distribution", fmt)] = (
            "application/pdf" if fmt == "pdf" else "application/epub+zip",
            ctx.payload,
        )

    for key, (content_type, data) in pending.items():
        # Long cache lifetimes are safe because the keys are content- and
        # job-addressed: a new run writes a new job's key, so a stale object is never
        # served under a key whose contents changed.
        stored = await ctx.storage.put_bytes(
            key,
            data,
            content_type=content_type,
            cache_seconds=31_536_000 if key.startswith("public/") else 0,
        )
        written[key] = stored.size

    return {
        "uploads": written,
        # Cleared so a resumed run does not re-upload from a stale result document,
        # and so the checkpoint does not carry megabytes of file bytes into JSON.
        "_pending_uploads": {},
        "keys": _artefact_keys(ctx, pending, fmt=fmt, transformed=transformed),
    }


def _artefact_keys(
    ctx: StageContext, pending: dict[str, tuple[str, bytes]], *, fmt: str, transformed: bool
) -> dict[str, str]:
    keys: dict[str, str] = {}
    cover = ctx.public_key("cover", "jpg")
    thumb = ctx.public_key("thumbnail", "jpg")
    sample = ctx.artefact_key("sample", "pdf")
    if cover in pending:
        keys["cover_key"] = cover
    if thumb in pending:
        keys["thumbnail_key"] = thumb
    if sample in pending:
        keys["sample_key"] = sample
    if transformed:
        keys[f"{fmt}_key"] = ctx.artefact_key("distribution", fmt)
    return keys


def build_catalogue_update(result: dict[str, Any]) -> dict[str, Any]:
    """The patch to send to the books service.

    Only fields the pipeline actually produced. Sending nulls for the rest would
    clear an editor's hand-written description with a blank the moment an AI stage
    was skipped — which is precisely the case where the human text is all there is.
    """
    document = result.get("document") or {}
    copy = result.get("copy") or {}
    seo_out = result.get("seo") or {}
    taxonomy = result.get("taxonomy") or {}
    keys = result.get("keys") or {}

    update: dict[str, Any] = {}

    if description := copy.get("description"):
        update["description"] = description[:50_000]
    if summary := copy.get("summary"):
        update["ai_summary"] = summary[:20_000]
    if meta_title := seo_out.get("meta_title"):
        update["meta_title"] = meta_title[:255]
    if meta_description := seo_out.get("meta_description"):
        update["meta_description"] = meta_description[:500]
    if tag_list := taxonomy.get("tags"):
        update["ai_tags"] = tag_list[:10]

    if page_count := document.get("page_count"):
        update["page_count"] = page_count
    if isbn13 := document.get("isbn13"):
        # 13 digits exactly, or the books service rejects the whole patch and every
        # other field goes with it.
        digits = "".join(char for char in isbn13 if char.isdigit())
        if len(digits) == 13:
            update["isbn13"] = digits

    update.update(keys)
    return update


async def index(ctx: StageContext) -> dict[str, Any]:
    """Write the pipeline's output back to the catalogue, then index it.

    The order is not negotiable: search reads the book from the books service, so
    indexing before the write would index the record as it was before this job ran.
    """
    patch = build_catalogue_update(ctx.result)
    if patch:
        await ctx.clients.update_book(ctx.book_id, patch)

    indexed = await ctx.clients.index_book(ctx.book_id)
    return {"catalogue_update": sorted(patch), "indexed": indexed}


async def publish(ctx: StageContext) -> dict[str, Any]:
    """Make the book visible — if that is what this deployment wants.

    `auto_publish` defaults to **off**. A pipeline that publishes whatever it is
    given puts machine-written copy in front of customers with nobody having read it,
    and the first time a model hallucinates an award into a description it is on the
    storefront. Off means the book lands ready for a human to approve, which takes
    one click and is the difference between an assistant and an unsupervised
    publisher.
    """
    wanted = (
        ctx.options.auto_publish
        if ctx.options.auto_publish is not None
        else ctx.settings.auto_publish
    )
    if not wanted:
        return {"published": False, "reason": "awaiting_review"}

    await ctx.clients.publish_book(ctx.book_id)
    return {"published": True}


#: Stage -> implementation. The runner reads this; there is no dispatch by name and
#: no `getattr` on a string, so a stage added to the enum without an implementation
#: fails at import rather than at three in the morning on the one job that reached it.
HANDLERS = {
    Stage.COLLECT: collect,
    Stage.VALIDATE: validate,
    Stage.EXTRACT_METADATA: extract_metadata,
    Stage.GENERATE_DESCRIPTION: generate_description,
    Stage.SEO: seo,
    Stage.TAGS: tags,
    Stage.THUMBNAIL: thumbnail,
    Stage.CONVERT: convert,
    Stage.COMPRESS: compress,
    Stage.WATERMARK: watermark,
    Stage.UPLOAD: upload,
    Stage.INDEX: index,
    Stage.PUBLISH: publish,
}

#: Errors that mean the input is wrong, not that something transient went wrong.
#: Retrying these five times with exponential backoff spends twenty minutes arriving
#: at the same answer and buries the real reason under four duplicate log lines.
TERMINAL_ERRORS = (UnprocessableDocument, BadRequestError)

"""Derived artefacts: covers, thumbnails, compression, watermarking.

Pure byte-in/byte-out functions, for the same reason as `documents.py` — they are the
expensive parts, and testing them against a real book in a real bucket is how they
end up untested.

The governing rule: **a transformation that does not clearly improve the file is not
applied.** Compression that saves 1% has spent minutes of CPU to produce a file that
is different from the master for no benefit, and every difference from the master is
a thing that can be wrong. Each function here reports what it did, and the caller
records "skipped, no gain" as a first-class outcome rather than a failure.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from knowledgeos_core import get_logger
from services.documents import UnprocessableDocument

logger = get_logger(__name__)

#: JPEG rather than PNG for covers: a book cover is a photograph or a rendered page,
#: and PNG's lossless compression on that content is several times the bytes for no
#: visible difference. Thumbnails are served on every catalogue card.
_JPEG = "image/jpeg"


@dataclass(slots=True)
class Derived:
    """One produced artefact."""

    data: bytes
    content_type: str
    width: int | None = None
    height: int | None = None

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(slots=True)
class CompressionResult:
    data: bytes
    original_size: int
    #: False when the rewrite gained too little to be worth diverging from the
    #: master. `data` is then the original, unchanged.
    applied: bool

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def ratio(self) -> float:
        if not self.original_size:
            return 0.0
        return 1.0 - (len(self.data) / self.original_size)


def _pillow():  # type: ignore[no-untyped-def]
    try:
        from PIL import Image

        return Image
    except ImportError:  # pragma: no cover - dependency is declared
        raise UnprocessableDocument(
            "Image support is not installed on this deployment.", code="image_support_missing"
        ) from None


def render_pdf_cover(data: bytes) -> Derived | None:
    """Extract a cover image from the first page of a PDF.

    **Extracted, not rasterised.** Rendering a page to a bitmap needs a full PDF
    renderer (poppler, mupdf) — a large native dependency whose job is to draw
    things, on a service whose job is to move files. Most publisher PDFs place the
    cover as a single full-page embedded image, and pulling that out gives a better
    result than a render anyway: it is the original artwork rather than a
    re-rasterisation of it.

    Returns None when the first page has no usable image. That is a normal outcome,
    not a failure — a typeset title page has no cover to extract, and the book simply
    keeps whatever cover was uploaded with it.
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is declared
        return None

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return None
        if not reader.pages:
            return None
        images = list(reader.pages[0].images)
    except Exception as exc:
        logger.info("automation.cover_extract_failed", error=str(exc)[:200])
        return None

    if not images:
        return None

    # The largest image on the page. A page often carries a publisher logo or a
    # decorative rule alongside the artwork, and picking the first would reliably
    # produce a 40px logo as the book's cover.
    best = max(images, key=lambda image: len(image.data))
    if len(best.data) < 2_048:
        # Too small to be cover art at any resolution.
        return None
    return Derived(data=best.data, content_type="application/octet-stream")


def resize(
    data: bytes, *, width: int, height: int, quality: int = 82, fit: str = "cover"
) -> Derived:
    """Scale an image to fit a box, as JPEG.

    ``fit="cover"`` fills the box and crops the overflow, centred. Book covers vary
    from nearly square to very tall, and letterboxing them produces a catalogue grid
    of different-sized images in identical frames, which looks broken. Cropping a few
    percent off a cover's edges does not.
    """
    Image = _pillow()

    try:
        with Image.open(io.BytesIO(data)) as source:
            # Guard before decode, not after: `Image.open` reads the header only, so
            # the dimensions are known before the pixels are allocated. A 60000x60000
            # PNG is 14GB decoded and a few KB on the wire.
            pixels = (source.width or 0) * (source.height or 0)
            if pixels > 80_000_000:
                raise UnprocessableDocument(
                    "That image is too large to process.",
                    details={"width": source.width, "height": source.height},
                )

            # `RGB` because JPEG has no alpha channel; without the convert, a
            # transparent PNG raises rather than saving.
            image = source.convert("RGB")

            if fit == "cover":
                scale = max(width / image.width, height / image.height)
                target = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
                image = image.resize(target, Image.Resampling.LANCZOS)
                left = (image.width - width) // 2
                top = (image.height - height) // 2
                image = image.crop((left, top, left + width, top + height))
            else:
                image.thumbnail((width, height), Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            # `optimize` runs a second Huffman pass — a few percent smaller for a
            # little CPU, on a file that will be served on every catalogue card.
            # `progressive` renders a low-quality pass first on a slow connection.
            image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
            return Derived(
                data=buffer.getvalue(),
                content_type=_JPEG,
                width=image.width,
                height=image.height,
            )
    except UnprocessableDocument:
        raise
    except Exception as exc:
        raise UnprocessableDocument(
            "That image could not be processed.", details={"reason": str(exc)[:200]}
        ) from exc


def compress_pdf(data: bytes, *, min_gain: float = 0.05) -> CompressionResult:
    """Rewrite a PDF smaller, or leave it alone.

    What this does is lossless: deduplicate identical objects and recompress the
    content streams. It does **not** downsample embedded images, which is where the
    large wins usually are — because that is lossy, and silently degrading the file a
    customer paid for is not a decision a compression stage gets to make. Downsampling
    belongs behind an explicit per-book setting, not in the default path.

    Returns the original when the gain is below `min_gain`. Every byte that differs
    from the master is a byte that can be wrong, and a 1% saving does not buy that.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover - dependency is declared
        return CompressionResult(data=data, original_size=len(data), applied=False)

    original = len(data)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Rewriting an encrypted PDF would drop its encryption. Leave it.
            return CompressionResult(data=data, original_size=original, applied=False)

        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        if reader.metadata:
            writer.add_metadata(reader.metadata)
        # Identical fonts and images appear once per page in many exports; this
        # collapses them to one shared object.
        writer.compress_identical_objects()
        for page in writer.pages:
            page.compress_content_streams()

        buffer = io.BytesIO()
        writer.write(buffer)
        compressed = buffer.getvalue()
    except Exception as exc:
        # A PDF that will not round-trip keeps its original. Compression is an
        # optimisation, and an optimisation that can lose the file is not one.
        logger.info("automation.compress_failed", error=str(exc)[:200])
        return CompressionResult(data=data, original_size=original, applied=False)

    gain = 1.0 - (len(compressed) / original) if original else 0.0
    if gain < min_gain:
        return CompressionResult(data=data, original_size=original, applied=False)
    return CompressionResult(data=compressed, original_size=original, applied=True)


def watermark_pdf(data: bytes, *, text: str) -> bytes:
    """Stamp store identity into a PDF's metadata.

    **Not a visible overlay, and deliberately so.** A visible watermark on the file a
    customer paid for degrades the thing they bought, and it is removed in seconds by
    anyone who wants it gone — so it annoys honest readers and stops nobody. What it
    is good for is per-recipient tracing, and that has to happen at *download* time
    with the buyer's identity in it, which is the books service's job, not this one's.

    What is useful to set here is provenance on the master copy: producer, creator
    and a marker that this file came out of this pipeline. That survives copying,
    costs the reader nothing, and answers "where did this file come from" when a copy
    turns up somewhere it should not be.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover - dependency is declared
        return data

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return data

        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        existing = dict(reader.metadata or {})
        existing.update(
            {
                "/Producer": text,
                "/Creator": text,
                # A distinct key rather than overwriting /Keywords: the publisher's
                # keywords are metadata a store should not be silently discarding.
                "/KOSOrigin": f"{text} automation pipeline",
            }
        )
        writer.add_metadata(existing)

        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except Exception as exc:
        logger.info("automation.watermark_failed", error=str(exc)[:200])
        return data


def build_sample_pdf(data: bytes, *, pages: int = 10) -> bytes | None:
    """A free preview: the first N pages.

    Returns None when the book is short enough that a "sample" would be most of it.
    A 12-page pamphlet with a 10-page sample is a giveaway, not a preview.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover - dependency is declared
        return None

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return None
        total = len(reader.pages)
        # Never more than a quarter of the book, whatever `pages` says.
        allowed = min(pages, max(1, total // 4))
        if total <= 4 or allowed >= total:
            return None

        writer = PdfWriter()
        for index in range(allowed):
            writer.add_page(reader.pages[index])
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except Exception as exc:
        logger.info("automation.sample_failed", error=str(exc)[:200])
        return None

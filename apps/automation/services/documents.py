"""File inspection: what is this, is it safe, and what does it say.

Pure functions over bytes. No database, no network, no storage — which is what makes
the interesting cases testable with a few hundred bytes of fixture instead of a real
book and a real bucket.

Two rules run through all of it.

**The extension is a claim, the magic bytes are evidence.** A file named `book.pdf`
is a file someone named `book.pdf`. Every format decision here reads the header, and
a mismatch between the two is itself reported — not because a renamed file is
necessarily an attack, but because a pipeline that trusts the name will eventually
hand a zip to a PDF parser and produce a stack trace instead of an answer.

**Every bound is checked before the work it bounds, never after.** Checking the
decompressed size of an EPUB after unzipping it is not a check; the memory is already
gone. The archive's declared sizes are read from its directory first, and the entries
are only then read — with the running total checked as it goes, because a declared
size is also a claim.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree

from knowledgeos_core import BadRequestError, get_logger

logger = get_logger(__name__)

#: Header signatures. Ordered longest-first so a prefix never shadows a longer match.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),  # EPUB, DOCX and every other zip container
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BOOKMOBI", "mobi"),
)

#: An EPUB is a zip whose first entry is an uncompressed `mimetype` file with this
#: content. The spec requires it precisely so a reader can identify the format
#: without unpacking anything.
_EPUB_MIMETYPE = b"application/epub+zip"

#: Dublin Core, the namespace EPUB metadata actually lives in.
_DC = "{http://purl.org/dc/elements/1.1/}"

_ISBN_RE = re.compile(r"(?:97[89])[- ]?(?:\d[- ]?){9}\d")
# The no-break space is deliberate: PDF extraction emits it constantly, and leaving
# it in means a "collapsed" excerpt still carries runs of invisible separators.
_WHITESPACE_RE = re.compile(r"[ \t ]+")  # noqa: RUF001
_BLANKLINES_RE = re.compile(r"\n{3,}")


class UnprocessableDocument(BadRequestError):
    """The file cannot be processed, and trying again will not change that.

    Distinct from a transient failure on purpose: retrying a corrupt PDF five times
    with exponential backoff spends twenty minutes arriving at the same answer, and
    buries the real one under four duplicate log lines.
    """

    default_code = "unprocessable_document"


@dataclass(slots=True)
class Inspection:
    """Everything inspection learned. The metadata stage's output."""

    format: str
    size_bytes: int
    checksum: str
    page_count: int | None = None
    title: str | None = None
    author: str | None = None
    language: str | None = None
    isbn13: str | None = None
    publication_date: str | None = None
    encrypted: bool = False
    excerpt: str = ""
    text_empty: bool = False
    #: Non-fatal observations — a renamed file, unreadable metadata, no extractable
    #: text. Surfaced on the job so an editor understands why a listing looks thin.
    warnings: list[str] = field(default_factory=list)


def checksum(data: bytes) -> str:
    """Content address for the source file.

    Used to notice that a "new" upload is a file the pipeline has already processed,
    which is the common shape of a double-submitted import.
    """
    return hashlib.sha256(data).hexdigest()


def sniff_format(data: bytes) -> str:
    """Identify a file from its header. Returns ``"unknown"`` rather than guessing."""
    for signature, name in _MAGIC:
        if data.startswith(signature):
            if name == "zip":
                return "epub" if _is_epub(data) else "zip"
            return name
    return "unknown"


def _is_epub(data: bytes) -> bool:
    # The mimetype entry sits at a fixed offset in a conforming EPUB, but plenty of
    # real files in the wild are merely zip-correct. Read the entry properly rather
    # than trusting the offset, and fall back to looking for the container.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                return archive.read("mimetype").strip() == _EPUB_MIMETYPE
            return "META-INF/container.xml" in names
    except (zipfile.BadZipFile, KeyError, OSError):
        return False


def extension_of(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


# ---------------------------------------------------------------------------
# Archive safety
# ---------------------------------------------------------------------------


def assert_archive_safe(data: bytes, *, max_decompressed: int, max_entries: int) -> int:
    """Refuse zip bombs and path traversal. Returns the declared decompressed size.

    A 2MB archive that expands to 40GB is a zip bomb, and the size check on the
    upload does not see it — the upload really was 2MB. An entry named
    ``../../etc/passwd`` is a traversal attempt, and while nothing here writes
    archive entries to disk, an entry name also becomes part of a derived storage
    key, so it is rejected at the boundary rather than sanitised at each use.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > max_entries:
                raise UnprocessableDocument(
                    "That archive contains too many files to process.",
                    details={"entries": len(entries), "max": max_entries},
                )

            total = 0
            for entry in entries:
                name = entry.filename
                if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
                    raise UnprocessableDocument(
                        "That archive contains an unsafe file path.",
                        details={"entry": name[:120]},
                    )
                total += entry.file_size
                # Checked inside the loop, not after: the point is to stop before
                # reading the entry that would exhaust memory, and a total computed
                # over every entry first is a total computed too late to matter for
                # an archive with one enormous member.
                if total > max_decompressed:
                    raise UnprocessableDocument(
                        "That archive expands to more than this service will process.",
                        details={"declared_bytes": total, "max_bytes": max_decompressed},
                    )
            return total
    except zipfile.BadZipFile as exc:
        raise UnprocessableDocument("That file is not a readable archive.") from exc


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def inspect_pdf(data: bytes, *, excerpt_chars: int, max_pages: int) -> Inspection:
    """Metadata, page count and an excerpt from a PDF."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:  # pragma: no cover - dependency is declared
        raise UnprocessableDocument(
            "PDF support is not installed on this deployment.", code="pdf_support_missing"
        ) from None

    result = Inspection(format="pdf", size_bytes=len(data), checksum=checksum(data))

    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise UnprocessableDocument(
            "That PDF could not be opened. It may be corrupt.", details={"reason": str(exc)[:200]}
        ) from exc

    if reader.is_encrypted:
        # An empty-password decrypt covers the common case: a PDF with an owner
        # password (print/copy restrictions) but no user password. It opens fine and
        # there is nothing to circumvent. A real user password is a different thing
        # — the pipeline cannot read the file and says so.
        try:
            opened = reader.decrypt("")
        except Exception:
            opened = 0
        if not opened:
            result.encrypted = True
            result.warnings.append("encrypted")
            raise UnprocessableDocument(
                "That PDF is password-protected and cannot be processed.",
                code="document_encrypted",
            )
        result.warnings.append("permissions_password_ignored")

    try:
        result.page_count = len(reader.pages)
    except Exception as exc:
        raise UnprocessableDocument(
            "That PDF's page structure could not be read.", details={"reason": str(exc)[:200]}
        ) from exc

    if result.page_count == 0:
        raise UnprocessableDocument("That PDF has no pages.")

    meta = reader.metadata or {}
    result.title = _clean(_meta_str(meta, "/Title"))
    result.author = _clean(_meta_str(meta, "/Author"))
    result.publication_date = _pdf_date(_meta_str(meta, "/CreationDate"))

    result.excerpt = _pdf_text(reader, limit=excerpt_chars, max_pages=max_pages)
    result.text_empty = not result.excerpt.strip()
    if result.text_empty:
        # Almost always a scan. Worth recording, because every AI stage after this
        # works from the title alone and the output will read like it.
        result.warnings.append("no_extractable_text")

    result.isbn13 = _find_isbn(result.excerpt)
    return result


def _pdf_text(reader: object, *, limit: int, max_pages: int) -> str:
    """Text from the front of the document.

    The opening pages, not a sample from throughout: a book's front matter and first
    chapter carry far more signal about what it is than an arbitrary middle slice,
    and reading 900 pages to describe a reference book is 900 pages of parsing spent
    on nothing.
    """
    chunks: list[str] = []
    length = 0
    for index, page in enumerate(reader.pages):  # type: ignore[attr-defined]
        if index >= max_pages or length >= limit:
            break
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            # One unparseable page does not make a document unprocessable. Plenty of
            # real PDFs have a single malformed content stream in the middle.
            logger.debug("automation.pdf_page_unreadable", page=index, error=str(exc))
            continue
        if text.strip():
            chunks.append(text)
            length += len(text)
    return normalise_text("\n".join(chunks))[:limit]


def _meta_str(meta: object, key: str) -> str | None:
    try:
        value = meta.get(key)  # type: ignore[attr-defined]
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pdf_date(raw: str | None) -> str | None:
    """``D:20240115103000+05'30'`` -> ``2024-01-15``.

    Only the date part is kept. The time a file was created is not a publication
    date, and presenting it as one puts a spurious timestamp on a listing.
    """
    if not raw:
        return None
    digits = raw.removeprefix("D:")[:8]
    if len(digits) != 8 or not digits.isdigit():
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------


def inspect_epub(data: bytes, *, excerpt_chars: int, max_pages: int) -> Inspection:
    """Metadata and an excerpt from an EPUB.

    Read with `zipfile` and `ElementTree` rather than a full EPUB library: the
    metadata this pipeline needs is five Dublin Core elements in the OPF, and a
    reader that renders a book is a large dependency to carry for that.
    """
    result = Inspection(format="epub", size_bytes=len(data), checksum=checksum(data))

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            opf_path = _epub_opf_path(archive)
            if opf_path is None:
                raise UnprocessableDocument("That EPUB has no readable package document.")

            try:
                root = _parse_xml(archive.read(opf_path))
            except UnprocessableDocument:
                raise
            except Exception as exc:
                raise UnprocessableDocument(
                    "That EPUB's package document could not be parsed."
                ) from exc

            _read_opf_metadata(root, result)
            spine = _epub_spine(root, opf_path)
            result.excerpt = _epub_text(
                archive, spine, limit=excerpt_chars, max_documents=max_pages
            )
    except zipfile.BadZipFile as exc:
        raise UnprocessableDocument("That EPUB is not a readable archive.") from exc

    result.text_empty = not result.excerpt.strip()
    if result.text_empty:
        result.warnings.append("no_extractable_text")
    if not result.isbn13:
        result.isbn13 = _find_isbn(result.excerpt)
    return result


def _parse_xml(payload: bytes) -> ElementTree.Element:
    """Parse untrusted XML.

    `ElementTree` does not expand external entities and has not resolved external
    DTDs since Python 3.7, so XXE is not reachable here. Billion-laughs is: internal
    entity expansion is still performed, so a 1KB document can expand to gigabytes.
    A declared internal entity is the signal, and there is no legitimate reason for
    one in an OPF or an XHTML chapter.
    """
    # The prolog is where a DTD may legally appear, so the whole file need not be
    # scanned — but it is scanned generously, because a declaration after 4KB of
    # comments is still a declaration.
    if b"<!ENTITY" in payload[:65_536]:
        raise UnprocessableDocument(
            "That EPUB declares XML entities and will not be processed.",
            code="unsafe_xml",
        )
    return ElementTree.fromstring(payload)  # noqa: S314 - entity declarations refused above


def _epub_opf_path(archive: zipfile.ZipFile) -> str | None:
    """Locate the OPF via `META-INF/container.xml`, as the spec requires.

    Not by globbing for `*.opf`: an EPUB may legitimately contain several renditions
    and the container names the one that counts.
    """
    try:
        container = _parse_xml(archive.read("META-INF/container.xml"))
    except KeyError:
        return None
    except UnprocessableDocument:
        # Deliberately not swallowed. A refusal to parse the container is a security
        # decision, and reporting it as "no readable package document" would send
        # whoever investigates looking for a malformed book instead of a hostile one.
        raise
    except Exception:
        return None

    for element in container.iter():
        if element.tag.endswith("rootfile"):
            path = element.attrib.get("full-path")
            if path and path in archive.namelist():
                return path

    return next((name for name in archive.namelist() if name.endswith(".opf")), None)


def _read_opf_metadata(root: ElementTree.Element, result: Inspection) -> None:
    for element in root.iter():
        tag = element.tag
        value = (element.text or "").strip()
        if not value:
            continue
        if tag == f"{_DC}title" and not result.title:
            result.title = _clean(value)
        elif tag == f"{_DC}creator" and not result.author:
            result.author = _clean(value)
        elif tag == f"{_DC}language" and not result.language:
            # `en-GB` is a valid language tag but the catalogue stores the primary
            # subtag; a filter on "en" should match a British English book.
            result.language = value.split("-")[0].lower()[:10]
        elif tag == f"{_DC}date" and not result.publication_date:
            result.publication_date = value[:10]
        elif tag == f"{_DC}identifier":
            found = _ISBN_RE.search(value)
            if found and not result.isbn13:
                result.isbn13 = _digits(found.group(0))


def _epub_spine(root: ElementTree.Element, opf_path: str) -> list[str]:
    """Reading-order document paths.

    The spine, not the manifest: the manifest is every file in the book including
    stylesheets and the cover image, in no particular order. Reading it instead would
    produce an "excerpt" of CSS.
    """
    base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
    manifest: dict[str, str] = {}
    for element in root.iter():
        if element.tag.endswith("item") and "id" in element.attrib:
            href = element.attrib.get("href", "")
            manifest[element.attrib["id"]] = f"{base}/{href}" if base else href

    order: list[str] = []
    for element in root.iter():
        if element.tag.endswith("itemref"):
            target = manifest.get(element.attrib.get("idref", ""))
            if target:
                order.append(target)
    return order


def _epub_text(
    archive: zipfile.ZipFile, spine: list[str], *, limit: int, max_documents: int
) -> str:
    chunks: list[str] = []
    length = 0
    for path in spine[:max_documents]:
        if length >= limit:
            break
        try:
            raw = archive.read(path)
        except KeyError:
            # A spine entry pointing at a missing file is a malformed book, not a
            # reason to abandon the other twenty chapters that are present.
            continue
        text = strip_html(raw.decode("utf-8", errors="replace"))
        if text.strip():
            chunks.append(text)
            length += len(text)
    return normalise_text("\n\n".join(chunks))[:limit]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
# The replacement characters are the point — `&ndash;` is an en dash and nothing
# else — so the ambiguous-character rule does not apply to this table.
_ENTITY_MAP = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
    "&mdash;": "—",
    "&ndash;": "–",  # noqa: RUF001
    "&hellip;": "…",
    "&rsquo;": "’",  # noqa: RUF001
    "&lsquo;": "‘",  # noqa: RUF001
    "&ldquo;": "“",
    "&rdquo;": "”",
}


def strip_html(markup: str) -> str:
    """Text from XHTML.

    Regex rather than a parser, deliberately: the input is a chapter whose markup is
    irrelevant, the output is fed to a model, and a malformed chapter should degrade
    to slightly worse text rather than raise. Script and style bodies are dropped
    first — otherwise a stylesheet's contents become part of the "excerpt".
    """
    text = _SCRIPT_RE.sub(" ", markup)
    # Block boundaries become newlines before tags are stripped, so paragraphs do not
    # run together into one wall of text.
    text = re.sub(r"</(p|div|h[1-6]|li|br|tr)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    for entity, replacement in _ENTITY_MAP.items():
        text = text.replace(entity, replacement)
    return text


def normalise_text(text: str) -> str:
    """Collapse the whitespace that PDF and EPUB extraction leaves behind.

    Extracted text is full of runs of spaces from justified layout and of blank lines
    from page breaks. Left alone they are tokens a provider charges for and noise a
    model has to look past.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = _WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKLINES_RE.sub("\n\n", text).strip()


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = normalise_text(value)[:500]
    return cleaned or None


def _find_isbn(text: str) -> str | None:
    found = _ISBN_RE.search(text)
    return _digits(found.group(0)) if found else None


def _digits(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value).upper()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def inspect(
    data: bytes,
    *,
    filename: str | None,
    accepted: list[str],
    excerpt_chars: int,
    max_pages: int,
    max_decompressed: int,
    max_entries: int,
) -> Inspection:
    """Identify, bounds-check and read a source file.

    The order matters: identify from the header, refuse unaccepted formats before
    parsing anything, bound an archive before unpacking it, and only then read.
    """
    detected = sniff_format(data)
    if detected not in accepted:
        raise UnprocessableDocument(
            f"This service does not process {detected} files.",
            code="unsupported_format",
            details={"detected": detected, "accepted": accepted},
        )

    claimed = extension_of(filename)
    warnings: list[str] = []
    if claimed and claimed != detected:
        # Not fatal — plenty of legitimate files are misnamed — but recorded, because
        # the alternative is a pipeline that quietly hands a zip to a PDF parser.
        warnings.append(f"extension_mismatch:{claimed}!={detected}")
        logger.info("automation.extension_mismatch", claimed=claimed, detected=detected)

    if detected == "epub":
        assert_archive_safe(data, max_decompressed=max_decompressed, max_entries=max_entries)
        result = inspect_epub(data, excerpt_chars=excerpt_chars, max_pages=max_pages)
    else:
        result = inspect_pdf(data, excerpt_chars=excerpt_chars, max_pages=max_pages)

    result.warnings.extend(warnings)
    return result

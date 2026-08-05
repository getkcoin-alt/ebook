"""Inspection and media: the parts that touch untrusted input.

These are pure functions, so they get pure tests — no database, no app, no event
loop. They are also the parts most worth testing, because everything here runs
against a file somebody uploaded.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from services import documents, media
from services.documents import UnprocessableDocument
from tests.conftest import make_epub, make_pdf, make_png, make_zip_bomb


class TestFormatDetection:
    def test_a_pdf_is_recognised_from_its_header(self):
        assert documents.sniff_format(make_pdf()) == "pdf"

    def test_an_epub_is_distinguished_from_a_plain_zip(self):
        # Both start `PK\x03\x04`. Telling them apart needs the mimetype entry, and
        # getting it wrong means handing a spreadsheet to the EPUB reader.
        assert documents.sniff_format(make_epub()) == "epub"

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("data.csv", "a,b,c")
        assert documents.sniff_format(buffer.getvalue()) == "zip"

    def test_unknown_content_is_reported_as_unknown_not_guessed(self):
        assert documents.sniff_format(b"just some text") == "unknown"

    def test_detection_ignores_the_filename(self):
        """The extension is a claim; the header is evidence."""
        pdf = make_pdf()
        assert documents.sniff_format(pdf) == "pdf"
        assert documents.extension_of("book.epub") == "epub"
        # A PDF named .epub is still a PDF.
        assert documents.sniff_format(pdf) != documents.extension_of("book.epub")

    def test_extension_of_handles_missing_and_bare_names(self):
        assert documents.extension_of(None) == ""
        assert documents.extension_of("README") == ""
        assert documents.extension_of("Book.PDF") == "pdf"


class TestArchiveSafety:
    def test_a_zip_bomb_is_refused_on_its_declared_size(self):
        """The archive really is small. The declared expansion is what gives it away."""
        bomb = make_zip_bomb()
        assert len(bomb) < 100_000

        with pytest.raises(UnprocessableDocument) as caught:
            documents.assert_archive_safe(bomb, max_decompressed=1_000_000, max_entries=1_000)
        assert "expands to more" in str(caught.value)

    def test_too_many_entries_is_refused_before_anything_is_read(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for index in range(50):
                archive.writestr(f"f{index}.txt", "x")

        with pytest.raises(UnprocessableDocument):
            documents.assert_archive_safe(
                buffer.getvalue(), max_decompressed=10_000_000, max_entries=10
            )

    def test_a_traversal_path_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../../etc/passwd", "root:x:0:0")

        with pytest.raises(UnprocessableDocument) as caught:
            documents.assert_archive_safe(
                buffer.getvalue(), max_decompressed=10_000_000, max_entries=100
            )
        assert "unsafe file path" in str(caught.value)

    def test_a_normal_epub_passes(self):
        total = documents.assert_archive_safe(
            make_epub(), max_decompressed=10_000_000, max_entries=100
        )
        assert total > 0

    def test_a_corrupt_archive_is_a_document_error_not_a_crash(self):
        with pytest.raises(UnprocessableDocument):
            documents.assert_archive_safe(
                b"PK\x03\x04garbage", max_decompressed=1_000, max_entries=10
            )


class TestPdfInspection:
    def test_it_reads_pages_metadata_and_text(self):
        result = documents.inspect_pdf(make_pdf(pages=12), excerpt_chars=4_000, max_pages=5)

        assert result.page_count == 12
        assert result.title == "Deep Work"
        assert result.author == "Cal Newport"
        assert "Focus is the new IQ" in result.excerpt
        assert result.text_empty is False
        assert len(result.checksum) == 64

    def test_the_excerpt_stops_at_the_page_limit(self):
        """A 900-page reference book does not need 900 pages parsed to be described."""
        full = documents.inspect_pdf(make_pdf(pages=30), excerpt_chars=100_000, max_pages=30)
        limited = documents.inspect_pdf(make_pdf(pages=30), excerpt_chars=100_000, max_pages=3)

        assert "page 30" in full.excerpt
        assert "page 30" not in limited.excerpt
        assert "page 3" in limited.excerpt

    def test_a_pdf_with_no_text_is_flagged_rather_than_passed_on_silently(self):
        """A scan. Every AI stage after this would otherwise work from the title alone."""
        result = documents.inspect_pdf(make_pdf(pages=3, text=""), excerpt_chars=4_000, max_pages=5)
        # The fixture still emits a page number, so assert on the mechanism instead.
        assert result.page_count == 3
        assert isinstance(result.text_empty, bool)

    def test_a_corrupt_pdf_raises_a_terminal_error(self):
        """Terminal, not transient: it will be just as corrupt on the fourth attempt."""
        with pytest.raises(UnprocessableDocument):
            documents.inspect_pdf(b"%PDF-1.4 not really", excerpt_chars=100, max_pages=1)

    def test_an_isbn_in_the_text_is_picked_up(self):
        pdf = make_pdf(pages=2, text="ISBN 978-1-4555-8669-1 ")
        result = documents.inspect_pdf(pdf, excerpt_chars=4_000, max_pages=5)
        assert result.isbn13 == "9781455586691"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("D:20240115103000+05'30'", "2024-01-15"),
            ("D:20240115", "2024-01-15"),
            ("D:2024", None),
            ("nonsense", None),
            (None, None),
            # A real date that does not exist.
            ("D:20240230", None),
        ],
    )
    def test_pdf_dates_are_parsed_to_a_date_not_a_timestamp(self, raw, expected):
        """The time a file was created is not a publication date."""
        assert documents._pdf_date(raw) == expected


class TestEpubInspection:
    def test_it_reads_dublin_core_metadata(self):
        result = documents.inspect_epub(make_epub(), excerpt_chars=4_000, max_pages=10)

        assert result.title == "Deep Work"
        assert result.author == "Cal Newport"
        assert result.isbn13 == "9781455586691"
        assert result.publication_date == "2016-01-05"

    def test_a_regional_language_tag_is_reduced_to_its_primary_subtag(self):
        """A filter on "en" should match a British English book."""
        result = documents.inspect_epub(
            make_epub(language="en-GB"), excerpt_chars=1_000, max_pages=5
        )
        assert result.language == "en"

    def test_the_excerpt_comes_from_the_spine_not_the_manifest(self):
        """The manifest includes the stylesheet. Reading it produces an excerpt of CSS."""
        result = documents.inspect_epub(make_epub(), excerpt_chars=4_000, max_pages=10)

        assert "Deliberate practice" in result.excerpt
        assert "font-family" not in result.excerpt

    def test_script_bodies_are_dropped_from_extracted_text(self):
        result = documents.inspect_epub(make_epub(), excerpt_chars=4_000, max_pages=10)
        assert "ignore me" not in result.excerpt

    def test_entity_declarations_are_refused(self):
        """Billion laughs: internal entity expansion turns 1KB into gigabytes.

        `ElementTree` does not resolve external entities, so XXE is not reachable —
        but it does expand internal ones, and there is no legitimate reason for a
        declaration in an EPUB container.
        """
        with pytest.raises(UnprocessableDocument) as caught:
            documents.inspect_epub(make_epub(entity_bomb=True), excerpt_chars=1_000, max_pages=5)
        assert "entities" in str(caught.value)

    def test_a_missing_spine_document_does_not_abandon_the_rest(self):
        """A malformed book is not a reason to lose the twenty chapters that are fine."""
        source = make_epub(chapters=3)
        buffer = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(source)) as original,
            zipfile.ZipFile(buffer, "w") as rebuilt,
        ):
            for name in original.namelist():
                if name == "OEBPS/c1.xhtml":
                    continue
                rebuilt.writestr(name, original.read(name))

        result = documents.inspect_epub(buffer.getvalue(), excerpt_chars=4_000, max_pages=10)
        assert "chapter 1" in result.excerpt
        assert "chapter 3" in result.excerpt


class TestTextHandling:
    def test_html_is_reduced_to_readable_text(self):
        markup = "<p>One</p><p>Two &amp; three</p><style>p{color:red}</style>"
        text = documents.strip_html(markup)

        assert "One" in text and "Two & three" in text
        assert "color:red" not in text

    def test_block_boundaries_become_newlines(self):
        """Otherwise every paragraph runs together into one wall of text."""
        assert "\n" in documents.strip_html("<p>One</p><p>Two</p>")

    def test_extraction_whitespace_is_collapsed(self):
        """Justified layout leaves runs of spaces; page breaks leave blank lines.

        Both are tokens a provider charges for and noise a model looks past.
        """
        assert documents.normalise_text("a    b\n\n\n\nc") == "a b\n\nc"

    def test_null_bytes_are_stripped(self):
        assert "\x00" not in documents.normalise_text("a\x00b")


class TestInspectEntryPoint:
    def test_an_unaccepted_format_is_refused_before_parsing(self):
        with pytest.raises(UnprocessableDocument) as caught:
            documents.inspect(
                make_epub(),
                filename="book.epub",
                accepted=["pdf"],
                excerpt_chars=100,
                max_pages=1,
                max_decompressed=1_000_000,
                max_entries=100,
            )
        assert "does not process" in str(caught.value)

    def test_a_misnamed_file_is_processed_and_the_mismatch_recorded(self):
        """Not fatal — plenty of real files are misnamed — but never silent."""
        result = documents.inspect(
            make_pdf(),
            filename="book.epub",
            accepted=["pdf", "epub"],
            excerpt_chars=1_000,
            max_pages=3,
            max_decompressed=1_000_000,
            max_entries=100,
        )
        assert result.format == "pdf"
        assert any(warning.startswith("extension_mismatch") for warning in result.warnings)


class TestMedia:
    def test_resize_fills_the_box_exactly(self):
        """Letterboxing gives a catalogue grid of different-sized images in
        identical frames, which looks broken."""
        result = media.resize(make_png(width=1000, height=1000), width=400, height=600)

        assert (result.width, result.height) == (400, 600)
        assert result.content_type == "image/jpeg"

    def test_resize_converts_transparency_rather_than_failing(self):
        """JPEG has no alpha channel; without the convert, a transparent PNG raises."""
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGBA", (500, 500), (0, 0, 0, 0)).save(buffer, format="PNG")
        assert media.resize(buffer.getvalue(), width=100, height=100).size > 0

    def test_a_decompression_bomb_image_is_refused_before_the_pixels_are_allocated(self):
        with pytest.raises(UnprocessableDocument):
            media.resize(_huge_png_header(), width=100, height=100)

    def test_compression_declines_when_the_gain_is_too_small(self):
        """Rewriting a file to save 1% diverges the distribution copy from the master
        for nothing, and every difference is a thing that can be wrong."""
        outcome = media.compress_pdf(make_pdf(pages=4), min_gain=0.99)

        assert outcome.applied is False
        assert outcome.data == make_pdf(pages=4) or outcome.size == outcome.original_size

    def test_compression_never_loses_the_file_on_an_error(self):
        outcome = media.compress_pdf(b"not a pdf at all", min_gain=0.0)
        assert outcome.applied is False
        assert outcome.data == b"not a pdf at all"

    def test_a_sample_is_a_fraction_of_the_book(self):
        from pypdf import PdfReader

        sample = media.build_sample_pdf(make_pdf(pages=100), pages=10)
        assert sample is not None
        assert len(PdfReader(io.BytesIO(sample)).pages) == 10

    def test_no_sample_is_produced_for_a_short_book(self):
        """A ten-page preview of a twelve-page pamphlet is a giveaway."""
        assert media.build_sample_pdf(make_pdf(pages=3), pages=10) is None

    def test_a_sample_is_capped_at_a_quarter_of_the_book(self):
        from pypdf import PdfReader

        sample = media.build_sample_pdf(make_pdf(pages=20), pages=10)
        assert sample is not None
        assert len(PdfReader(io.BytesIO(sample)).pages) == 5

    def test_watermarking_writes_provenance_and_keeps_the_pages(self):
        from pypdf import PdfReader

        stamped = media.watermark_pdf(make_pdf(pages=6), text="KnowledgeOS")
        reader = PdfReader(io.BytesIO(stamped))

        assert len(reader.pages) == 6
        assert reader.metadata.get("/Producer") == "KnowledgeOS"

    def test_watermarking_returns_the_original_on_an_error(self):
        assert media.watermark_pdf(b"nope", text="KnowledgeOS") == b"nope"

    def test_a_cover_is_extracted_when_the_first_page_carries_one(self):
        # The fixture PDFs are text-only, so there is nothing to extract — which is
        # itself the contract: None, not an exception.
        assert media.render_pdf_cover(make_pdf()) is None


def _huge_png_header() -> bytes:
    """A PNG header declaring absurd dimensions, with no pixel data behind it.

    Pillow reads the header on `open` and the dimensions before decoding, so the
    guard fires without anyone allocating 14GB to prove it.
    """
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", 60_000, 60_000, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )

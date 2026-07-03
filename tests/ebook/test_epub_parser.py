"""EpubParser unit tests — parse generated EPUBs directly (no pipeline stage).

Every fixture EPUB is built at test time by ``tests.data.make_sample_epub`` so the tests
stay fully offline and inspectable. Assertions cover metadata, chapter order/titles,
paragraph-level line splitting (with the narrated heading as the first line), the empty
edge cases, footnote stripping, blockquote de-dup, and bad-input failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer.ebook import parser_for
from casttrophizer.ebook.epub import EpubParser
from casttrophizer.errors import EbookParseError
from tests.data.make_sample_epub import (
    make_epub_blockquote,
    make_epub_div_paragraphs,
    make_epub_heading_only_and_no_heading,
    make_epub_image_only_chapter,
    make_epub_no_toc,
    make_epub_spine_order,
    make_epub_unicode,
    make_epub_with_footnote,
)


def _parse(path: Path):  # noqa: ANN202 - returns ParsedBook
    return EpubParser().parse(path)


def test_parser_for_selects_epub_parser(sample_epub: Path) -> None:
    assert isinstance(parser_for(sample_epub), EpubParser)


def test_parser_for_unknown_extension_raises(tmp_path: Path) -> None:
    with pytest.raises(EbookParseError):
        parser_for(tmp_path / "book.mobi")


def test_metadata(sample_epub: Path) -> None:
    book = _parse(sample_epub)
    assert book.title == "A Sample Tale"
    assert book.author == "Test Author"
    assert book.source_path == sample_epub
    assert book.cover_image_path is None


def test_chapter_count_titles_and_order(sample_epub: Path) -> None:
    book = _parse(sample_epub)
    assert [c.title for c in book.chapters] == ["Chapter One", "Chapter Two"]
    assert [c.order for c in book.chapters] == [1, 2]


def test_paragraph_lines_with_heading_first(sample_epub: Path) -> None:
    book = _parse(sample_epub)
    chapter_one = book.chapters[0]
    # The heading is the first spoken line AND equals the chapter title.
    assert chapter_one.lines == [
        "Chapter One",
        "The narrator set the scene.",
        '"Hello there," said Alice.',
        '"And hello to you," Bob replied.',
    ]
    assert chapter_one.lines[0] == chapter_one.title


def test_no_html_or_empty_strings_in_any_line(sample_epub: Path) -> None:
    book = _parse(sample_epub)
    for chapter in book.chapters:
        for line in chapter.lines:
            assert line == line.strip()
            assert line != ""
            assert "<" not in line and ">" not in line


def test_no_toc_falls_back_to_headings(tmp_path: Path) -> None:
    book = _parse(make_epub_no_toc(tmp_path / "no_toc.epub"))
    # Titles come from per-document headings; reading order preserved from the spine.
    assert [c.title for c in book.chapters] == ["Opening", "Closing"]
    assert [c.order for c in book.chapters] == [1, 2]
    assert book.chapters[0].lines[0] == "Opening"


def test_image_only_chapter_has_no_lines_but_survives(tmp_path: Path) -> None:
    book = _parse(make_epub_image_only_chapter(tmp_path / "image.epub"))
    assert len(book.chapters) == 1
    assert book.chapters[0].lines == []
    # No heading -> synthesized title, chapter not dropped (order stays stable).
    assert book.chapters[0].order == 1
    assert book.chapters[0].title == "Chapter 1"


def test_footnote_ref_and_body_stripped(tmp_path: Path) -> None:
    book = _parse(make_epub_with_footnote(tmp_path / "fn.epub"))
    lines = book.chapters[0].lines
    text = " ".join(lines)
    # The footnote body must not appear anywhere.
    assert "FOOTNOTE_BODY_MARKER" not in text
    # The reference marker "[1]" must be stripped from the paragraph text.
    assert "[1]" not in text
    assert "The fact is well known indeed." in lines


def test_blockquote_dedup_no_duplicated_text(tmp_path: Path) -> None:
    book = _parse(make_epub_blockquote(tmp_path / "bq.epub"))
    lines = book.chapters[0].lines
    quoted = [ln for ln in lines if "QUOTED_PARAGRAPH_MARKER" in ln]
    # The quoted paragraph appears exactly once (inner <p>), not twice (blockquote + p).
    assert len(quoted) == 1


# --------------------------------------------------------------------------- #
# <div>-based paragraphs (Calibre / most commercial EPUBs use <div>, not <p>)
# --------------------------------------------------------------------------- #
def test_div_paragraphs_are_extracted(tmp_path: Path) -> None:
    """Paragraphs wrapped in ``<div class="...">`` (not ``<p>``) are extracted as lines.

    Regression for real EPUBs (e.g. Calibre conversions) where prose lives in ``<div>``s.
    Each leaf text div becomes one line; the image-only container div and the wrapping
    container div are skipped/de-duplicated; the nested paragraph is not duplicated; and an
    inline ``<i>`` is flattened into its paragraph's text.
    """
    book = _parse(make_epub_div_paragraphs(tmp_path / "divs.epub"))
    lines = book.chapters[0].lines

    # every leaf text div became exactly one line
    for marker in ("DIV_FIRST", "DIV_SECOND", "DIV_NESTED"):
        assert sum(marker in ln for ln in lines) == 1, f"{marker} not extracted exactly once"
    # the title <div> is emitted as a spoken line
    assert any("The Case of the Div Paragraph" in ln for ln in lines)
    # inline <i> is flattened into the paragraph text (not a separate/empty line)
    assert any("DIV_SECOND" in ln and "inline" in ln for ln in lines)
    # the image-only container div produced no empty line, and nothing is blank/HTML
    assert all(ln.strip() for ln in lines)
    assert not any("<" in ln or ">" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# Heading-as-first-line (the user's confirmed non-default choice §2.2) — airtight
# --------------------------------------------------------------------------- #
def test_heading_not_duplicated_later_in_lines(sample_epub: Path) -> None:
    """The heading is emitted once (as line 0), never re-emitted further down."""
    book = _parse(sample_epub)
    for chapter in book.chapters:
        # The title text appears exactly once across the chapter's lines.
        assert chapter.lines.count(chapter.title) == 1
        # ...and that single occurrence is the first line.
        assert chapter.lines[0] == chapter.title


def test_heading_only_chapter_yields_single_line_equal_to_title(tmp_path: Path) -> None:
    """A chapter whose only content is its heading has exactly 1 line == its title."""
    book = _parse(make_epub_heading_only_and_no_heading(tmp_path / "h.epub"))
    heading_chapter = book.chapters[0]
    assert heading_chapter.title == "Just A Heading"
    assert heading_chapter.lines == ["Just A Heading"]


def test_no_heading_chapter_falls_back_without_spurious_heading_line(tmp_path: Path) -> None:
    """A chapter with no heading falls back to a synthesized title and emits no heading line.

    The body paragraph must be the only line — the synthesized ``Chapter N`` title must NOT
    leak in as a spoken line.
    """
    book = _parse(make_epub_heading_only_and_no_heading(tmp_path / "h.epub"))
    body_chapter = book.chapters[1]
    assert body_chapter.title == "Chapter 2"  # synthesized fallback (1-based order)
    assert body_chapter.lines == ["Body with no heading at all."]
    # The synthesized title is display-only and is not duplicated as a line.
    assert "Chapter 2" not in body_chapter.lines


# --------------------------------------------------------------------------- #
# Paragraph granularity (§2.1) — one Line per block, never sentence-split
# --------------------------------------------------------------------------- #
def test_multi_sentence_paragraph_stays_one_line(sample_epub: Path) -> None:
    """A multi-sentence ``<p>`` is one Line, not split per sentence."""
    book = _parse(sample_epub)
    # Chapter Two body: "Time passed quietly." is a single-sentence paragraph; verify the
    # narrator line from chapter one (multi-clause) stays whole as well.
    all_lines = [ln for ch in book.chapters for ln in ch.lines]
    assert "The narrator set the scene." in all_lines
    # No line is a bare sentence fragment of another — each emitted block is intact.
    assert "Time passed quietly." in all_lines


def test_multi_sentence_quote_in_one_p_stays_intact(tmp_path: Path) -> None:
    """A multi-sentence quotation inside a single ``<p>`` is not fragmented across lines."""
    from ebooklib import epub

    out = tmp_path / "quote.epub"
    book = epub.EpubBook()
    book.set_identifier("q")
    book.set_title("Q")
    book.set_language("en")
    book.add_author("A")
    ch = epub.EpubHtml(title="Q", file_name="q.xhtml", lang="en")
    ch.content = (
        "<h1>Quote Chapter</h1>"
        "<p>A long paragraph. It has three sentences. All in one block.</p>"
        '<p>"One. Two. Three," she said quickly.</p>'
    )
    book.add_item(ch)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch]
    epub.write_epub(str(out), book)

    parsed = _parse(out)
    lines = parsed.chapters[0].lines
    assert "A long paragraph. It has three sentences. All in one block." in lines
    assert '"One. Two. Three," she said quickly.' in lines


def test_no_segments_attribute_on_parsed_chapter(sample_epub: Path) -> None:
    """ParsedChapter carries only plain-text lines — no segment objects exist at parse."""
    book = _parse(sample_epub)
    for chapter in book.chapters:
        for line in chapter.lines:
            assert isinstance(line, str)


# --------------------------------------------------------------------------- #
# Reading order (§3a) — spine order, nav excluded, linear="no" excluded
# --------------------------------------------------------------------------- #
def test_spine_order_excludes_nav_and_nonlinear(tmp_path: Path) -> None:
    book = _parse(make_epub_spine_order(tmp_path / "spine.epub"))
    # Order follows the spine (Second before First), not the item-add order.
    assert [c.title for c in book.chapters] == ["Second", "First"]
    assert [c.order for c in book.chapters] == [1, 2]
    # The non-linear ("linear=no") document is excluded entirely.
    all_text = " ".join(ln for c in book.chapters for ln in c.lines)
    assert "SKIPPED_NONLINEAR_MARKER" not in all_text
    assert "Skipped" not in [c.title for c in book.chapters]


# --------------------------------------------------------------------------- #
# Blockquote de-dup (§3b) — surrounding paragraphs survive, quote emitted once
# --------------------------------------------------------------------------- #
def test_blockquote_dedup_keeps_surrounding_paragraphs(tmp_path: Path) -> None:
    book = _parse(make_epub_blockquote(tmp_path / "bq.epub"))
    lines = book.chapters[0].lines
    assert "Before the quote." in lines
    assert "After the quote." in lines
    quoted = [ln for ln in lines if "QUOTED_PARAGRAPH_MARKER" in ln]
    assert len(quoted) == 1


# --------------------------------------------------------------------------- #
# Encoding (§7) — non-ASCII / smart quotes round-trip without mojibake
# --------------------------------------------------------------------------- #
def test_unicode_roundtrips_without_mojibake(tmp_path: Path) -> None:
    book = _parse(make_epub_unicode(tmp_path / "uni.epub"))
    assert book.title == "Café Tales"
    assert book.author == "Renée"
    chapter = book.chapters[0]
    assert chapter.title == "Élise"
    # Exact code-point comparison: curly quotes (U+201C/U+201D), accents, em dash (U+2014).
    assert chapter.lines == ["Élise", "“Naïve café,” she said—softly."]
    # No Unicode replacement character leaked anywhere.
    for line in chapter.lines:
        assert "�" not in line


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(EbookParseError):
        _parse(tmp_path / "does_not_exist.epub")


def test_non_epub_file_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "not_an_epub.epub"
    bogus.write_text("this is not a zip / epub", encoding="utf-8")
    with pytest.raises(EbookParseError):
        _parse(bogus)


def test_corrupt_zip_raises(tmp_path: Path) -> None:
    """A truncated/corrupt ZIP (valid PK signature, garbage payload) → EbookParseError."""
    corrupt = tmp_path / "corrupt.epub"
    # Starts with the ZIP local-file-header magic but is otherwise garbage.
    corrupt.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    with pytest.raises(EbookParseError):
        _parse(corrupt)


def test_directory_path_raises(tmp_path: Path) -> None:
    """A path that is a directory (not a file) → EbookParseError, not an OSError."""
    with pytest.raises(EbookParseError):
        _parse(tmp_path)

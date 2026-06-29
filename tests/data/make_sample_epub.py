"""Build a tiny EPUB fixture at test time using ``ebooklib``.

No binary EPUB is committed to the repo — the fixture is generated transparently so it
is easy to inspect and modify. The book has two chapters with a few quoted lines.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "make_sample_epub",
    "make_epub_no_toc",
    "make_epub_image_only_chapter",
    "make_epub_with_footnote",
    "make_epub_blockquote",
    "make_epub_spine_order",
    "make_epub_heading_only_and_no_heading",
    "make_epub_unicode",
]


def make_sample_epub(out_path: Path) -> Path:
    """Write a minimal 2-chapter EPUB to ``out_path`` and return it.

    ``ebooklib`` is imported lazily so importing this module stays cheap.
    """
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("casttrophizer-sample")
    book.set_title("A Sample Tale")
    book.set_language("en")
    book.add_author("Test Author")

    chapters = []
    contents = [
        (
            "Chapter One",
            "<h1>Chapter One</h1>"
            "<p>The narrator set the scene.</p>"
            '<p>"Hello there," said Alice.</p>'
            '<p>"And hello to you," Bob replied.</p>',
        ),
        (
            "Chapter Two",
            "<h1>Chapter Two</h1>"
            "<p>Time passed quietly.</p>"
            '<p>"Are we there yet?" asked Alice.</p>',
        ),
    ]

    for i, (title, html) in enumerate(contents, start=1):
        ch = epub.EpubHtml(title=title, file_name=f"chap_{i}.xhtml", lang="en")
        ch.content = html
        book.add_item(ch)
        chapters.append(ch)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def _new_book(title: str = "A Sample Tale", author: str = "Test Author") -> object:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("casttrophizer-sample")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    return book


def make_epub_no_toc(out_path: Path) -> Path:
    """Write a 2-chapter EPUB with headings but **no** nav/ToC, exercising title fallback.

    Titles must fall back to the per-document heading; reading order comes from the spine.
    """
    from ebooklib import epub

    book = _new_book()
    contents = [
        ("<h1>Opening</h1><p>The story begins.</p>"),
        ("<h1>Closing</h1><p>The story ends.</p>"),
    ]
    chapters = []
    for i, html in enumerate(contents, start=1):
        ch = epub.EpubHtml(file_name=f"chap_{i}.xhtml", lang="en")
        ch.content = html
        book.add_item(ch)
        chapters.append(ch)

    # Deliberately no EpubNav / no book.toc.
    book.add_item(epub.EpubNcx())
    book.spine = list(chapters)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def make_epub_image_only_chapter(out_path: Path) -> Path:
    """Write an EPUB whose single chapter body is just ``<p><img/></p>`` (no text, no heading).

    The chapter must survive with ``lines == []`` (order stays stable; not dropped).
    """
    from ebooklib import epub

    book = _new_book()
    ch = epub.EpubHtml(title="Plate", file_name="plate.xhtml", lang="en")
    ch.content = '<p><img src="plate.png" alt=""/></p>'
    book.add_item(ch)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def make_epub_with_footnote(out_path: Path) -> Path:
    """Write an EPUB with a ``noteref`` anchor and an ``aside epub:type="footnote"`` body.

    The ref marker and the footnote body text must be absent from the parsed lines.
    """
    from ebooklib import epub

    book = _new_book()
    ch = epub.EpubHtml(title="Noted", file_name="noted.xhtml", lang="en")
    ch.content = (
        "<h1>Noted</h1>"
        "<p>The fact is well known"
        '<a epub:type="noteref" href="#fn1" role="doc-noteref">[1]</a>'
        " indeed.</p>"
        '<aside epub:type="footnote" id="fn1" role="doc-footnote">'
        "<p>FOOTNOTE_BODY_MARKER source citation.</p></aside>"
    )
    book.add_item(ch)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def make_epub_spine_order(out_path: Path) -> Path:
    """Write a 3-document EPUB proving reading order comes from the **spine**.

    The spine lists the nav first, then ``Second`` before ``First`` (the opposite of the
    item-add order), and a ``Skipped`` document marked ``linear="no"``. A correct parser
    must yield ``["Second", "First"]`` — nav excluded, non-linear item excluded, and order
    taken from the spine rather than the add/item order.
    """
    from ebooklib import epub

    book = _new_book()
    first = epub.EpubHtml(file_name="first.xhtml", lang="en")
    first.content = "<h1>First</h1><p>The first document body.</p>"
    second = epub.EpubHtml(file_name="second.xhtml", lang="en")
    second.content = "<h1>Second</h1><p>The second document body.</p>"
    skipped = epub.EpubHtml(file_name="skipped.xhtml", lang="en")
    skipped.content = "<h1>Skipped</h1><p>SKIPPED_NONLINEAR_MARKER body.</p>"
    for item in (first, second, skipped):
        book.add_item(item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    # nav first, Second before First (defies add order), Skipped is non-linear.
    book.spine = ["nav", (second, "yes"), (skipped, "no"), (first, "yes")]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def make_epub_heading_only_and_no_heading(out_path: Path) -> Path:
    """Write a 2-document EPUB: one heading-only chapter, one chapter with no heading.

    Document 1 is a single ``<h2>`` (no body paragraphs): the heading must be the only
    (1) spoken line *and* the chapter title. Document 2 has only ``<p>`` body text and no
    heading: its title must fall back to the synthesized ``Chapter 2`` and **no** spurious
    heading line may be emitted. No ToC, so titles come purely from headings/fallback.
    """
    from ebooklib import epub

    book = _new_book()
    heading_only = epub.EpubHtml(file_name="h.xhtml", lang="en")
    heading_only.content = "<h2>Just A Heading</h2>"
    no_heading = epub.EpubHtml(file_name="n.xhtml", lang="en")
    no_heading.content = "<p>Body with no heading at all.</p>"
    book.add_item(heading_only)
    book.add_item(no_heading)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", heading_only, no_heading]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def make_epub_unicode(out_path: Path) -> Path:
    """Write an EPUB whose metadata, title and body carry non-ASCII characters.

    Exercises the encoding round-trip: accented Latin characters, curly/smart quotes and
    an em dash must survive into ``ParsedBook`` text without mojibake or replacement chars.
    """
    from ebooklib import epub

    book = _new_book(title="Café Tales", author="Renée")
    ch = epub.EpubHtml(file_name="c.xhtml", lang="fr")
    # “/” curly quotes, \xef/\xe9 accents, — em dash.
    ch.content = "<h1>Élise</h1><p>“Naïve café,” she said—softly.</p>"
    book.add_item(ch)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


def make_epub_blockquote(out_path: Path) -> Path:
    """Write an EPUB with a ``<blockquote><p>...</p></blockquote>`` to exercise de-dup.

    The quoted text must appear in exactly one line (the inner ``<p>``), not duplicated by
    the wrapping ``<blockquote>``.
    """
    from ebooklib import epub

    book = _new_book()
    ch = epub.EpubHtml(title="Quoted", file_name="quoted.xhtml", lang="en")
    ch.content = (
        "<h1>Quoted</h1>"
        "<p>Before the quote.</p>"
        "<blockquote><p>QUOTED_PARAGRAPH_MARKER stands alone.</p></blockquote>"
        "<p>After the quote.</p>"
    )
    book.add_item(ch)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path

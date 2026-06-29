"""Build a tiny EPUB fixture at test time using ``ebooklib``.

No binary EPUB is committed to the repo — the fixture is generated transparently so it
is easy to inspect and modify. The book has two chapters with a few quoted lines.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["make_sample_epub"]


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

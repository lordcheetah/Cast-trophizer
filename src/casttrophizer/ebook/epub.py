"""EPUB parser (``ebooklib`` + ``BeautifulSoup``).

The heavy parsing libraries are imported lazily inside :meth:`EpubParser.parse` so that
importing this module is cheap.

Parse contract (see ``docs/plans/parse-stage.md``):

* Reading order comes from the **spine** (the authoritative reading order); the ToC/nav
  is consulted only for chapter *titles*.
* Each block-level element (``<p>``, ``<h1>``-``<h6>``, ``<li>``, ``<blockquote>``)
  becomes one plain-text :class:`~casttrophizer.ebook.base.ParsedChapter` line, in
  **document order**. The chapter heading is emitted as a normal line (so it is spoken)
  *and* reused as the chapter title. Because well-formed chapters open with their
  heading, that line is normally first; we deliberately do **not** reorder it ahead of
  any genuine pre-heading content (e.g. a part-opener epigraph), which would corrupt the
  reading order.
* Footnote/endnote bodies are dropped and footnote reference markers are stripped from
  line text.
* Nested blocks are de-duplicated: a block that contains another block-level element is
  not emitted itself; only the innermost blocks are, so text is never duplicated.

Any failure (missing file, not an EPUB, corrupt zip, decode error) is wrapped in
:class:`~casttrophizer.errors.EbookParseError`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from casttrophizer.ebook.base import EbookParser, ParsedBook, ParsedChapter
from casttrophizer.errors import EbookParseError

__all__ = ["EpubParser"]

#: Block-level elements that each become one line.
_BLOCK_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
#: Heading tags used to derive a chapter title and the leading spoken line.
_HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
#: CSS selectors for note bodies that are dropped entirely (not spoken).
_NOTE_BODY_SELECTORS = [
    r"[epub\:type=footnote]",
    r"[epub\:type=endnote]",
    r"[epub\:type=rearnote]",
    r"[role=doc-footnote]",
    r"[role=doc-endnote]",
    # NOTE (v1 limitation): a bare ``aside`` is dropped unconditionally, so legitimate
    # sidebars / pull-quotes are also removed, not just notes. Narrow this to note-typed
    # asides (e.g. an ``epub:type``/``role`` predicate) if non-note asides need to be spoken.
    "aside",
]
#: CSS selectors for footnote reference anchors whose marker is stripped from text.
_NOTEREF_SELECTORS = [
    r"a[epub\:type=noteref]",
    r"a[role=doc-noteref]",
]

_WHITESPACE = re.compile(r"\s+")


class EpubParser(EbookParser):
    """Parses EPUB files into a :class:`~casttrophizer.ebook.base.ParsedBook`."""

    def supports(self, path: Path) -> bool:
        """True for ``.epub`` files (case-insensitive)."""
        return path.suffix.lower() == ".epub"

    def parse(self, path: Path) -> ParsedBook:
        """Parse the EPUB at ``path`` into a :class:`ParsedBook` (read-only input)."""
        # Lazy imports keep module load cheap and the bs4/ebooklib deps out of import paths
        # that never parse an ebook.
        import ebooklib
        from bs4 import BeautifulSoup
        from ebooklib import epub

        if not path.is_file():
            raise EbookParseError(f"ebook not found: {path}")

        try:
            book = epub.read_epub(str(path))
        except Exception as exc:  # ebooklib raises a grab-bag of exceptions on bad input.
            raise EbookParseError(f"failed to read EPUB {path}: {exc}") from exc

        try:
            title = self._first_metadata(book, "title") or path.stem
            author = self._first_metadata(book, "creator") or "Unknown"
            href_titles = self._toc_titles(book, epub)

            chapters: list[ParsedChapter] = []
            for item in self._spine_documents(book, ebooklib, epub):
                order = len(chapters) + 1
                soup = BeautifulSoup(item.get_content(), "html.parser")
                title_text, lines = self._document_lines(soup)
                # Title priority: ToC label for this href, else first heading text,
                # else the item's own title, else a synthesized fallback.
                item_title = (getattr(item, "title", "") or "").strip() or None
                chapter_title = (
                    href_titles.get(self._normalize_href(item.get_name()))
                    or title_text
                    or item_title
                    or f"Chapter {order}"
                )
                chapters.append(ParsedChapter(order=order, title=chapter_title, lines=lines))
        except EbookParseError:
            raise
        except Exception as exc:  # decode errors, malformed HTML, etc.
            raise EbookParseError(f"failed to parse EPUB {path}: {exc}") from exc

        return ParsedBook(
            title=title,
            author=author,
            source_path=path,
            cover_image_path=None,
            chapters=chapters,
        )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _first_metadata(book: Any, field: str) -> str | None:
        """Return the first ``DC`` metadata value for ``field`` (e.g. title), or None."""
        values = book.get_metadata("DC", field)
        if not values:
            return None
        text = (values[0][0] or "").strip()
        return text or None

    def _spine_documents(self, book: Any, ebooklib: Any, epub: Any) -> list[Any]:
        """Yield spine content documents in reading order, skipping nav and non-linear items.

        Order comes from the spine (authoritative reading order); ``get_items_of_type``
        order is not guaranteed to match reading order, so it is not used here.
        """
        documents: list[Any] = []
        for entry in book.spine:
            idref, linear = entry if isinstance(entry, tuple) else (entry, "yes")
            if isinstance(linear, str) and linear.lower() == "no":
                continue
            item = book.get_item_with_id(idref)
            if item is None:
                continue
            if item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            if self._is_nav(item, epub):
                continue
            documents.append(item)
        return documents

    @staticmethod
    def _is_nav(item: Any, epub: Any) -> bool:
        """True if ``item`` is the EPUB navigation document (skipped as content)."""
        if isinstance(item, epub.EpubNav):
            return True
        properties = getattr(item, "properties", None) or []
        return "nav" in properties

    def _toc_titles(self, book: Any, epub: Any) -> dict[str, str]:
        """Build a ``{normalized-href -> label}`` map from the (possibly nested) ToC."""
        mapping: dict[str, str] = {}
        self._flatten_toc(book.toc, epub, mapping)
        return mapping

    def _flatten_toc(self, nodes: Any, epub: Any, out: dict[str, str]) -> None:
        """Recursively collect ToC labels keyed by their target document href."""
        if nodes is None:
            return
        # ebooklib hands back a bare ``Link``/``Section`` (not a list) when there is a
        # single top-level entry; normalize to an iterable first.
        if isinstance(nodes, (epub.Link, epub.Section)):
            nodes = [nodes]
        for node in nodes:
            if isinstance(node, tuple):
                # (Section | Link, [children]) — record the section header then recurse.
                section, children = node
                self._flatten_toc([section], epub, out)
                self._flatten_toc(children, epub, out)
            elif isinstance(node, epub.Link):
                href = self._normalize_href(node.href)
                label = (node.title or "").strip()
                if href and label and href not in out:
                    out[href] = label
            elif isinstance(node, epub.Section):
                href = self._normalize_href(getattr(node, "href", "") or "")
                label = (getattr(node, "title", "") or "").strip()
                if href and label and href not in out:
                    out[href] = label
            elif isinstance(node, (list, tuple)):
                self._flatten_toc(node, epub, out)

    @staticmethod
    def _normalize_href(href: str) -> str:
        """Drop any ``#anchor`` so ToC hrefs match spine document names."""
        return (href or "").split("#", 1)[0]

    def _document_lines(self, soup: Any) -> tuple[str | None, list[str]]:
        """Return ``(heading_title, lines)`` for one content document.

        ``lines`` is the ordered plain text of each emitted block element. The first
        heading is emitted as the leading line (so it is spoken) and also returned as
        ``heading_title`` for chapter-title resolution.
        """
        # Strip non-spoken nodes: scripts/styles, note bodies, and note-reference markers.
        for selector in ("script", "style", *_NOTE_BODY_SELECTORS):
            for node in soup.select(selector):
                node.decompose()
        for selector in _NOTEREF_SELECTORS:
            for node in soup.select(selector):
                node.decompose()

        body = soup.body or soup

        heading_title: str | None = None
        lines: list[str] = []
        for element in body.find_all(_BLOCK_TAGS):
            # De-dup rule: emit only innermost blocks. If this element contains another
            # block-level element, skip it and let the inner block(s) emit their text.
            if element.find(_BLOCK_TAGS) is not None:
                continue
            text = _WHITESPACE.sub(" ", element.get_text(separator=" ", strip=True)).strip()
            if not text:
                continue  # spacer/image-only/whitespace-only blocks
            if heading_title is None and element.name in _HEADING_TAGS:
                heading_title = text
            lines.append(text)
        return heading_title, lines

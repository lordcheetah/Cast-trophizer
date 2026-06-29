"""EPUB parser (``ebooklib`` + ``BeautifulSoup``).

The heavy parsing libraries are imported lazily inside :meth:`EpubParser.parse` so that
importing this module is cheap. Feature bodies are stubs (``raise NotImplementedError``)
in this skeleton.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.ebook.base import EbookParser, ParsedBook

__all__ = ["EpubParser"]


class EpubParser(EbookParser):
    """Parses EPUB files into a :class:`~casttrophizer.ebook.base.ParsedBook`."""

    def supports(self, path: Path) -> bool:
        """True for ``.epub`` files (case-insensitive)."""
        return path.suffix.lower() == ".epub"

    def parse(self, path: Path) -> ParsedBook:
        # Lazy imports keep module load cheap:
        #   import ebooklib
        #   from ebooklib import epub
        #   from bs4 import BeautifulSoup
        raise NotImplementedError("EpubParser.parse is not yet implemented")

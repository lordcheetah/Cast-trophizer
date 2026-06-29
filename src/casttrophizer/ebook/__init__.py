"""Ebook parsing package: format-neutral interface plus concrete parsers (EPUB first).

``parser_for`` keeps format selection behind the :class:`EbookParser` interface so the
pipeline can support more formats later without touching stage code.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.ebook.base import EbookParser, ParsedBook, ParsedChapter
from casttrophizer.ebook.epub import EpubParser
from casttrophizer.errors import EbookParseError

__all__ = ["EbookParser", "ParsedBook", "ParsedChapter", "EpubParser", "parser_for"]

#: Registered parsers, tried in order. New formats append their parser here.
_PARSERS: tuple[EbookParser, ...] = (EpubParser(),)


def parser_for(path: Path) -> EbookParser:
    """Return the first registered :class:`EbookParser` whose ``supports(path)`` is True.

    Raises :class:`~casttrophizer.errors.EbookParseError` if no parser handles the file.
    """
    for parser in _PARSERS:
        if parser.supports(path):
            return parser
    raise EbookParseError(f"no parser supports {path.suffix!r} files: {path}")

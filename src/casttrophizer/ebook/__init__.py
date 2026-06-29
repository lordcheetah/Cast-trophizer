"""Ebook parsing package: format-neutral interface plus concrete parsers (EPUB first)."""

from __future__ import annotations

from casttrophizer.ebook.base import EbookParser, ParsedBook, ParsedChapter
from casttrophizer.ebook.epub import EpubParser

__all__ = ["EbookParser", "ParsedBook", "ParsedChapter", "EpubParser"]

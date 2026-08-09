"""Ebook parsing behind an interface.

``EbookParser`` is the ABC every format parser implements; ``ParsedBook`` is the
format-neutral DTO it returns (chapters of plain-text lines plus metadata). The parser
treats the source file as read-only and never writes into a workspace.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ParsedChapter", "ParsedBook", "EbookParser"]


@dataclass
class ParsedChapter:
    """One parsed chapter: an ordered title plus its plain-text lines/paragraphs."""

    order: int
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class ParsedBook:
    """Format-neutral result of parsing an ebook.

    ``cover_image_path`` points at an extracted/located cover when one exists; the
    pipeline decides whether and where to persist it.
    """

    title: str
    author: str
    source_path: Path
    cover_image_path: Path | None = None
    chapters: list[ParsedChapter] = field(default_factory=list)


class EbookParser(ABC):
    """Abstract parser for a single ebook format."""

    @abstractmethod
    def supports(self, path: Path) -> bool:
        """True if this parser can handle the file at ``path`` (e.g. by extension)."""

    @abstractmethod
    def parse(self, path: Path) -> ParsedBook:
        """Parse the ebook at ``path`` into a :class:`ParsedBook` (read-only input)."""

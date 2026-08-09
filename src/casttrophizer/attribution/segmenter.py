"""Deterministic, offline line segmentation for speaker attribution.

A :class:`Segmenter` splits one ``Line.text`` into ordered narration/quote spans. The
segment is the per-segment audio cache-key unit downstream, so the split MUST be stable
and reproducible: given identical ``line_text``, :meth:`QuoteSegmenter.split` returns
identical spans (same text, same order). This module is pure, deterministic, Qt-free, and
imports no SDK.

v1 ruleset (CONFIRMED): straight ``"`` and curly ``“…”`` **double** quotes are the only
quote delimiters. Narration between/around quotes (including dialogue tags like
``said Alice``) stays narration. A line with no recognizable quote, or with unbalanced
quotes, falls back to a single narration span == the whole (trimmed) line. British single
quotes, em-dash dialogue, and multi-paragraph/cross-line quotes are DEFERRED — they degrade
to whole-line narration rather than guessing a boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["SegmentSpan", "Segmenter", "QuoteSegmenter"]

#: Span kinds. A quote span includes its delimiters so it reads naturally when rendered.
KIND_NARRATION = "narration"
KIND_QUOTE = "quote"

#: Straight double quote toggles open/close; curly open/close are matched as a pair.
_STRAIGHT = '"'
_CURLY_OPEN = "“"
_CURLY_CLOSE = "”"


@dataclass(frozen=True)
class SegmentSpan:
    """One ordered span of a line: trimmed, non-empty text plus its kind.

    ``kind`` is ``"narration"`` or ``"quote"``. A quote span's ``text`` includes its
    surrounding double-quote delimiters.
    """

    text: str
    kind: str


class Segmenter(Protocol):
    """Splits one line into ordered narration/quote spans. Pure, deterministic, offline."""

    def split(self, line_text: str) -> list[SegmentSpan]:
        """Return the ordered spans for ``line_text``.

        A line with no recognizable quote returns a single narration span equal to the
        whole (trimmed) line; an empty/whitespace-only line returns ``[]``.
        """
        ...


class QuoteSegmenter:
    """Deterministic double-quote segmenter (straight + curly), per the v1 ruleset."""

    def split(self, line_text: str) -> list[SegmentSpan]:
        """Split ``line_text`` into ordered narration/quote spans (see module docstring)."""
        if not line_text.strip():
            return []

        spans = self._split_balanced(line_text)
        if spans is None:
            # Unbalanced / unsupported -> safe degenerate: whole line is narration.
            return [SegmentSpan(text=line_text.strip(), kind=KIND_NARRATION)]
        return spans

    @staticmethod
    def _split_balanced(text: str) -> list[SegmentSpan] | None:
        """Scan ``text`` into alternating narration/quote spans, or ``None`` if unbalanced.

        A quote opens on a straight ``"`` or a curly open ``“`` and closes on the matching
        delimiter (straight ``"`` toggles; curly ``“`` must close with ``”``). An unclosed
        quote, or a stray curly close with no open, makes the line unsplittable -> ``None``.
        """
        spans: list[SegmentSpan] = []
        buf: list[str] = []  # current narration run
        i = 0
        n = len(text)

        def flush_narration() -> None:
            chunk = "".join(buf).strip()
            if chunk:
                spans.append(SegmentSpan(text=chunk, kind=KIND_NARRATION))
            buf.clear()

        while i < n:
            ch = text[i]
            if ch == _STRAIGHT or ch == _CURLY_OPEN:
                close = _STRAIGHT if ch == _STRAIGHT else _CURLY_CLOSE
                end = text.find(close, i + 1)
                if end == -1:
                    return None  # unbalanced: no closing delimiter on this line
                flush_narration()
                quote = text[i : end + 1].strip()
                if quote:
                    spans.append(SegmentSpan(text=quote, kind=KIND_QUOTE))
                i = end + 1
                continue
            if ch == _CURLY_CLOSE:
                return None  # stray curly close with no matching open
            buf.append(ch)
            i += 1

        flush_narration()
        return spans

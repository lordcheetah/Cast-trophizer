"""Offline, deterministic text-correction doubles (no real dictionary).

``FakeSpellchecker`` is a tiny known-words set plus a fixed correction map, used to drive
:class:`~casttrophizer.text.spelling.SpellcheckCorrector` (and the OCR heuristics'
``is_word`` gate) without loading the real bundled dictionary — exactly the way parse tests
inject a ``CountingParser``. ``CountingCorrector`` wraps another corrector and counts
``line_fixes`` calls, proving the stage skips already-corrected lines on re-run.
``RaisingCorrector`` raises on ``line_fixes`` to exercise the stage's FAILED path.
"""

from __future__ import annotations

from casttrophizer.text.base import Corrector, FixCandidate
from casttrophizer.text.spelling import SpellcheckCorrector


class FakeSpellchecker:
    """A fixed known-words set + correction map standing in for the real dictionary."""

    def __init__(
        self,
        known: set[str] | None = None,
        corrections: dict[str, str] | None = None,
    ) -> None:
        self._known = {w.lower() for w in (known or set())}
        self._corrections = {k.lower(): v for k, v in (corrections or {}).items()}

    def is_word(self, token: str) -> bool:
        return token.lower() in self._known

    def correct(self, token: str) -> str | None:
        return self._corrections.get(token.lower())

    def as_corrector(self) -> SpellcheckCorrector:
        """Wrap this fake in the real :class:`SpellcheckCorrector` (tests the real policy)."""
        return SpellcheckCorrector(is_word=self.is_word, correct=self.correct)


class CountingCorrector:
    """Wraps a corrector and counts ``line_fixes`` calls (proves idempotent skip)."""

    def __init__(self, inner: Corrector) -> None:
        self._inner = inner
        self.calls = 0

    def line_fixes(self, text: str, *, protected: frozenset[str]) -> list[FixCandidate]:
        self.calls += 1
        return self._inner.line_fixes(text, protected=protected)


class RaisingCorrector:
    """A corrector whose ``line_fixes`` always raises (exercises the FAILED path)."""

    def line_fixes(self, text: str, *, protected: frozenset[str]) -> list[FixCandidate]:
        raise RuntimeError("corrector boom")

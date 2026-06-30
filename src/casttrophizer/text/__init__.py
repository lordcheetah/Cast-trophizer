"""Offline, deterministic text-correction package.

Exposes the corrector interface (:class:`Corrector`, :class:`FixCandidate`), the
auto-vs-surface policy (:func:`apply_fixes`), proper-noun protection
(:func:`build_protected_set`), and a :func:`default_correctors` factory the stage uses when
no correctors are injected. The real dictionary load stays lazy (inside
:mod:`casttrophizer.text.spelling`), so importing this package is cheap and Qt-/SDK-free.
"""

from __future__ import annotations

from casttrophizer.text.base import (
    CORRECTION_REASONS,
    Corrector,
    FixCandidate,
    apply_fixes,
    build_protected_set,
    is_correction_suggestion,
    tokenize,
)
from casttrophizer.text.ocr import OcrHeuristicCorrector
from casttrophizer.text.spelling import SpellcheckCorrector, build_pyspellchecker

__all__ = [
    "Corrector",
    "FixCandidate",
    "apply_fixes",
    "build_protected_set",
    "is_correction_suggestion",
    "tokenize",
    "CORRECTION_REASONS",
    "OcrHeuristicCorrector",
    "SpellcheckCorrector",
    "build_pyspellchecker",
    "default_correctors",
]


def default_correctors(protected: frozenset[str] = frozenset()) -> list[Corrector]:
    """Build the production correctors, sharing one (lazily loaded) dictionary.

    The spellchecker is seeded with ``protected`` so protected tokens are not flagged, and
    its membership test is reused as the OCR heuristics' ``is_word`` gate so both correctors
    agree on what a real word is. The heavy dictionary is loaded here (once per run), not at
    import time.
    """
    spell = build_pyspellchecker(known_words=protected)
    ocr = OcrHeuristicCorrector(is_word=spell.is_word)
    return [ocr, spell]

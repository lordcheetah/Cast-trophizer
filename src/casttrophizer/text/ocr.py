"""OCR-artifact heuristics: typographic auto-fixes plus surfaced character-confusion fixes.

Two classes of candidate, both with ``reason="ocr-artifact"``:

* **Auto (whitespace/typography only):** collapse runs of spaces/tabs and remove
  space-before-punctuation. These are meaning-preserving and reversible, so they carry
  ``auto=True`` and the stage applies them to ``Line.text`` (§5). Idempotent by
  construction — running them on already-normalized text yields no candidate.
* **Surfaced (``auto=False``):** de-hyphenation across a line break and single-substitution
  character-confusion fixes (``rn``↔``m``, ``l``↔``1``, ``0``↔``O``, ``cl``↔``d``). These are
  lexical guesses gated on "the result is a real word and the input is not", so they need an
  injected ``is_word`` and are only ever *surfaced* (PENDING), never auto-applied (§4a).

The ``is_word`` callable is injected so unit tests need no real dictionary; in production the
stage passes the spellchecker's membership test. When ``is_word`` is ``None`` the
dictionary-gated heuristics are disabled (only the whitespace auto-fixes run).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from casttrophizer.text.base import FixCandidate

__all__ = ["OcrHeuristicCorrector"]

_REASON = "ocr-artifact"

#: Runs of two-or-more horizontal whitespace chars collapse to a single space.
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
#: A space (or run of spaces) sitting before sentence punctuation is removed.
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,.;:!?])")
#: A word split across a line break by a hyphen: ``care-\nfully`` / ``care- fully``.
#:
#: In production ``EpubParser`` collapses all whitespace (including the original newline) to
#: single spaces, so ``Line.text`` never contains a literal ``\n``. The break separator must
#: therefore match either a newline (with optional surrounding spaces/tabs) OR one-or-more
#: plain spaces — otherwise this heuristic would be dead on real parsed input. The join is
#: still hard-gated by ``is_word`` and the not-an-open-compound check in
#: :meth:`_dehyphenation_fixes`, and is always surfaced (``auto=False``), never auto-applied.
_HYPHEN_BREAK_RE = re.compile(r"([^\W\d_]+)-(?:[ \t]*\n[ \t]*|[ \t]+)([^\W\d_]+)", re.UNICODE)
#: Word-ish tokens for the per-token confusion pass.
_TOKEN_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*", re.UNICODE)
#: True if a line contains at least one word character; gates the whitespace auto-fix so
#: punctuation-/whitespace-only lines (scene breaks, spaced ellipses) are left untouched.
_HAS_WORD_RE = re.compile(r"\w", re.UNICODE)

#: Single-substitution OCR confusions, applied one at a time (§4a). Each maps a substring
#: that OCR commonly mis-reads to its likely intended form. Symmetric pairs are listed both
#: ways so either direction can be proposed when it yields a real word.
_CONFUSIONS: tuple[tuple[str, str], ...] = (
    ("rn", "m"),
    ("m", "rn"),
    ("cl", "d"),
    ("d", "cl"),
    ("l", "i"),
    ("i", "l"),
)

#: The ``l``/``i`` confusion fires on many short tokens and inflates PENDING volume, so it is
#: gated to longer tokens; the other pairs apply at any length.
_LI_CONFUSIONS: frozenset[tuple[str, str]] = frozenset({("l", "i"), ("i", "l")})
#: Minimum token length for the ``l``/``i`` confusion pair to be considered.
_LI_MIN_TOKEN_LEN = 4


class OcrHeuristicCorrector:
    """Yields whitespace auto-fixes and surfaced OCR-confusion / de-hyphenation candidates."""

    def __init__(self, is_word: Callable[[str], bool] | None = None) -> None:
        """``is_word`` gates the lexical heuristics; ``None`` disables them (whitespace only)."""
        self._is_word = is_word

    def line_fixes(self, text: str, *, protected: frozenset[str]) -> list[FixCandidate]:
        """Return whitespace auto-fixes plus surfaced de-hyphenation/confusion candidates."""
        candidates: list[FixCandidate] = []

        whitespace_fixed = self._whitespace_fix(text)
        if whitespace_fixed is not None and whitespace_fixed != text:
            candidates.append(
                FixCandidate(
                    original=text,
                    suggested=whitespace_fixed,
                    reason=_REASON,
                    confidence=0.99,
                    auto=True,
                )
            )

        if self._is_word is None:
            return candidates

        candidates.extend(self._dehyphenation_fixes(text, protected))
        candidates.extend(self._confusion_fixes(text, protected))
        return candidates

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _whitespace_fix(text: str) -> str | None:
        """Collapse multi-space runs and remove space-before-punctuation; None if no change.

        Punctuation-/whitespace-only lines (no word characters) are exempt: a spaced
        scene-break or ellipsis line (``"!!! ??? ..."``) is intentional layout, not a typo,
        so collapsing it would silently alter the author's text. Such lines yield no fix and
        no suggestion. Normal prose lines (which contain word characters) still get their
        multi-space and space-before-punctuation fixes.
        """
        if not _HAS_WORD_RE.search(text):
            return None
        fixed = _MULTISPACE_RE.sub(" ", text)
        fixed = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", fixed)
        return fixed if fixed != text else None

    def _dehyphenation_fixes(self, text: str, protected: frozenset[str]) -> list[FixCandidate]:
        """Surface a join of a hyphen-broken word when the joined form is a real word."""
        is_word = self._is_word
        assert is_word is not None  # guarded by caller
        out: list[FixCandidate] = []
        for match in _HYPHEN_BREAK_RE.finditer(text):
            left, right = match.group(1), match.group(2)
            joined = left + right
            if joined.casefold() in protected:
                continue
            # Only propose when the join is a real word and the pieces aren't both words
            # (i.e. not a legitimate open compound split across the break).
            if is_word(joined) and not (is_word(left) and is_word(right)):
                out.append(
                    FixCandidate(
                        original=match.group(0),
                        suggested=joined,
                        reason=_REASON,
                        confidence=0.5,
                        auto=False,
                    )
                )
        return out

    def _confusion_fixes(self, text: str, protected: frozenset[str]) -> list[FixCandidate]:
        """Surface a single-substitution confusion fix that turns a non-word into a word."""
        is_word = self._is_word
        assert is_word is not None  # guarded by caller
        out: list[FixCandidate] = []
        seen: set[str] = set()
        for match in _TOKEN_RE.finditer(text):
            token = match.group(0)
            if len(token) < 3:
                continue
            folded = token.casefold()
            if folded in seen:  # case-insensitive dedup, matching the protection check
                continue
            seen.add(folded)
            if folded in protected:
                continue
            if is_word(token):
                continue  # already a real word — don't second-guess it
            fixed = self._first_word_substitution(token, is_word, protected)
            if fixed is not None:
                out.append(
                    FixCandidate(
                        original=token,
                        suggested=fixed,
                        reason=_REASON,
                        confidence=0.4,
                        auto=False,
                    )
                )
        return out

    @staticmethod
    def _first_word_substitution(
        token: str,
        is_word: Callable[[str], bool],
        protected: frozenset[str],
    ) -> str | None:
        """First single-substitution (at any position) of a confusion pair yielding a word."""
        lowered = token.lower()
        for src, dst in _CONFUSIONS:
            if (src, dst) in _LI_CONFUSIONS and len(token) < _LI_MIN_TOKEN_LEN:
                continue  # l/i confusion is noisy on short tokens (§3c)
            start = lowered.find(src)
            while start >= 0:
                fixed = token[:start] + dst + token[start + len(src) :]
                if fixed != token and fixed.casefold() not in protected and is_word(fixed):
                    return fixed
                start = lowered.find(src, start + 1)
        return None

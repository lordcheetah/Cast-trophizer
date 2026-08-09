"""Spellcheck corrector backed by ``pyspellchecker`` (lazy import, always surfaced).

For each word-ish token not protected (§7b), an unknown token yields a single ``auto=False``
candidate proposing the dictionary's top correction. Spellcheck NEVER auto-applies — names,
dialect, and jargon are exactly the words a dictionary mis-flags, so the worst case is a
PENDING suggestion the user rejects (§5).

The heavy dictionary load is deferred behind two seams so CI never loads it:

* The ``pyspellchecker`` import is **lazy** (inside the corrector, not at module load), like
  ``ebooklib`` in the parse stage — importing this module stays cheap.
* The membership/correction logic goes through an injected ``is_word`` / ``correct``; tests
  pass a tiny fake, never the real dictionary.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from casttrophizer.text.base import FixCandidate

__all__ = ["SpellcheckCorrector", "build_pyspellchecker"]

_REASON = "spellcheck"
#: Tokens shorter than this are skipped (initials/abbreviations are not typos to chase).
_MIN_TOKEN_LEN = 3
#: Whitespace-delimited tokens with surrounding punctuation stripped (digits kept so
#: alphanumerics like ``Room101`` stay whole and are skipped by the digit rule).
_RAW_TOKEN_RE = re.compile(r"\S+")
_EDGE_PUNCT_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)


def _spell_tokens(text: str) -> list[str]:
    """Split ``text`` on whitespace and strip leading/trailing punctuation per token."""
    tokens: list[str] = []
    for raw in _RAW_TOKEN_RE.findall(text):
        token = _EDGE_PUNCT_RE.sub("", raw)
        if token:
            tokens.append(token)
    return tokens


class SpellcheckCorrector:
    """Surfaces dictionary-unknown tokens as PENDING (``auto=False``) candidates.

    Wraps two injected callables so the heavy dictionary stays out of unit tests:
    ``is_word(token) -> bool`` (membership) and ``correct(token) -> str | None`` (top
    suggestion, or ``None`` when the checker has none). Protection, casing, and
    skip-rules are enforced here so every corrector shares one tokenization policy.
    """

    def __init__(
        self,
        is_word: Callable[[str], bool],
        correct: Callable[[str], str | None],
    ) -> None:
        self._is_word = is_word
        self._correct = correct

    @property
    def is_word(self) -> Callable[[str], bool]:
        """The membership test, reused as the OCR heuristics' ``is_word`` gate."""
        return self._is_word

    def line_fixes(self, text: str, *, protected: frozenset[str]) -> list[FixCandidate]:
        """Return one PENDING candidate per unknown, non-protected, spell-checkable token."""
        out: list[FixCandidate] = []
        seen: set[str] = set()
        for token in _spell_tokens(text):
            if not self._checkable(token, protected):
                continue
            folded = token.casefold()
            if folded in seen:  # case-insensitive dedup, matching the protection check
                continue
            seen.add(folded)
            if self._is_word(token):
                continue
            suggestion = self._correct(token)
            if not suggestion or suggestion == token:
                continue
            out.append(
                FixCandidate(
                    original=token,
                    suggested=suggestion,
                    reason=_REASON,
                    confidence=0.7,
                    auto=False,
                )
            )
        return out

    @staticmethod
    def _checkable(token: str, protected: frozenset[str]) -> bool:
        """False for tokens we never spellcheck: protected, all-caps, digit-bearing, short."""
        if len(token) < _MIN_TOKEN_LEN:
            return False
        if token.casefold() in protected:
            return False
        if any(ch.isdigit() for ch in token):
            return False
        if token.isupper():  # acronyms / initialisms (NASA, OK)
            return False
        return True


def build_pyspellchecker(
    known_words: frozenset[str] = frozenset(),
) -> SpellcheckCorrector:
    """Build a :class:`SpellcheckCorrector` over the real bundled English dictionary.

    The ``pyspellchecker`` import is lazy (kept out of module load and unit tests). The
    auto-seeded protection set (§7b) is loaded into the checker's known words so protected
    tokens are not even flagged. Used only when no corrector is injected — tests inject a
    fake and never reach this path.
    """
    from spellchecker import SpellChecker  # lazy: heavy dictionary load

    checker = SpellChecker()
    if known_words:
        checker.word_frequency.load_words(known_words)

    def is_word(token: str) -> bool:
        return token.lower() not in checker.unknown([token])

    def correct(token: str) -> str | None:
        return checker.correction(token)

    return SpellcheckCorrector(is_word=is_word, correct=correct)

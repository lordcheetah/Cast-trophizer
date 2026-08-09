"""Corrector interface, fix-candidate DTO, the auto-vs-surface policy, and protection.

This module is the seam every concrete corrector plugs into. It encodes two contracts:

* **Reversibility / disposition policy** (``apply_fixes``): an ``auto=True`` candidate is a
  *meaning-preserving typographic* fix — it mutates ``Line.text`` and is recorded as an
  ``AUTO_APPLIED`` :class:`~casttrophizer.domain.models.TextSuggestion` whose ``original``
  recovers the pre-fix text. Everything else (``auto=False``: spellcheck and all OCR
  character-confusion / de-hyphenation guesses) is **surfaced** as a ``PENDING`` suggestion
  and leaves ``Line.text`` unchanged. Nothing lexical is ever auto-applied — the review
  stage/UI decides.
* **Proper-noun protection** (``build_protected_set``): a per-project, case-folded allowlist
  auto-seeded from high-frequency capitalized tokens and the project's speaker names, so
  character names and coined words never generate suggestions.

Pure, deterministic, offline, Qt-free. No dictionary is imported here; dictionary-gated
correctors receive an injected ``is_word`` callable (so unit tests need no real dictionary).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Line, TextSuggestion

if TYPE_CHECKING:
    from casttrophizer.domain.models import Project

__all__ = [
    "FixCandidate",
    "Corrector",
    "apply_fixes",
    "build_protected_set",
    "is_correction_suggestion",
    "CORRECTION_REASONS",
    "tokenize",
]

#: ``reason`` strings this stage emits; used as the idempotency key (§7a).
CORRECTION_REASONS: frozenset[str] = frozenset({"ocr-artifact", "spellcheck"})

#: A "word-ish" token: letters/apostrophes/hyphens, used for tokenizing & seeding.
_WORD_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)

#: Minimum capitalized-token frequency to treat a token as a proper noun (§7b).
_PROPER_NOUN_MIN_COUNT = 2


@dataclass(frozen=True)
class FixCandidate:
    """One proposed correction yielded by a :class:`Corrector`.

    ``auto`` is the disposition gate: ``True`` means the fix is a meaning-preserving
    typographic transform eligible for auto-apply (whitespace/typography only, in v1);
    ``False`` means surface it as a ``PENDING`` suggestion without touching ``Line.text``.
    """

    original: str  # the token or whole-line text being changed
    suggested: str  # the proposed replacement
    reason: str  # one of CORRECTION_REASONS
    confidence: float
    auto: bool


class Corrector(Protocol):
    """A pure, deterministic, offline source of candidate fixes for one line."""

    def line_fixes(self, text: str, *, protected: frozenset[str]) -> list[FixCandidate]:
        """Return candidate fixes for one line's ``text`` (no mutation, no I/O).

        ``protected`` is the case-folded allowlist (§7b); a corrector must not propose a
        change to a protected token.
        """
        ...


def tokenize(text: str) -> list[str]:
    """Split ``text`` into word-ish tokens (letters, internal apostrophes/hyphens)."""
    return _WORD_RE.findall(text)


def is_correction_suggestion(suggestion: TextSuggestion) -> bool:
    """True if ``suggestion`` was produced by this correction stage (any status).

    The idempotency key (§7a): a line already carrying one of these has been processed and
    is skipped on re-run, so corrections are never double-applied or re-surfaced.
    """
    return suggestion.reason in CORRECTION_REASONS


def apply_fixes(line: Line, candidates: list[FixCandidate]) -> None:
    """Apply the disposition policy for one line's ``candidates`` in place.

    ``auto=True`` candidates are applied left-to-right to ``line.text`` (each recorded as an
    ``AUTO_APPLIED`` suggestion whose ``original`` is the whole pre-fix line text, so undo is
    a clean text swap). ``auto=False`` candidates leave ``line.text`` untouched and are
    recorded as ``PENDING`` suggestions. Order is preserved; the function is a no-op when
    ``candidates`` is empty.
    """
    for candidate in candidates:
        if candidate.auto:
            pre_fix = line.text
            # Whole-line-auto contract: an auto candidate's ``original`` is a snapshot of the
            # entire current line text, and ``suggested`` is the whole replacement line. We
            # therefore assign ``suggested`` directly rather than ``str.replace`` (which is
            # replace-all and would over-replace or silently drop on a substring ``original``).
            # Assert the contract so a future corrector emitting a substring-level auto
            # candidate fails loudly here instead of corrupting the line.
            assert candidate.original == pre_fix, (
                "auto FixCandidate.original must be the whole current line text "
                f"(got {candidate.original!r}, line is {pre_fix!r})"
            )
            line.text = candidate.suggested
            line.suggestions.append(
                TextSuggestion(
                    id=new_id("sug"),
                    original=pre_fix,
                    suggested=line.text,
                    reason=candidate.reason,
                    confidence=candidate.confidence,
                    status=ReviewStatus.AUTO_APPLIED,
                )
            )
        else:
            line.suggestions.append(
                TextSuggestion(
                    id=new_id("sug"),
                    original=candidate.original,
                    suggested=candidate.suggested,
                    reason=candidate.reason,
                    confidence=candidate.confidence,
                    status=ReviewStatus.PENDING,
                )
            )


def build_protected_set(project: Project) -> frozenset[str]:
    """Auto-seed the case-folded allowlist of do-not-correct tokens for ``project`` (§7b).

    Seeds from two sources: (a) the project's speaker names — character names are exactly
    what must never be "corrected" — and (b) capitalized tokens that recur in the book text
    (appearing capitalized at least :data:`_PROPER_NOUN_MIN_COUNT` times), the proper-noun
    heuristic. Tokens are case-folded so protection is case-insensitive.
    """
    protected: set[str] = set()

    for speaker in project.speakers:
        for token in tokenize(speaker.name):
            protected.add(token.casefold())

    capitalized_counts: Counter[str] = Counter()
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for token in tokenize(line.text):
                if token[:1].isupper():
                    capitalized_counts[token.casefold()] += 1

    for token, count in capitalized_counts.items():
        if count >= _PROPER_NOUN_MIN_COUNT:
            protected.add(token)

    return frozenset(protected)

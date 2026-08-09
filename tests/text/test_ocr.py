"""Unit tests for :class:`OcrHeuristicCorrector` — whitespace auto + surfaced lexical fixes.

The dictionary-gated heuristics (de-hyphenation, character-confusion) are driven by an
injected ``is_word`` so no real dictionary is loaded. Whitespace fixes need no dictionary and
are tested with ``is_word=None`` to prove the lexical heuristics stay disabled there.
"""

from __future__ import annotations

from casttrophizer.text.ocr import OcrHeuristicCorrector

_EMPTY: frozenset[str] = frozenset()


def _is_word(words: set[str]):
    lowered = {w.lower() for w in words}
    return lambda token: token.lower() in lowered


# --------------------------------------------------------------------------- #
# whitespace / typography (auto)
# --------------------------------------------------------------------------- #
def test_double_space_collapse_is_auto() -> None:
    fixes = OcrHeuristicCorrector().line_fixes("a  b", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].auto is True
    assert fixes[0].suggested == "a b"
    assert fixes[0].reason == "ocr-artifact"


def test_space_before_punctuation_collapse_is_auto() -> None:
    fixes = OcrHeuristicCorrector().line_fixes("word .", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].auto is True
    assert fixes[0].suggested == "word."


def test_whitespace_fix_is_idempotent_on_clean_text() -> None:
    assert OcrHeuristicCorrector().line_fixes("a b word.", protected=_EMPTY) == []


def test_no_isword_disables_lexical_heuristics() -> None:
    # With is_word=None only whitespace runs; the broken word yields no confusion candidate.
    fixes = OcrHeuristicCorrector(is_word=None).line_fixes("modem  art", protected=_EMPTY)
    assert all(f.auto for f in fixes)  # only the whitespace auto-fix, if any
    assert all(f.suggested != "modern art" for f in fixes)


# --------------------------------------------------------------------------- #
# de-hyphenation (surfaced)
# --------------------------------------------------------------------------- #
def test_dehyphenation_surfaces_when_joined_is_a_word() -> None:
    corr = OcrHeuristicCorrector(is_word=_is_word({"carefully"}))
    fixes = corr.line_fixes("care-\nfully chosen", protected=_EMPTY)
    join = [f for f in fixes if f.suggested == "carefully"]
    assert len(join) == 1
    assert join[0].auto is False


def test_dehyphenation_surfaces_on_space_joined_break() -> None:
    # Production input: EpubParser collapses the original newline to a single space, so the
    # break arrives as "care- fully" (hyphen + space), NOT "care-\nfully". The heuristic must
    # still fire on this space-joined form, else it is dead on real parsed text.
    corr = OcrHeuristicCorrector(is_word=_is_word({"carefully"}))
    fixes = corr.line_fixes("care- fully chosen", protected=_EMPTY)
    join = [f for f in fixes if f.suggested == "carefully"]
    assert len(join) == 1
    assert join[0].auto is False  # still surfaced, never auto-applied


def test_dehyphenation_space_joined_skipped_when_join_not_a_word() -> None:
    # A hyphen+space that does NOT join into a dictionary word (e.g. a real em-dash-style
    # break between two distinct words) must stay untouched — no spurious join proposed.
    corr = OcrHeuristicCorrector(is_word=_is_word({"the", "wind"}))  # "thewind" is NOT a word
    fixes = corr.line_fixes("the- wind howled", protected=_EMPTY)
    assert [f for f in fixes if f.suggested == "thewind"] == []


def test_dehyphenation_skipped_when_joined_not_a_word() -> None:
    corr = OcrHeuristicCorrector(is_word=_is_word(set()))  # nothing is a word
    fixes = corr.line_fixes("care-\nfully chosen", protected=_EMPTY)
    assert [f for f in fixes if f.suggested == "carefully"] == []


def test_dehyphenation_skipped_for_open_compound() -> None:
    # Both pieces are real words -> likely a legitimate compound; don't propose a join.
    corr = OcrHeuristicCorrector(is_word=_is_word({"care", "fully", "carefully"}))
    fixes = corr.line_fixes("care-\nfully chosen", protected=_EMPTY)
    assert [f for f in fixes if f.suggested == "carefully"] == []


# --------------------------------------------------------------------------- #
# character confusion (surfaced)
# --------------------------------------------------------------------------- #
def test_confusion_surfaces_when_one_sub_yields_a_word() -> None:
    # "modem" is itself NOT a word here, "modern" is -> rn<->m surfaces.
    corr = OcrHeuristicCorrector(is_word=_is_word({"modern"}))
    fixes = corr.line_fixes("modem art", protected=_EMPTY)
    confusions = [f for f in fixes if f.suggested == "modern"]
    assert len(confusions) == 1
    assert confusions[0].auto is False


def test_confusion_not_proposed_when_token_already_a_word() -> None:
    # "modem" IS a real word -> never second-guess it.
    corr = OcrHeuristicCorrector(is_word=_is_word({"modem", "modern"}))
    fixes = corr.line_fixes("modem art", protected=_EMPTY)
    assert [f for f in fixes if f.suggested == "modern"] == []


def test_confusion_protected_token_yields_nothing() -> None:
    corr = OcrHeuristicCorrector(is_word=_is_word({"modern"}))
    fixes = corr.line_fixes("modem art", protected=frozenset({"modem"}))
    assert [f for f in fixes if f.suggested == "modern"] == []


def test_dehyphenation_idempotent_on_already_joined_word() -> None:
    # Once a word is joined there is no hyphen-break for the heuristic to fire on again.
    corr = OcrHeuristicCorrector(is_word=_is_word({"carefully"}))
    assert corr.line_fixes("carefully chosen", protected=_EMPTY) == []


def test_li_confusion_gated_off_for_short_tokens() -> None:
    # The l/i pair is noisy on short tokens, so it is gated to len >= 4. "fil" (len 3) with
    # l->i at index 2 would yield the word "fii", but the length gate suppresses it.
    corr = OcrHeuristicCorrector(is_word=_is_word({"fii"}))
    fixes = corr.line_fixes("fil here", protected=_EMPTY)
    assert [f for f in fixes if f.suggested == "fii"] == []


def test_li_confusion_fires_for_long_enough_tokens() -> None:
    # A len-4 token IS eligible for the l/i substitution: "falr" -> "fair" (l->i at index 2).
    corr = OcrHeuristicCorrector(is_word=_is_word({"fair"}))
    fixes = corr.line_fixes("falr enough", protected=_EMPTY)
    confusions = [f for f in fixes if f.suggested == "fair"]
    assert len(confusions) == 1
    assert confusions[0].auto is False


def test_other_confusion_pairs_still_fire_on_short_tokens() -> None:
    # The length gate is l/i-only; a short rn->m token still surfaces ("arn" is len 3).
    corr = OcrHeuristicCorrector(is_word=_is_word({"am"}))
    fixes = corr.line_fixes("arn ready", protected=_EMPTY)
    assert len([f for f in fixes if f.suggested == "am"]) == 1


def test_confusion_dedup_is_case_insensitive() -> None:
    # "Modem modem" must propose the fix once, not twice across case — dedup is case-folded
    # (only the first occurrence is processed; the substitution preserves the token's casing).
    corr = OcrHeuristicCorrector(is_word=_is_word({"modern"}))
    fixes = corr.line_fixes("Modem modem", protected=_EMPTY)
    assert len([f for f in fixes if f.suggested.casefold() == "modern"]) == 1


def test_confusion_first_substitution_only() -> None:
    # The corrector proposes the *first* single substitution that yields a word and stops —
    # it never auto-applies and never returns multiple guesses for one token.
    corr = OcrHeuristicCorrector(is_word=_is_word({"modern"}))
    fixes = corr.line_fixes("modem", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].auto is False


# --------------------------------------------------------------------------- #
# tokenization / robustness edge cases (no crashes, sensible behavior)
# --------------------------------------------------------------------------- #
def test_empty_and_whitespace_only_and_punct_lines_do_not_crash() -> None:
    corr = OcrHeuristicCorrector(is_word=_is_word(set()))
    assert corr.line_fixes("", protected=_EMPTY) == []
    # A whitespace-only line has no word characters, so it is exempt from the auto-fix
    # (it carries no meaning to "correct") — no candidate, no crash.
    assert corr.line_fixes("   ", protected=_EMPTY) == []


def test_punctuation_only_line_is_exempt_from_whitespace_autofix() -> None:
    # A line with NO word characters (a spaced scene-break / ellipsis run) is intentional
    # layout, not a typo: the whitespace/typographic auto-fix is suppressed entirely so the
    # author's spacing is never silently collapsed. No fix, no suggestion.
    corr = OcrHeuristicCorrector()
    assert corr.line_fixes("!!! ??? ...", protected=_EMPTY) == []


def test_prose_line_space_before_comma_still_auto_fixes() -> None:
    # A normal prose line (contains word characters) still gets its space-before-punctuation
    # and multi-space fixes — the exemption only covers punctuation-/whitespace-only lines.
    corr = OcrHeuristicCorrector()
    fixes = corr.line_fixes("He said , hello", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].auto is True
    assert fixes[0].suggested == "He said, hello"


def test_multi_space_collapse_still_works_on_prose() -> None:
    corr = OcrHeuristicCorrector()
    fixes = corr.line_fixes("the  cat  sat", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].auto is True
    assert fixes[0].suggested == "the cat sat"


def test_unicode_apostrophe_hyphen_allcaps_numbers_no_crash() -> None:
    # Accented words, contractions, hyphenated compounds, all-caps, and numerics must not
    # crash the OCR pass; with an empty dictionary none yield a (lexical) candidate.
    corr = OcrHeuristicCorrector(is_word=_is_word(set()))
    for text in ["café au lait", "don't can't", "well-known", "NASA OK", "Room101 3rd"]:
        assert corr.line_fixes(text, protected=_EMPTY) == []

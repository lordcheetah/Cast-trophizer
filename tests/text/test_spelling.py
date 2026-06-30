"""Unit tests for :class:`SpellcheckCorrector` driven by a fake dictionary.

Every test injects ``FakeSpellchecker`` (a tiny known-words set + correction map) — the real
``pyspellchecker`` dictionary is never loaded. Pins: unknown token surfaces one ``auto=False``
candidate; known/protected/all-caps/numeric/short tokens yield none.
"""

from __future__ import annotations

from tests.fakes import FakeSpellchecker

_EMPTY: frozenset[str] = frozenset()


def _corrector() -> object:
    fake = FakeSpellchecker(
        known={"the", "spoke", "modern", "art"},
        corrections={"narrarator": "narrator"},
    )
    return fake.as_corrector()


def test_unknown_token_surfaces_one_pending_candidate() -> None:
    fixes = _corrector().line_fixes("The narrarator spoke.", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].original == "narrarator"
    assert fixes[0].suggested == "narrator"
    assert fixes[0].reason == "spellcheck"
    assert fixes[0].auto is False


def test_known_tokens_yield_nothing() -> None:
    assert _corrector().line_fixes("The art spoke.", protected=_EMPTY) == []


def test_protected_token_yields_nothing() -> None:
    fixes = _corrector().line_fixes("The narrarator spoke.", protected=frozenset({"narrarator"}))
    assert fixes == []


def test_allcaps_and_short_and_numeric_tokens_skipped() -> None:
    fake = FakeSpellchecker(known=set(), corrections={"nasa": "x", "room": "y", "ok": "z"})
    corr = fake.as_corrector()
    # NASA (all caps), OK (short+caps), Room101 (digit) -> all skipped despite being unknown.
    assert corr.line_fixes("NASA OK Room101", protected=_EMPTY) == []


def test_no_correction_available_yields_nothing() -> None:
    # Unknown token but the checker offers no correction (returns None) -> no candidate.
    fake = FakeSpellchecker(known=set(), corrections={})
    assert fake.as_corrector().line_fixes("xyzzyq here", protected=_EMPTY) == []


def test_duplicate_unknown_token_proposed_once() -> None:
    fixes = _corrector().line_fixes("narrarator and narrarator", protected=_EMPTY)
    assert len(fixes) == 1


def test_duplicate_unknown_token_dedup_is_case_insensitive() -> None:
    # "Narrarator narrarator" must surface once, not twice across case — dedup is case-folded.
    fixes = _corrector().line_fixes("Narrarator narrarator", protected=_EMPTY)
    assert len(fixes) == 1


def test_hyphenated_compound_tokenized_as_one_unit() -> None:
    # "well-known" is a single token, not "well" + "known"; an unknown compound surfaces once.
    fake = FakeSpellchecker(known={"the"}, corrections={"well-knonw": "well-known"})
    fixes = fake.as_corrector().line_fixes("the well-knonw fact", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].original == "well-knonw"
    assert fixes[0].suggested == "well-known"


def test_internal_apostrophe_token_preserved() -> None:
    # A contraction/possessive keeps its internal apostrophe through tokenization (only edge
    # punctuation is stripped), so it is spell-checked whole, not split at the apostrophe.
    fake = FakeSpellchecker(known=set(), corrections={"o'brein": "O'Brien"})
    fixes = fake.as_corrector().line_fixes("said O'brein loudly", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].original == "O'brein"  # apostrophe retained, not tokenized away
    assert fixes[0].suggested == "O'Brien"


def test_empty_and_punct_only_lines_yield_no_tokens() -> None:
    corr = _corrector()
    assert corr.line_fixes("", protected=_EMPTY) == []
    assert corr.line_fixes("   ", protected=_EMPTY) == []
    assert corr.line_fixes("... !!! ???", protected=_EMPTY) == []


def test_mixed_case_unknown_token_surfaced() -> None:
    # A capitalized-but-not-all-caps unknown token is still spell-checkable (not skipped).
    fake = FakeSpellchecker(known=set(), corrections={"teh": "the"})
    fixes = fake.as_corrector().line_fixes("Teh end", protected=_EMPTY)
    assert len(fixes) == 1
    assert fixes[0].original == "Teh"

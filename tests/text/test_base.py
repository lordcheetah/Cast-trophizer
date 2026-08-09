"""Unit tests for the corrector seam: ``apply_fixes`` policy and ``build_protected_set``.

All offline and deterministic. ``apply_fixes`` is exercised directly on hand-built
``FixCandidate``s (no corrector, no dictionary) so the auto-vs-surface contract is pinned in
isolation; ``build_protected_set`` is checked against a small in-memory project.
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Speaker,
    TextSuggestion,
)
from casttrophizer.text.base import (
    FixCandidate,
    apply_fixes,
    build_protected_set,
    is_correction_suggestion,
    tokenize,
)


def _line(text: str) -> Line:
    return Line(id=new_id("line"), chapter_id="ch", order=0, text=text, segments=[])


def test_auto_candidate_mutates_text_and_records_auto_applied() -> None:
    line = _line("He  said")
    apply_fixes(line, [FixCandidate("He  said", "He said", "ocr-artifact", 0.99, auto=True)])

    assert line.text == "He said"  # text mutated
    assert len(line.suggestions) == 1
    sug = line.suggestions[0]
    assert sug.status == ReviewStatus.AUTO_APPLIED
    assert sug.original == "He  said"  # pre-fix text recoverable
    assert sug.suggested == "He said"


def test_auto_applied_original_recovers_pre_fix_text() -> None:
    line = _line("word ,  next")
    apply_fixes(line, [FixCandidate("word ,  next", "word, next", "ocr-artifact", 0.99, True)])
    assert line.text == "word, next"
    # The review UI can undo by restoring `original`.
    assert line.suggestions[0].original == "word ,  next"


def test_non_auto_candidate_leaves_text_and_records_pending() -> None:
    line = _line("The narrarator spoke.")
    apply_fixes(line, [FixCandidate("narrarator", "narrator", "spellcheck", 0.7, auto=False)])

    assert line.text == "The narrarator spoke."  # unchanged
    sug = line.suggestions[0]
    assert sug.status == ReviewStatus.PENDING
    assert sug.original == "narrarator"  # token granularity
    assert sug.suggested == "narrator"


def test_apply_fixes_empty_is_noop() -> None:
    line = _line("clean line")
    apply_fixes(line, [])
    assert line.text == "clean line"
    assert line.suggestions == []


def test_whole_line_auto_with_repeating_token_is_not_over_replaced() -> None:
    # The OcrHeuristicCorrector emits a *whole-line* ``original`` for whitespace fixes, so
    # ``str.replace(original, suggested)`` is a single full-string match even when a token
    # repeats. Pin that the result is the intended whole-line swap (not a corrupted multi-hit).
    line = _line("the  cat the  cat")
    apply_fixes(
        line,
        [FixCandidate("the  cat the  cat", "the cat the cat", "ocr-artifact", 0.99, auto=True)],
    )
    assert line.text == "the cat the cat"
    assert line.suggestions[0].original == "the  cat the  cat"  # recovery key is the whole line


def test_apply_fixes_combined_auto_and_pending_on_one_line() -> None:
    # A line carrying BOTH an auto whitespace fix and a surfaced spellcheck issue: text must
    # receive ONLY the whitespace fix while the misspelling stays a separate PENDING suggestion.
    line = _line("The  narrarator spoke.")  # double space (auto) + misspelling (pending)
    apply_fixes(
        line,
        [
            FixCandidate(
                "The  narrarator spoke.", "The narrarator spoke.", "ocr-artifact", 0.99, auto=True
            ),
            FixCandidate("narrarator", "narrator", "spellcheck", 0.7, auto=False),
        ],
    )
    # Only the whitespace collapse landed; "narrarator" is NOT silently corrected to "narrator".
    assert line.text == "The narrarator spoke."
    auto = [s for s in line.suggestions if s.status == ReviewStatus.AUTO_APPLIED]
    pending = [s for s in line.suggestions if s.status == ReviewStatus.PENDING]
    assert len(auto) == 1 and auto[0].reason == "ocr-artifact"
    assert len(pending) == 1
    assert pending[0].reason == "spellcheck"
    assert pending[0].original == "narrarator" and pending[0].suggested == "narrator"


def test_apply_fixes_pending_then_auto_order_preserved() -> None:
    # Disposition is per-candidate and order-preserving: a PENDING followed by an AUTO still
    # mutates text once (the auto) and records both, in the order given.
    line = _line("a  b unknwn")
    apply_fixes(
        line,
        [
            FixCandidate("unknwn", "unknown", "spellcheck", 0.7, auto=False),
            FixCandidate("a  b unknwn", "a b unknwn", "ocr-artifact", 0.99, auto=True),
        ],
    )
    assert line.text == "a b unknwn"
    assert [s.status for s in line.suggestions] == [
        ReviewStatus.PENDING,
        ReviewStatus.AUTO_APPLIED,
    ]


def test_is_correction_suggestion_recognizes_reasons() -> None:
    ocr = TextSuggestion(new_id("sug"), "a", "b", "ocr-artifact", 0.5, ReviewStatus.PENDING)
    spell = TextSuggestion(new_id("sug"), "a", "b", "spellcheck", 0.5, ReviewStatus.AUTO_APPLIED)
    other = TextSuggestion(new_id("sug"), "a", "b", "attribution", 0.5, ReviewStatus.PENDING)
    assert is_correction_suggestion(ocr) is True
    assert is_correction_suggestion(spell) is True
    assert is_correction_suggestion(other) is False


def test_tokenize_splits_words_keeps_internal_punctuation() -> None:
    assert tokenize("well-known don't, cat.") == ["well-known", "don't", "cat"]
    # ``tokenize`` (used for proper-noun seeding) keeps only alpha runs; digits split.
    assert tokenize("Room101 3rd") == ["Room", "rd"]


# --------------------------------------------------------------------------- #
# build_protected_set
# --------------------------------------------------------------------------- #
def _project(lines: list[str], speakers: list[Speaker]) -> Project:
    ch_id = "ch"
    line_objs = [
        Line(id=new_id("line"), chapter_id=ch_id, order=i, text=t, segments=[])
        for i, t in enumerate(lines)
    ]
    book = Book(
        title="t",
        author="a",
        source_ebook_path="src.epub",
        chapters=[Chapter(id=ch_id, order=0, title="C", lines=line_objs)],
    )
    return Project(
        schema_version=1,
        id=new_id("proj"),
        name="p",
        workspace_dir="ws",
        book=book,
        speakers=speakers,
        stage_status={str(StageName.PARSE): ReviewStatus.COMPLETED},
    )


def test_protected_set_seeds_recurring_capitalized_tokens() -> None:
    # "Aelin" capitalized twice -> protected; "blade" never capitalized -> not.
    project = _project(["Aelin drew her blade.", "Aelin smiled."], speakers=[])
    protected = build_protected_set(project)
    assert "aelin" in protected
    assert "blade" not in protected


def test_protected_set_includes_speaker_names() -> None:
    speaker = Speaker(id=new_id("spk"), name="Daenerys", role=SpeakerRole.CHARACTER)
    project = _project(["The dragon flew."], speakers=[speaker])
    protected = build_protected_set(project)
    assert "daenerys" in protected  # case-folded


def test_protected_set_skips_once_capitalized_token() -> None:
    # A single sentence-initial capitalization (count == 1) is not treated as a proper noun.
    project = _project(["Hello world.", "the cat sat."], speakers=[])
    protected = build_protected_set(project)
    assert "hello" not in protected


def test_protected_set_seeds_recurring_non_dictionary_token() -> None:
    # The heuristic is dictionary-free: a coined/invented capitalized token (not a real word)
    # that recurs >= the threshold is protected, so an invented name never gets "corrected".
    project = _project(["Zorblax went home.", "Zorblax laughed.", "A Glimphor passed once."], [])
    protected = build_protected_set(project)
    assert "zorblax" in protected  # recurs twice -> protected even though not a dictionary word
    assert "glimphor" not in protected  # one-off capitalized token -> still surfaceable


def test_protected_set_tokenizes_multiword_speaker_name() -> None:
    # A multi-word speaker name protects each component token, case-folded, even when the name
    # does not appear in the book text at all.
    speaker = Speaker(id=new_id("spk"), name="Aelin Galathynius", role=SpeakerRole.CHARACTER)
    project = _project(["nothing relevant here."], speakers=[speaker])
    protected = build_protected_set(project)
    assert "aelin" in protected
    assert "galathynius" in protected

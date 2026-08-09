"""Pure ``attribution.classify.classify_speakers`` orchestration — offline, in-memory.

Covers: CHARACTER-only targeting + narrator stays unknown; VoiceCategory.coerce mapping of
provider strings; dialogue-sample gathering (bounded, own quotes only); soft-fail on any
provider error; and the no-op when nothing is left to classify.
"""

from __future__ import annotations

from casttrophizer.attribution.classify import CLASSIFY_SAMPLE_LINES, classify_speakers
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, VoiceCategory
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Chapter, Line, Project, Segment, Speaker
from tests.fakes import FakeLLMProvider


def _project(*speakers: Speaker, lines: list[Line] | None = None) -> Project:
    ch = Chapter(id=new_id("ch"), order=0, title="One", lines=lines or [])
    return Project(
        schema_version=2,
        id=new_id("proj"),
        name="classify",
        workspace_dir="ws",
        book=Book(title="t", author="a", source_ebook_path="e.epub", chapters=[ch]),
        speakers=list(speakers),
    )


def _character(name: str) -> Speaker:
    return Speaker(id=new_id("spk"), name=name, role=SpeakerRole.CHARACTER)


def _quote(text: str, speaker: Speaker) -> Segment:
    return Segment(
        id=new_id("seg"),
        text=text,
        speaker_id=speaker.id,
        role=SpeakerRole.CHARACTER,
        confidence=0.9,
        review_status=ReviewStatus.APPROVED,
    )


def test_stamps_characters_and_leaves_narrator_unknown() -> None:
    narrator = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)
    alice = _character("Alice")
    project = _project(narrator, alice)
    llm = FakeLLMProvider(classify_script={"Alice": ("WOMAN", 0.9)})

    classify_speakers(project, llm)

    assert alice.category == VoiceCategory.WOMAN  # coerced from "WOMAN"
    assert narrator.category == VoiceCategory.UNKNOWN  # narrator never sent
    assert len(llm.classify_calls) == 1
    assert [p.name for p in llm.classify_calls[0]] == ["Alice"]


def test_unknown_provider_string_coerces_to_unknown() -> None:
    alice = _character("Alice")
    project = _project(alice)
    classify_speakers(project, FakeLLMProvider(classify_script={"Alice": ("android", 0.5)}))
    assert alice.category == VoiceCategory.UNKNOWN


def test_samples_are_bounded_and_own_quotes_only() -> None:
    alice = _character("Alice")
    bob = _character("Bob")
    ch_id = new_id("ch")
    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text="dialogue",
        segments=[
            _quote("a1", alice),
            _quote("b1", bob),
            _quote("a2", alice),
            _quote("a3", alice),
            _quote("a4", alice),  # 4th Alice quote -> dropped (cap is CLASSIFY_SAMPLE_LINES)
        ],
    )
    project = _project(alice, bob, lines=[line])
    llm = FakeLLMProvider(classify_script={"Alice": ("woman", 0.9), "Bob": ("man", 0.9)})

    classify_speakers(project, llm)

    by_name = {p.name: p for p in llm.classify_calls[0]}
    assert by_name["Alice"].samples == ["a1", "a2", "a3"]  # capped, own quotes, reading order
    assert len(by_name["Alice"].samples) == CLASSIFY_SAMPLE_LINES
    assert by_name["Bob"].samples == ["b1"]


def test_soft_fail_leaves_unknown() -> None:
    alice = _character("Alice")
    project = _project(alice)
    classify_speakers(project, FakeLLMProvider(raise_classify_unreachable=True))
    assert alice.category == VoiceCategory.UNKNOWN

    project2 = _project(_character("Bob"))
    classify_speakers(project2, FakeLLMProvider(raise_classify_malformed=True))
    assert project2.speakers[0].category == VoiceCategory.UNKNOWN


def test_no_targets_makes_no_call() -> None:
    already = _character("Zed")
    already.category = VoiceCategory.MAN
    project = _project(already)
    llm = FakeLLMProvider(classify_script={"Zed": ("woman", 0.9)})
    classify_speakers(project, llm)
    assert llm.classify_calls == []  # no unknown-category CHARACTER speakers to classify
    assert already.category == VoiceCategory.MAN  # untouched


def test_user_set_category_is_not_overwritten_only_unknown_is_classified() -> None:
    # A mixed re-run: one speaker the user already categorized (MAN) + one still UNKNOWN. Only
    # the UNKNOWN speaker is sent to the classifier; the user's category is preserved even
    # though the script would map it differently. Proves a re-run never clobbers a user choice.
    user_set = _character("Zed")
    user_set.category = VoiceCategory.MAN  # user's manual choice
    fresh = _character("Alice")
    project = _project(user_set, fresh)
    llm = FakeLLMProvider(classify_script={"Zed": ("woman", 0.9), "Alice": ("woman", 0.9)})

    classify_speakers(project, llm)

    assert user_set.category == VoiceCategory.MAN  # NOT overwritten to woman
    assert fresh.category == VoiceCategory.WOMAN  # the unknown one got classified
    # Only the still-unknown speaker was ever sent to the LLM.
    assert [p.name for p in llm.classify_calls[0]] == ["Alice"]

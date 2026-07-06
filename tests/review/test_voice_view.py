"""Pure-derivation unit tests for :mod:`casttrophizer.review.voice_view` — no Qt, no presenter.

Over a project mixing referenced-unvoiced, referenced-voiced, unreferenced, and
voice_clip_id-set-but-file-missing speakers, these assert each :class:`SpeakerVoiceRow`'s
``needs_voice`` **equals** membership in ``unresolved_speakers`` and ``is_referenced`` equals
membership in ``referenced_speaker_ids`` — the load-bearing anti-drift rule — plus the clip
label/path resolution, ``clip_file_missing``, the category options, and the needs-voice count.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.audio.synthesize import referenced_speaker_ids, unresolved_speakers
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, VoiceCategory
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    VoiceClip,
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.review.voice_view import (
    category_options,
    needs_voice_count,
    speaker_voice_rows,
)


def _mixed_project(fake_voice_clips: list[Path], tmp_path: Path) -> Project:
    """A project mixing every voice-state case (see module docstring)."""
    narrator_clip = VoiceClip(
        id=new_id("voice"), source_path=str(fake_voice_clips[0]), label="Narrator clip"
    )
    alice_clip = VoiceClip(
        id=new_id("voice"), source_path=str(fake_voice_clips[1]), label="Alice clip"
    )
    # Carol's clip id is set, but its file does not exist (deleted/renamed after assignment).
    carol_clip = VoiceClip(
        id=new_id("voice"), source_path=str(tmp_path / "gone.wav"), label="Carol clip"
    )

    narrator = Speaker(
        id=new_id("spk"),
        name="narrator",
        role=SpeakerRole.NARRATOR,
        voice_clip_id=narrator_clip.id,
        category=VoiceCategory.UNKNOWN,
    )
    alice = Speaker(
        id=new_id("spk"),
        name="Alice",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=alice_clip.id,
        category=VoiceCategory.WOMAN,
    )
    bob = Speaker(
        id=new_id("spk"),
        name="Bob",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=None,  # referenced but unvoiced -> needs_voice
        category=VoiceCategory.MAN,
    )
    carol = Speaker(
        id=new_id("spk"),
        name="Carol",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=carol_clip.id,  # set, but file missing -> needs_voice + clip_file_missing
        category=VoiceCategory.WOMAN,
    )
    dan = Speaker(
        id=new_id("spk"),
        name="Dan",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=None,  # unreferenced + unvoiced -> NOT a blocker
        category=VoiceCategory.MAN,
    )

    ch_id = new_id("ch")

    def _seg(text: str, speaker: Speaker) -> Segment:
        return Segment(
            id=new_id("seg"),
            text=text,
            speaker_id=speaker.id,
            role=speaker.role,
            confidence=1.0,
            review_status=ReviewStatus.APPROVED,
        )

    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text="mixed",
        segments=[
            _seg("Narration.", narrator),
            _seg('"Hi," said Alice.', alice),
            _seg('"Yo," said Bob.', bob),
            _seg('"Hello," said Carol.', carol),
        ],  # Dan is referenced by NO segment
    )
    chapter = Chapter(id=ch_id, order=0, title="One", lines=[line])
    book = Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter])
    return Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Mixed",
        workspace_dir=str(tmp_path),
        book=book,
        speakers=[narrator, alice, bob, carol, dan],
        voice_clips=[narrator_clip, alice_clip, carol_clip],
    )


def _row(rows: list, name: str):  # type: ignore[no-untyped-def]
    return next(r for r in rows if r.name == name)


def test_needs_voice_and_is_referenced_match_the_synthesize_predicates(
    fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    project = _mixed_project(fake_voice_clips, tmp_path)
    rows = speaker_voice_rows(project)

    referenced_ids = set(referenced_speaker_ids(project))
    needs_ids = {sp.id for sp in unresolved_speakers(project)}

    # Every row's flags equal membership in the exact synthesize predicates (no drift).
    for row in rows:
        assert row.is_referenced == (row.speaker_id in referenced_ids)
        assert row.needs_voice == (row.speaker_id in needs_ids)

    assert _row(rows, "Bob").needs_voice is True
    assert _row(rows, "Carol").needs_voice is True
    assert _row(rows, "narrator").needs_voice is False
    assert _row(rows, "Alice").needs_voice is False
    assert _row(rows, "Dan").needs_voice is False  # unreferenced -> not a blocker
    assert _row(rows, "Dan").is_referenced is False


def test_clip_label_path_and_missing_flag_resolution(
    fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    project = _mixed_project(fake_voice_clips, tmp_path)
    rows = speaker_voice_rows(project)

    alice = _row(rows, "Alice")
    assert alice.voice_clip_label == "Alice clip"
    assert alice.voice_clip_path == str(fake_voice_clips[1])  # feeds ▶ audition
    assert alice.clip_file_missing is False

    bob = _row(rows, "Bob")
    assert bob.voice_clip_label is None
    assert bob.voice_clip_path is None
    assert bob.clip_file_missing is False  # no clip id at all -> not "missing file"

    carol = _row(rows, "Carol")
    assert carol.voice_clip_label == "Carol clip"  # id resolves to the clip record...
    assert carol.clip_file_missing is True  # ...but the file is absent


def test_needs_voice_count_agrees_with_unresolved_speakers(
    fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    project = _mixed_project(fake_voice_clips, tmp_path)
    rows = speaker_voice_rows(project)

    assert needs_voice_count(rows) == len(unresolved_speakers(project))
    assert needs_voice_count(rows) == 2  # Bob + Carol


def test_speaker_voice_rows_are_one_per_speaker_in_order(
    fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    project = _mixed_project(fake_voice_clips, tmp_path)
    rows = speaker_voice_rows(project)
    assert [r.name for r in rows] == ["narrator", "Alice", "Bob", "Carol", "Dan"]


def test_category_options_lists_every_voice_category() -> None:
    options = category_options()
    assert [opt.value for opt in options] == list(VoiceCategory)
    assert {opt.display for opt in options} == {c.value for c in VoiceCategory}


def test_none_speaker_segment_makes_the_unvoiced_narrator_a_blocker(tmp_path: Path) -> None:
    """A ``speaker_id=None`` quote resolves to the narrator, so an unvoiced narrator needs a voice.

    Guards the ``referenced_speaker_ids`` alias's None→narrator resolution through the UI
    derivation: with only a speaker-less quote referencing it, the narrator row must still come
    back ``is_referenced`` and ``needs_voice`` (it is the fallback voice for that segment).
    """
    narrator = Speaker(
        id=new_id("spk"),
        name="narrator",
        role=SpeakerRole.NARRATOR,
        voice_clip_id=None,  # unvoiced narrator
    )
    ch_id = new_id("ch")
    # The only renderable segment is speaker_id=None (an unattributed dialogue quote).
    orphan = Segment(
        id=new_id("seg"),
        text='"Who said that?"',
        speaker_id=None,
        role=SpeakerRole.NARRATOR,
        confidence=0.0,
        review_status=ReviewStatus.NEEDS_REVIEW,
    )
    line = Line(id=new_id("line"), chapter_id=ch_id, order=0, text="q", segments=[orphan])
    chapter = Chapter(id=ch_id, order=0, title="One", lines=[line])
    book = Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter])
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Orphan",
        workspace_dir=str(tmp_path),
        book=book,
        speakers=[narrator],
    )

    rows = speaker_voice_rows(project)
    narrator_row = _row(rows, "narrator")
    assert narrator_row.is_referenced is True  # None resolves to the narrator
    assert narrator_row.needs_voice is True  # ...which is unvoiced -> a criterion-3 blocker
    assert needs_voice_count(rows) == 1
    assert narrator.id in set(referenced_speaker_ids(project))

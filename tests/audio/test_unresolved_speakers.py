"""``unresolved_speakers`` — the Speaker-object analogue of the review gate's precheck.

Confirms it matches ``unresolved_voices`` for the ordinary case (so ``assign-voice --rest``
targets exactly what the gate flags), returns real Speaker objects, and excludes the
``<unattributed>`` sentinel (which ``--rest`` cannot fix).
"""

from __future__ import annotations

import wave
from pathlib import Path

from casttrophizer.audio.synthesize import unresolved_speakers, unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Project, Segment, VoiceClip


def test_matches_the_gate_names(review_ready_project: Project) -> None:
    # review_ready_project: narrator + Alice voiced, Bob unvoiced (and referenced).
    speakers = unresolved_speakers(review_ready_project)
    assert [sp.name for sp in speakers] == unresolved_voices(review_ready_project) == ["Bob"]
    # Returns actual Speaker objects (not names).
    assert all(isinstance(sp.name, str) and sp.role == SpeakerRole.CHARACTER for sp in speakers)


def test_empty_when_every_referenced_speaker_is_voiced(
    synthesize_ready_project: Project,
) -> None:
    assert unresolved_speakers(synthesize_ready_project) == []
    assert unresolved_voices(synthesize_ready_project) == []


def test_excludes_unattributed_sentinel(review_ready_project: Project) -> None:
    # Plant a renderable segment with no speaker: unresolved_voices reports "<unattributed>",
    # but unresolved_speakers cannot (there is no Speaker to voice) — proving --rest's scope.
    chapter = review_ready_project.book.chapters[0]
    chapter.lines[0].segments.append(
        Segment(
            id=new_id("seg"),
            text="an orphan quote",
            speaker_id=None,
            role=SpeakerRole.NARRATOR,
            confidence=0.0,
            review_status=ReviewStatus.NEEDS_REVIEW,
        )
    )
    assert "<unattributed>" in unresolved_voices(review_ready_project)
    assert "<unattributed>" not in [sp.name for sp in unresolved_speakers(review_ready_project)]


def _silent_wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 128)
    return str(path)


def test_assigning_a_voice_removes_the_speaker_from_both_lists(
    review_ready_project: Project, tmp_path: Path
) -> None:
    # Bob starts unvoiced (in both lists). Assign him an existing clip -> he drops out of
    # BOTH unresolved_speakers and unresolved_voices, proving --rest's assignment clears the
    # same gate criterion the review gate reads.
    assert "Bob" in [sp.name for sp in unresolved_speakers(review_ready_project)]
    bob = next(sp for sp in review_ready_project.speakers if sp.name == "Bob")

    clip_path = _silent_wav(tmp_path / "bob.wav")
    clip = VoiceClip(id=new_id("voice"), source_path=clip_path, label="Bob")
    review_ready_project.voice_clips.append(clip)
    bob.voice_clip_id = clip.id

    assert "Bob" not in [sp.name for sp in unresolved_speakers(review_ready_project)]
    assert "Bob" not in unresolved_voices(review_ready_project)


def test_deleting_the_clip_file_re_adds_the_speaker_to_both_lists(
    review_ready_project: Project, tmp_path: Path
) -> None:
    # Voice resolution requires the reference file to exist on disk. Assign Bob a real clip
    # (resolves), then delete the file -> he re-appears in BOTH lists (the is_file() check in
    # _resolved_clip_path), matching the synthesize precheck exactly.
    bob = next(sp for sp in review_ready_project.speakers if sp.name == "Bob")
    clip_path = Path(_silent_wav(tmp_path / "bob.wav"))
    clip = VoiceClip(id=new_id("voice"), source_path=str(clip_path), label="Bob")
    review_ready_project.voice_clips.append(clip)
    bob.voice_clip_id = clip.id
    assert "Bob" not in unresolved_voices(review_ready_project)  # resolves while file exists

    clip_path.unlink()  # the user moved/deleted the reference clip

    assert "Bob" in [sp.name for sp in unresolved_speakers(review_ready_project)]
    assert "Bob" in unresolved_voices(review_ready_project)

"""``unresolved_speakers`` — the Speaker-object analogue of the review gate's precheck.

Confirms it matches ``unresolved_voices`` for the ordinary case (so ``assign-voice --rest``
targets exactly what the gate flags), returns real Speaker objects, and resolves a segment's
``speaker_id=None`` to the reserved narrator (voiced narrator drops out of both lists; unvoiced
narrator appears in both, so ``--rest`` can fix it). The only ``None`` case ``--rest`` cannot
fix is a narrator-less project, covered by the defensive test.
"""

from __future__ import annotations

import wave
from pathlib import Path

from casttrophizer.audio.synthesize import unresolved_speakers, unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Project, Segment, VoiceClip, find_narrator


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


def _plant_none_segment(project: Project) -> None:
    """Append a renderable ``speaker_id=None`` segment (resolves to the narrator at render)."""
    project.book.chapters[0].lines[0].segments.append(
        Segment(
            id=new_id("seg"),
            text="an orphan quote",
            speaker_id=None,
            role=SpeakerRole.NARRATOR,
            confidence=0.0,
            review_status=ReviewStatus.NEEDS_REVIEW,
        )
    )


def test_none_segment_resolves_to_voiced_narrator(review_ready_project: Project) -> None:
    # review_ready_project's narrator is voiced. A renderable None segment resolves to that
    # voiced narrator, so it is renderable AND surfaces no new blocker: the narrator appears in
    # neither list, and the "<unattributed>" sentinel never appears.
    _plant_none_segment(review_ready_project)

    voices = unresolved_voices(review_ready_project)
    speaker_names = [sp.name for sp in unresolved_speakers(review_ready_project)]
    assert "<unattributed>" not in voices
    assert "narrator" not in voices  # narrator is voiced -> not a blocker
    assert voices == ["Bob"]  # only the pre-existing unvoiced Bob
    assert "narrator" not in speaker_names


def test_none_segment_with_unvoiced_narrator_appears_in_both_lists(
    review_ready_project: Project,
) -> None:
    # Unassign the narrator's clip and plant a renderable None segment: the segment now resolves
    # to an *unvoiced* narrator, so the narrator surfaces by name in unresolved_voices AND as a
    # Speaker object in unresolved_speakers — proving `assign-voice --rest` can now voice it.
    narrator = find_narrator(review_ready_project)
    assert narrator is not None
    narrator.voice_clip_id = None  # narrator now unvoiced
    _plant_none_segment(review_ready_project)

    voices = unresolved_voices(review_ready_project)
    speakers = unresolved_speakers(review_ready_project)
    assert "narrator" in voices
    assert "<unattributed>" not in voices
    assert narrator in speakers  # a real Speaker object --rest can target


def test_defensive_no_narrator_reports_name_but_not_a_speaker(
    review_ready_project: Project,
) -> None:
    # Defensive can't-happen case: no NARRATOR speaker at all, yet a renderable None segment.
    # unresolved_voices reports "narrator" (clear, not a sentinel); unresolved_speakers cannot
    # include it (there is no Speaker to voice). find_narrator returns None.
    project = review_ready_project
    project.speakers = [sp for sp in project.speakers if sp.role != SpeakerRole.NARRATOR]
    # Repoint the two narrator-attributed segments to None so no dangling id is referenced.
    for ch in project.book.chapters:
        for ln in ch.lines:
            for seg in ln.segments:
                if seg.role == SpeakerRole.NARRATOR:
                    seg.speaker_id = None
    _plant_none_segment(project)

    assert find_narrator(project) is None
    voices = unresolved_voices(project)
    assert "narrator" in voices
    assert "<unattributed>" not in voices
    assert "narrator" not in [sp.name for sp in unresolved_speakers(project)]


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

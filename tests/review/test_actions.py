"""Pure review-action unit tests (no store, no Qt, no providers).

Covers every operation in :mod:`casttrophizer.review.actions`: text accept/reject/edit,
attribution approve/reject/override, speaker create/reassign, voice register/assign/unassign,
plus the indirect cache-key consequence and read-only-input guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer.attribution.policy import ensure_narrator, resolve_speaker
from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.models import Line, Project, Segment, Speaker
from casttrophizer.review import actions
from casttrophizer.workspace.audio_cache import AudioCache


def _first_line(project: Project) -> Line:
    return project.book.chapters[0].lines[0]


def _segment_with_status(project: Project, status: ReviewStatus) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == status
    )


def _speaker(project: Project, name: str) -> Speaker:
    return next(sp for sp in project.speakers if sp.name == name)


# --------------------------------------------------------------------------- #
# text suggestions
# --------------------------------------------------------------------------- #
def test_accept_suggestion_applies_text_and_approves(review_ready_project: Project) -> None:
    line = _first_line(review_ready_project)
    suggestion = line.suggestions[0]
    actions.accept_suggestion(line, suggestion.id)
    assert line.text == suggestion.suggested
    assert suggestion.status == ReviewStatus.APPROVED


def test_reject_suggestion_leaves_text(review_ready_project: Project) -> None:
    line = review_ready_project.book.chapters[0].lines[1]
    original_text = line.text
    suggestion = line.suggestions[0]
    actions.reject_suggestion(line, suggestion.id)
    assert line.text == original_text
    assert suggestion.status == ReviewStatus.REJECTED


def test_suggestion_action_raises_on_unknown_id(review_ready_project: Project) -> None:
    line = _first_line(review_ready_project)
    with pytest.raises(ValueError):
        actions.accept_suggestion(line, "sug_missing")


def test_edit_line_text_leaves_segments_untouched(review_ready_project: Project) -> None:
    line = _first_line(review_ready_project)
    before = [(s.id, s.text, s.speaker_id) for s in line.segments]
    actions.edit_line_text(line, "A brand new sentence.")
    assert line.text == "A brand new sentence."
    after = [(s.id, s.text, s.speaker_id) for s in line.segments]
    assert before == after  # segments NOT re-derived (deferred propagation)


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #
def test_approve_attribution(review_ready_project: Project) -> None:
    seg = _segment_with_status(review_ready_project, ReviewStatus.NEEDS_REVIEW)
    speaker_before, role_before = seg.speaker_id, seg.role
    actions.approve_attribution(seg)
    assert seg.review_status == ReviewStatus.APPROVED
    assert (seg.speaker_id, seg.role) == (speaker_before, role_before)


def test_reject_attribution(review_ready_project: Project) -> None:
    seg = _segment_with_status(review_ready_project, ReviewStatus.NEEDS_REVIEW)
    actions.reject_attribution(seg)
    assert seg.review_status == ReviewStatus.REJECTED


def test_set_segment_speaker_repoints_and_approves(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    alice = _speaker(project, "Alice")
    actions.set_segment_speaker(
        seg, project, speaker_id=alice.id, role=SpeakerRole.CHARACTER, approve=True
    )
    assert seg.speaker_id == alice.id
    assert seg.role == SpeakerRole.CHARACTER
    assert seg.review_status == ReviewStatus.APPROVED


def test_set_segment_speaker_no_approve(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    alice = _speaker(project, "Alice")
    actions.set_segment_speaker(
        seg, project, speaker_id=alice.id, role=SpeakerRole.CHARACTER, approve=False
    )
    assert seg.review_status == ReviewStatus.NEEDS_REVIEW  # untouched


def test_set_segment_speaker_raises_unknown(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    with pytest.raises(ValueError):
        actions.set_segment_speaker(seg, project, speaker_id="spk_nope", role=SpeakerRole.CHARACTER)


# --------------------------------------------------------------------------- #
# create / reassign speaker
# --------------------------------------------------------------------------- #
def test_create_character_new_and_reuse(review_ready_project: Project) -> None:
    project = review_ready_project
    n_before = len(project.speakers)
    carol = actions.create_character(project, "Carol")
    assert carol.role == SpeakerRole.CHARACTER
    assert len(project.speakers) == n_before + 1

    # case-insensitive reuse -> no duplicate; agrees with resolve_speaker.
    again = actions.create_character(project, "carol")
    assert again is carol
    assert len(project.speakers) == n_before + 1
    narrator = ensure_narrator(project)
    assert resolve_speaker(project, narrator, "CAROL") is carol


def test_reassign_segment_to_new_speaker(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    speaker = actions.reassign_segment_to_new_speaker(seg, project, "Dave")
    assert speaker.name == "Dave"
    assert speaker.role == SpeakerRole.CHARACTER
    assert seg.speaker_id == speaker.id
    assert seg.role == SpeakerRole.CHARACTER
    assert seg.review_status == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# voice clips
# --------------------------------------------------------------------------- #
def test_register_voice_clip_records_reference_no_copy(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    tmp_workspace,
) -> None:
    project = review_ready_project
    n_before = len(project.voice_clips)
    src = fake_voice_clips[0]
    clip = actions.register_voice_clip(project, src, "Bob's voice")

    assert clip in project.voice_clips
    assert len(project.voice_clips) == n_before + 1
    assert clip.source_path == str(Path(src))
    assert clip.label == "Bob's voice"
    # READ-ONLY input: nothing copied into the workspace audio dir.
    audio_dir = tmp_workspace.layout.audio_dir
    copied = list(audio_dir.glob("*")) if audio_dir.exists() else []
    assert copied == []


def test_register_voice_clip_raises_on_missing_path(
    review_ready_project: Project, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        actions.register_voice_clip(review_ready_project, tmp_path / "nope.wav", "ghost")


def test_assign_voice_clears_blocker(
    review_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    project = review_ready_project
    assert "Bob" in unresolved_voices(project)
    bob = _speaker(project, "Bob")
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Bob")
    actions.assign_voice(bob, clip)
    assert bob.voice_clip_id == clip.id
    assert "Bob" not in unresolved_voices(project)


def test_assign_voice_narrator_identical(
    review_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    project = review_ready_project
    narrator = _speaker(project, "narrator")
    narrator.voice_clip_id = None  # un-assign to prove the round-trip
    assert "narrator" in unresolved_voices(project)
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Narr")
    actions.assign_voice(narrator, clip)
    assert "narrator" not in unresolved_voices(project)


def test_assign_voice_by_ids_and_unknowns(review_ready_project: Project) -> None:
    project = review_ready_project
    bob = _speaker(project, "Bob")
    clip = project.voice_clips[0]
    actions.assign_voice_by_ids(project, speaker_id=bob.id, voice_clip_id=clip.id)
    assert bob.voice_clip_id == clip.id

    with pytest.raises(ValueError):
        actions.assign_voice_by_ids(project, speaker_id="spk_nope", voice_clip_id=clip.id)
    with pytest.raises(ValueError):
        actions.assign_voice_by_ids(project, speaker_id=bob.id, voice_clip_id="voice_nope")


def test_unassign_voice_reintroduces_blocker(review_ready_project: Project) -> None:
    project = review_ready_project
    alice = _speaker(project, "Alice")
    assert "Alice" not in unresolved_voices(project)
    actions.unassign_voice(alice)
    assert alice.voice_clip_id is None
    assert "Alice" in unresolved_voices(project)


# --------------------------------------------------------------------------- #
# cache-key consequence (indirect; no TTS)
# --------------------------------------------------------------------------- #
def test_set_segment_speaker_changes_cache_key(review_ready_project: Project) -> None:
    project = review_ready_project
    # The NEEDS_REVIEW Bob segment (Bob has no voice) -> repoint to voiced Alice.
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    alice = _speaker(project, "Alice")
    key_before = AudioCache.key_for(seg, project)
    actions.set_segment_speaker(seg, project, speaker_id=alice.id, role=SpeakerRole.CHARACTER)
    key_after = AudioCache.key_for(seg, project)
    assert key_before != key_after  # resolved voice changed -> re-render


def test_assign_voice_changes_cache_key(
    review_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    project = review_ready_project
    bob = _speaker(project, "Bob")
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)  # a Bob segment
    assert seg.speaker_id == bob.id
    key_before = AudioCache.key_for(seg, project)
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Bob")
    actions.assign_voice(bob, clip)
    key_after = AudioCache.key_for(seg, project)
    assert key_before != key_after  # voice now resolves -> re-render

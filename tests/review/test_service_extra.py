"""Service-layer gap tests: flag invalidation breadth + persistence of more actions.

Complements ``test_service.py``. The existing suite proves ``unassign_voice`` pops the
COMPLETED flag; here we also prove ``reassign_segment_to_new_speaker`` (introducing a
voice-less CHARACTER -> a fresh criterion-3 blocker) re-opens the gate, that
``set_segment_speaker`` persists + invalidates, and that the remaining un-round-tripped
actions (reject_suggestion, edit_line_text, register-only, set_segment_speaker) persist.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.models import Project, Segment
from casttrophizer.review.gate import is_review_complete
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.store import WorkspaceStore


def _reload(project: Project) -> Project:
    return WorkspaceStore.for_dir(project.workspace_dir).load()


def _needs_review_segment(project: Project) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    )


def test_reassign_to_voiceless_speaker_pops_completed_flag(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """Reassigning a segment to a brand-new (voice-less) speaker re-opens the gate."""
    project = review_ready_project
    project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    seg = project.book.chapters[0].lines[0].segments[0]  # currently a voiced Alice segment
    service.reassign_segment_to_new_speaker(seg, "Hank")  # Hank has no voice

    reloaded = _reload(project)
    assert str(StageName.REVIEW) not in reloaded.stage_status
    # The new voice-less speaker is a real criterion-3 blocker now.
    assert "Hank" in unresolved_voices(reloaded)


def test_set_segment_speaker_persists_and_invalidates(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    project = review_ready_project
    project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    seg = _needs_review_segment(project)
    alice = next(sp for sp in project.speakers if sp.name == "Alice")
    service.set_segment_speaker(seg, speaker_id=alice.id, role=SpeakerRole.CHARACTER)

    reloaded = _reload(project)
    assert str(StageName.REVIEW) not in reloaded.stage_status  # invalidated
    rseg = next(
        s for ch in reloaded.book.chapters for ln in ch.lines for s in ln.segments if s.id == seg.id
    )
    assert rseg.speaker_id == alice.id
    assert rseg.review_status == ReviewStatus.APPROVED


def test_reject_suggestion_persists(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    project = review_ready_project
    service = ReviewService(tmp_workspace, project)
    line = project.book.chapters[0].lines[1]
    original = line.text
    sug = line.suggestions[0]
    service.reject_suggestion(line, sug.id)

    reloaded = _reload(project)
    rline = reloaded.book.chapters[0].lines[1]
    assert rline.text == original  # unchanged
    assert rline.suggestions[0].status == ReviewStatus.REJECTED


def test_register_voice_clip_persists_without_assignment(
    tmp_workspace: WorkspaceStore,
    review_ready_project: Project,
    fake_voice_clips: list[Path],
) -> None:
    project = review_ready_project
    service = ReviewService(tmp_workspace, project)
    clip = service.register_voice_clip(fake_voice_clips[0], "spare")

    reloaded = _reload(project)
    assert any(c.id == clip.id and c.label == "spare" for c in reloaded.voice_clips)


def test_full_resolution_via_service_makes_gate_complete(
    tmp_workspace: WorkspaceStore,
    review_ready_project: Project,
    fake_voice_clips: list[Path],
) -> None:
    """End-to-end through the service: resolve all three blockers -> is_review_complete True."""
    project = review_ready_project
    service = ReviewService(tmp_workspace, project)
    assert is_review_complete(project) is False

    service.approve_attribution(_needs_review_segment(project))
    line1 = project.book.chapters[0].lines[1]
    service.accept_suggestion(line1, line1.suggestions[0].id)
    bob = next(sp for sp in project.speakers if sp.name == "Bob")
    clip = service.register_voice_clip(fake_voice_clips[0], "Bob")
    service.assign_voice(bob, clip)

    reloaded = _reload(project)
    assert is_review_complete(reloaded) is True

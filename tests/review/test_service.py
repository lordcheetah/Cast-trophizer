"""``ReviewService`` tests: each action mutates, persists, and (where required) invalidates.

Persistence is proven by reloading through a **fresh** ``WorkspaceStore`` (round-trip),
which also proves every mutated field serializes with no schema bump
(``CURRENT_SCHEMA_VERSION == 1``).
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.store import WorkspaceStore


def _reload(project: Project) -> Project:
    """Reload the saved project through a fresh store (no shared in-memory state)."""
    return WorkspaceStore.for_dir(project.workspace_dir).load()


def _needs_review_segment(project: Project):
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    )


def _speaker(project: Project, name: str):
    return next(sp for sp in project.speakers if sp.name == name)


def test_accept_suggestion_persists(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    service = ReviewService(tmp_workspace, review_ready_project)
    line = review_ready_project.book.chapters[0].lines[1]  # the PENDING-suggestion line
    sug = line.suggestions[0]
    before = line.text
    service.accept_suggestion(line, sug.id)

    reloaded = _reload(review_ready_project)
    rline = reloaded.book.chapters[0].lines[1]
    # Targeted token replacement (not a whole-line overwrite), persisted.
    assert rline.text == before.replace(sug.original, sug.suggested, 1)
    assert rline.suggestions[0].status == ReviewStatus.APPROVED


def test_approve_attribution_persists(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    service = ReviewService(tmp_workspace, review_ready_project)
    seg = _needs_review_segment(review_ready_project)
    service.approve_attribution(seg)

    reloaded = _reload(review_ready_project)
    rseg = next(
        s for ch in reloaded.book.chapters for ln in ch.lines for s in ln.segments if s.id == seg.id
    )
    assert rseg.review_status == ReviewStatus.APPROVED


def test_reassign_to_new_speaker_persists(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    service = ReviewService(tmp_workspace, review_ready_project)
    seg = _needs_review_segment(review_ready_project)
    speaker = service.reassign_segment_to_new_speaker(seg, "Eve")

    reloaded = _reload(review_ready_project)
    assert any(sp.name == "Eve" and sp.id == speaker.id for sp in reloaded.speakers)
    rseg = next(
        s for ch in reloaded.book.chapters for ln in ch.lines for s in ln.segments if s.id == seg.id
    )
    assert rseg.speaker_id == speaker.id
    assert rseg.review_status == ReviewStatus.APPROVED


def test_register_and_assign_voice_persists(
    tmp_workspace: WorkspaceStore,
    review_ready_project: Project,
    fake_voice_clips: list[Path],
) -> None:
    service = ReviewService(tmp_workspace, review_ready_project)
    bob = _speaker(review_ready_project, "Bob")
    clip = service.register_voice_clip(fake_voice_clips[0], "Bob")
    service.assign_voice(bob, clip)

    reloaded = _reload(review_ready_project)
    src = str(fake_voice_clips[0])
    assert any(c.id == clip.id and c.source_path == src for c in reloaded.voice_clips)
    rbob = next(sp for sp in reloaded.speakers if sp.id == bob.id)
    assert rbob.voice_clip_id == clip.id


def test_blocker_introducing_action_pops_review_flag(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """``unassign_voice`` after COMPLETED pops ``stage_status[REVIEW]`` (A.3a), per reload."""
    project = review_ready_project
    project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    service.unassign_voice(_speaker(project, "Alice"))

    reloaded = _reload(project)
    assert str(StageName.REVIEW) not in reloaded.stage_status


def test_non_blocker_action_keeps_review_flag(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """Approving an attribution does not re-introduce a blocker -> flag is preserved."""
    project = review_ready_project
    seg = _needs_review_segment(project)
    project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    service.approve_attribution(seg)

    reloaded = _reload(project)
    assert reloaded.stage_status.get(str(StageName.REVIEW)) == ReviewStatus.COMPLETED


def test_round_trip_keeps_schema_version(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    service = ReviewService(tmp_workspace, review_ready_project)
    service.edit_line_text(review_ready_project.book.chapters[0].lines[0], "edited")
    reloaded = _reload(review_ready_project)
    assert reloaded.schema_version == CURRENT_SCHEMA_VERSION
    assert reloaded.book.chapters[0].lines[0].text == "edited"


def test_blockers_delegates_to_gate(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    service = ReviewService(tmp_workspace, review_ready_project)
    blockers = service.blockers()
    assert blockers.unassigned_voices == ["Bob"]
    assert not blockers.is_empty

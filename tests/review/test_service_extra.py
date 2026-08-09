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
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName, VoiceCategory
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Line, Project, Segment, TextSuggestion
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


def test_accept_suggestion_persists_segment_propagation(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """Through the service, accepting fixes the segment text + resets its cache, on disk."""
    project = review_ready_project
    narrator = next(sp for sp in project.speakers if sp.role == SpeakerRole.NARRATOR)
    seg = Segment(
        id=new_id("seg"),
        text="The narrarator spoke.",
        speaker_id=narrator.id,
        role=SpeakerRole.NARRATOR,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
        audio_cache_key="STALE",
        audio_status=ReviewStatus.COMPLETED,
    )
    line = Line(
        id=new_id("line"),
        chapter_id=project.book.chapters[0].id,
        order=99,
        text="The narrarator spoke.",
        segments=[seg],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="narrarator",
                suggested="narrator",
                reason="spellcheck",
                confidence=0.7,
                status=ReviewStatus.PENDING,
            )
        ],
    )
    project.book.chapters[0].lines.append(line)
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    service.accept_suggestion(line, line.suggestions[0].id)

    reloaded = _reload(project)
    rline = reloaded.book.chapters[0].lines[-1]
    assert rline.text == "The narrator spoke."
    assert rline.segments[0].text == "The narrator spoke."
    assert rline.segments[0].audio_cache_key is None
    assert rline.segments[0].audio_status == ReviewStatus.PENDING


def test_edit_line_text_resegment_pops_completed_flag_and_persists(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """A re-segmenting line edit re-opens the gate (pops REVIEW) and persists the new text."""
    project = review_ready_project
    project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    line = project.book.chapters[0].lines[0]
    service.edit_line_text(line, '"A brand new quote," said Zed.')

    reloaded = _reload(project)
    assert str(StageName.REVIEW) not in reloaded.stage_status  # re-opened by the new quote
    assert reloaded.book.chapters[0].lines[0].text == '"A brand new quote," said Zed.'


# --------------------------------------------------------------------------- #
# render-stage invalidation: a review edit that dirties a segment's audio must re-open
# SYNTHESIZE **and** ASSEMBLE so a *post-render* correction actually re-renders + rebuilds
# the M4B (else the runner skips the still-COMPLETED stages and keeps stale audio).
# --------------------------------------------------------------------------- #
def _complete_render(project: Project) -> None:
    """Mark REVIEW + SYNTHESIZE + ASSEMBLE COMPLETED — a fully-rendered project."""
    for stage in (StageName.REVIEW, StageName.SYNTHESIZE, StageName.ASSEMBLE):
        project.stage_status[str(stage)] = ReviewStatus.COMPLETED


def _render_reopened(project: Project) -> bool:
    """True iff BOTH render stages were popped (the next run re-renders + reassembles)."""
    return (
        str(StageName.SYNTHESIZE) not in project.stage_status
        and str(StageName.ASSEMBLE) not in project.stage_status
    )


def test_edit_line_text_after_render_reopens_synth_and_assemble(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """The reviewer's post-render footgun: editing a line on a finished M4B must re-render."""
    project = review_ready_project
    _complete_render(project)
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    service.edit_line_text(project.book.chapters[0].lines[0], '"A brand new quote," said Zed.')

    assert _render_reopened(_reload(project)), "post-render line edit left synth/assemble complete"


def test_accept_suggestion_dirtying_segment_after_render_reopens_synth_and_assemble(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """Accepting a correction that reaches a segment must re-open the render stages."""
    project = review_ready_project
    narrator = next(sp for sp in project.speakers if sp.role == SpeakerRole.NARRATOR)
    line = Line(
        id=new_id("line"),
        chapter_id=project.book.chapters[0].id,
        order=99,
        text="The narrarator spoke.",
        segments=[
            Segment(
                id=new_id("seg"),
                text="The narrarator spoke.",
                speaker_id=narrator.id,
                role=SpeakerRole.NARRATOR,
                confidence=1.0,
                review_status=ReviewStatus.APPROVED,
                audio_cache_key="STALE",
                audio_status=ReviewStatus.COMPLETED,
            )
        ],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="narrarator",
                suggested="narrator",
                reason="spellcheck",
                confidence=0.7,
                status=ReviewStatus.PENDING,
            )
        ],
    )
    project.book.chapters[0].lines.append(line)
    _complete_render(project)
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    service.accept_suggestion(line, line.suggestions[0].id)

    assert _render_reopened(_reload(project))


def test_accept_suggestion_not_reaching_a_segment_leaves_render_complete(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """A line-text-only accept (token in no segment) changes no audio -> render stays complete."""
    project = review_ready_project
    narrator = next(sp for sp in project.speakers if sp.role == SpeakerRole.NARRATOR)
    line = Line(
        id=new_id("line"),
        chapter_id=project.book.chapters[0].id,
        order=98,
        text="A stage cue [note].",
        segments=[
            Segment(
                id=new_id("seg"),
                text="A stage cue.",  # segment does NOT contain the suggested token
                speaker_id=narrator.id,
                role=SpeakerRole.NARRATOR,
                confidence=1.0,
                review_status=ReviewStatus.APPROVED,
                audio_cache_key="KEEP",
                audio_status=ReviewStatus.COMPLETED,
            )
        ],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="[note]",
                suggested="",
                reason="ocr-artifact",
                confidence=0.9,
                status=ReviewStatus.PENDING,
            )
        ],
    )
    project.book.chapters[0].lines.append(line)
    _complete_render(project)
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    service.accept_suggestion(line, line.suggestions[0].id)

    reloaded = _reload(project)
    assert not _render_reopened(reloaded)  # no segment dirtied -> render still complete
    assert reloaded.book.chapters[0].lines[-1].segments[0].audio_cache_key == "KEEP"


def test_set_segment_speaker_after_render_reopens_synth_and_assemble(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """A speaker change alters the segment's resolved voice -> its cache key -> must re-render."""
    project = review_ready_project
    _complete_render(project)
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    seg = _needs_review_segment(project)
    alice = next(sp for sp in project.speakers if sp.name == "Alice")
    service.set_segment_speaker(seg, speaker_id=alice.id, role=SpeakerRole.CHARACTER)

    assert _render_reopened(_reload(project))


def test_set_speaker_category_leaves_render_complete(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """Category is not in the audio cache key -> setting it must NOT re-open the render stages."""
    project = review_ready_project
    _complete_render(project)
    tmp_workspace.save(project)

    service = ReviewService(tmp_workspace, project)
    speaker = next(sp for sp in project.speakers if sp.name == "Alice")
    service.set_speaker_category(speaker, VoiceCategory.WOMAN)

    reloaded = _reload(project)
    assert not _render_reopened(reloaded)  # cache-neutral edit -> render untouched
    assert str(StageName.SYNTHESIZE) in reloaded.stage_status


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


# --------------------------------------------------------------------------- #
# per-segment audio review (post-synthesize): approve / reroll / commit
# --------------------------------------------------------------------------- #
def _completed_segment(project: Project) -> Segment:
    seg = next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text.strip()
    )
    seg.audio_status = ReviewStatus.COMPLETED
    seg.audio_cache_key = "old-key"
    return seg


def test_approve_audio_flips_to_approved_without_reopening_review(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    project = review_ready_project
    project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    seg = _completed_segment(project)
    tmp_workspace.save(project)
    service = ReviewService(tmp_workspace, project)

    service.approve_audio(seg)

    assert seg.audio_status == ReviewStatus.APPROVED
    reloaded = _reload(project)
    # Approval is a curation marker: it must NOT re-open the pre-synth review gate.
    assert str(StageName.REVIEW) in reloaded.stage_status


def test_reroll_audio_sets_new_seed_resets_key_and_reopens_render(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    project = review_ready_project
    project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    project.stage_status[str(StageName.SYNTHESIZE)] = ReviewStatus.COMPLETED
    project.stage_status[str(StageName.ASSEMBLE)] = ReviewStatus.COMPLETED
    seg = _completed_segment(project)
    tmp_workspace.save(project)
    service = ReviewService(tmp_workspace, project)

    service.reroll_audio(seg)

    assert seg.audio_seed is not None  # a fresh seed was drawn
    assert seg.audio_cache_key is None
    assert seg.audio_status == ReviewStatus.PENDING
    reloaded = _reload(project)
    # Render stages re-opened so the fresh take (re-)synthesizes and the M4B rebuilds...
    assert str(StageName.SYNTHESIZE) not in reloaded.stage_status
    assert str(StageName.ASSEMBLE) not in reloaded.stage_status
    # ...but the pre-synth review gate is untouched (audio review is post-synth).
    assert str(StageName.REVIEW) in reloaded.stage_status


def test_reroll_audio_draws_a_different_seed_each_time(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    project = review_ready_project
    seg = _completed_segment(project)
    tmp_workspace.save(project)
    service = ReviewService(tmp_workspace, project)

    service.reroll_audio(seg)
    first = seg.audio_seed
    service.reroll_audio(seg)
    second = seg.audio_seed

    assert first is not None and second is not None
    assert first != second  # a re-roll always changes the seed (hence the take)


def test_commit_rendered_audio_persists_current_state(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    project = review_ready_project
    seg = _completed_segment(project)
    tmp_workspace.save(project)
    service = ReviewService(tmp_workspace, project)

    # Simulate the worker having stamped the segment in memory, then commit.
    seg.audio_status = ReviewStatus.COMPLETED
    seg.audio_cache_key = "fresh-key"
    service.commit_rendered_audio(seg)

    reloaded = _reload(project)
    committed = next(
        s for ch in reloaded.book.chapters for ln in ch.lines for s in ln.segments if s.id == seg.id
    )
    assert committed.audio_cache_key == "fresh-key"
    assert committed.audio_status == ReviewStatus.COMPLETED

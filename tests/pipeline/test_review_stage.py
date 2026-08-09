"""ReviewStage integration tests (against the saved project; Qt-free, offline).

Proves the gate: ``run`` returns NEEDS_REVIEW while blockers remain (writes nothing) and
COMPLETED once clear (writes ``stage_status[REVIEW]``, persists); the ``Pipeline`` runner
HALTS at review and synthesize never runs; clearing blockers advances ``next_stage`` to
synthesize and satisfies its voice precheck. No providers are needed — ``ctx.llm``/``ctx.tts``
are None.
"""

from __future__ import annotations

from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project, Segment
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.review import ReviewStage
from casttrophizer.pipeline.stages.synthesize import SynthesizeStage
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeTTSProvider, RecordingProgressReporter


def _ctx(store: WorkspaceStore, **kwargs) -> StageContext:
    return StageContext(store=store, progress=RecordingProgressReporter(), **kwargs)


def _needs_review_segment(project: Project) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    )


def _clear_all_blockers(store: WorkspaceStore, project: Project) -> None:
    """Resolve every blocker via the service (approve attribution, accept suggestion, voice)."""
    service = ReviewService(store, project)
    # attribution
    seg = _needs_review_segment(project)
    service.approve_attribution(seg)
    # pending suggestion
    line1 = project.book.chapters[0].lines[1]
    service.accept_suggestion(line1, line1.suggestions[0].id)
    # Bob's voice: reuse the narrator clip (existing on disk).
    bob = next(sp for sp in project.speakers if sp.name == "Bob")
    service.assign_voice(bob, project.voice_clips[0])


def test_run_needs_review_while_blockers_present(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    result = ReviewStage().run(review_ready_project, _ctx(tmp_workspace))
    assert result.stage == StageName.REVIEW
    assert result.status == ReviewStatus.NEEDS_REVIEW
    assert "voices needed for: Bob" in result.message
    # writes nothing: the flag stays unset, on disk too.
    assert str(StageName.REVIEW) not in review_ready_project.stage_status
    reloaded = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()
    assert str(StageName.REVIEW) not in reloaded.stage_status


def test_no_providers_needed(tmp_workspace: WorkspaceStore, review_ready_project: Project) -> None:
    ctx = StageContext(
        store=tmp_workspace, progress=RecordingProgressReporter(), llm=None, tts=None
    )
    result = ReviewStage().run(review_ready_project, ctx)
    assert result.status == ReviewStatus.NEEDS_REVIEW


def test_is_complete_status_driven(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    stage = ReviewStage()
    assert stage.is_complete(review_ready_project) is False
    review_ready_project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
    assert stage.is_complete(review_ready_project) is True


def test_runner_halts_at_review_synthesize_never_runs(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    tts = FakeTTSProvider()
    pipeline = Pipeline([ReviewStage(), SynthesizeStage()])
    result = pipeline.run(_ctx(tmp_workspace, tts=tts))

    assert result.stage == StageName.REVIEW
    assert result.status == ReviewStatus.NEEDS_REVIEW
    assert tts.synthesize_calls == []  # synthesize never reached
    reloaded = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()
    assert str(StageName.SYNTHESIZE) not in reloaded.stage_status


def test_clear_blockers_then_completed_and_persisted(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    _clear_all_blockers(tmp_workspace, review_ready_project)
    # work against the reloaded project (proves persistence chained correctly).
    project = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()

    stage = ReviewStage()
    result = stage.run(project, _ctx(tmp_workspace))
    assert result.status == ReviewStatus.COMPLETED

    reloaded = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()
    assert reloaded.stage_status.get(str(StageName.REVIEW)) == ReviewStatus.COMPLETED
    assert stage.is_complete(reloaded) is True

    # next_stage now advances to synthesize.
    pipeline = Pipeline([ReviewStage(), SynthesizeStage()])
    assert pipeline.next_stage(reloaded).name == StageName.SYNTHESIZE


def test_next_stage_is_review_until_complete(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    pipeline = Pipeline([ReviewStage(), SynthesizeStage()])
    assert pipeline.next_stage(review_ready_project).name == StageName.REVIEW


def test_voice_precondition_parity_with_synthesize(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """After review COMPLETED, synthesize's voice precheck passes (no FAILED on voices)."""
    _clear_all_blockers(tmp_workspace, review_ready_project)
    project = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()
    ReviewStage().run(project, _ctx(tmp_workspace))
    project = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()

    assert unresolved_voices(project) == []

    tts = FakeTTSProvider()
    result = SynthesizeStage().run(project, _ctx(tmp_workspace, tts=tts))
    assert result.status == ReviewStatus.COMPLETED  # did not FAIL on voices
    assert tts.synthesize_calls  # actually rendered


def test_idempotent_needs_review(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    stage = ReviewStage()
    r1 = stage.run(review_ready_project, _ctx(tmp_workspace))
    r2 = stage.run(review_ready_project, _ctx(tmp_workspace))
    assert r1.status == r2.status == ReviewStatus.NEEDS_REVIEW
    reloaded = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()
    assert str(StageName.REVIEW) not in reloaded.stage_status


def test_edit_after_complete_reopens_gate(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    _clear_all_blockers(tmp_workspace, review_ready_project)
    project = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()
    stage = ReviewStage()
    stage.run(project, _ctx(tmp_workspace))
    project = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()
    assert stage.is_complete(project) is True

    # un-assign a voice via the service -> flag popped, gate re-opens.
    service = ReviewService(tmp_workspace, project)
    service.unassign_voice(next(sp for sp in project.speakers if sp.name == "Alice"))
    project = WorkspaceStore.for_dir(review_ready_project.workspace_dir).load()

    assert stage.is_complete(project) is False
    pipeline = Pipeline([ReviewStage(), SynthesizeStage()])
    assert pipeline.next_stage(project).name == StageName.REVIEW

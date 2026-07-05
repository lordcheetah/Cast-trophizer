"""``interpret_result`` — map each StageResult status to the right RunOutcome."""

from __future__ import annotations

from casttrophizer.app_service.outcome import RunOutcomeKind, interpret_result
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import StageResult
from casttrophizer.workspace.store import WorkspaceStore

_FINAL = StageName.ASSEMBLE


def test_completed_full_pipeline_reports_output_path(tmp_workspace: WorkspaceStore) -> None:
    tmp_workspace.layout.output_dir.mkdir(parents=True, exist_ok=True)
    m4b = tmp_workspace.layout.output_dir / "book.m4b"
    m4b.write_bytes(b"FAKE-M4B")

    result = StageResult(stage=StageName.ASSEMBLE, status=ReviewStatus.COMPLETED)
    outcome = interpret_result(result, tmp_workspace, until=None, final=_FINAL)

    assert outcome.kind == RunOutcomeKind.COMPLETED
    assert outcome.output_path == m4b


def test_completed_without_m4b_has_no_output_path(tmp_workspace: WorkspaceStore) -> None:
    result = StageResult(stage=StageName.ASSEMBLE, status=ReviewStatus.COMPLETED)
    outcome = interpret_result(result, tmp_workspace, until=None, final=_FINAL)
    assert outcome.kind == RunOutcomeKind.COMPLETED
    assert outcome.output_path is None


def test_until_early_stop_completed_maps_to_stopped(tmp_workspace: WorkspaceStore) -> None:
    """A COMPLETED result for a pre-final stage under ``until`` is an early stop, not done."""
    result = StageResult(stage=StageName.PARSE, status=ReviewStatus.COMPLETED)
    outcome = interpret_result(result, tmp_workspace, until=StageName.PARSE, final=_FINAL)
    assert outcome.kind == RunOutcomeKind.STOPPED
    assert "parse" in outcome.summary


def test_needs_review_carries_blockers(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    result = StageResult(stage=StageName.REVIEW, status=ReviewStatus.NEEDS_REVIEW)
    outcome = interpret_result(result, tmp_workspace, until=None, final=_FINAL)

    assert outcome.kind == RunOutcomeKind.NEEDS_REVIEW
    assert outcome.blockers is not None
    # review_ready_project has 1 needs-review segment, 1 pending suggestion, Bob unvoiced.
    assert outcome.blockers.needs_attribution
    assert outcome.blockers.pending_suggestions
    assert outcome.blockers.unassigned_voices == ["Bob"]
    assert outcome.summary  # describe_blockers, non-empty


def test_stopped_status_maps_to_stopped(tmp_workspace: WorkspaceStore) -> None:
    result = StageResult(stage=StageName.SYNTHESIZE, status=ReviewStatus.STOPPED)
    outcome = interpret_result(result, tmp_workspace, until=None, final=_FINAL)
    assert outcome.kind == RunOutcomeKind.STOPPED


def test_failed_status_carries_message(tmp_workspace: WorkspaceStore) -> None:
    result = StageResult(
        stage=StageName.SYNTHESIZE, status=ReviewStatus.FAILED, message="TTS unavailable"
    )
    outcome = interpret_result(result, tmp_workspace, until=None, final=_FINAL)
    assert outcome.kind == RunOutcomeKind.FAILED
    assert outcome.message == "TTS unavailable"

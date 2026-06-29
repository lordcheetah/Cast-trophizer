"""Pipeline runner tests: next_stage, resume (skip complete), and cooperative stop.

The real stage bodies are stubs, so these tests use small in-test stages that record
completion in ``project.stage_status`` — exactly the resume mechanism the runner relies
on. Fakes + a recording progress reporter keep everything offline and Qt-free.
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import RecordingProgressReporter


class RecordingStage(Stage):
    """A stage that marks itself complete in ``stage_status`` and persists the project.

    If ``ctx.progress.should_stop()`` is set, it returns STOPPED *without* marking
    completion — mirroring a real stage that saved partial state and bailed.
    """

    def __init__(self, name: StageName, ran_log: list[StageName]) -> None:
        self.name = name
        self._ran_log = ran_log

    def is_complete(self, project: Project) -> bool:
        return project.stage_status.get(str(self.name)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        self._ran_log.append(self.name)
        if ctx.progress.should_stop():
            ctx.store.save(project)  # partial state persisted
            return StageResult(stage=self.name, status=ReviewStatus.STOPPED)
        project.stage_status[str(self.name)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(stage=self.name, status=ReviewStatus.COMPLETED)


def _ctx(store: WorkspaceStore, progress: RecordingProgressReporter) -> StageContext:
    return StageContext(store=store, progress=progress)


def test_next_stage_returns_first_incomplete(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    log: list[StageName] = []
    pipeline = Pipeline(
        [
            RecordingStage(StageName.PARSE, log),
            RecordingStage(StageName.CORRECT, log),
            RecordingStage(StageName.ATTRIBUTE, log),
        ]
    )
    # sample_project already has PARSE = COMPLETED.
    nxt = pipeline.next_stage(sample_project)
    assert nxt is not None and nxt.name == StageName.CORRECT


def test_next_stage_none_when_all_complete(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    log: list[StageName] = []
    pipeline = Pipeline([RecordingStage(StageName.PARSE, log)])
    # PARSE is already COMPLETED in the fixture.
    assert pipeline.next_stage(sample_project) is None


def test_run_skips_complete_stages(tmp_workspace: WorkspaceStore, sample_project: Project) -> None:
    log: list[StageName] = []
    pipeline = Pipeline(
        [
            RecordingStage(StageName.PARSE, log),
            RecordingStage(StageName.CORRECT, log),
        ]
    )
    progress = RecordingProgressReporter()
    result = pipeline.run(_ctx(tmp_workspace, progress))
    # PARSE was already complete, so only CORRECT ran.
    assert log == [StageName.CORRECT]
    assert result.status == ReviewStatus.COMPLETED
    assert result.stage == StageName.CORRECT


def test_stop_halts_and_resume_continues(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    log: list[StageName] = []
    stages = [
        RecordingStage(StageName.CORRECT, log),
        RecordingStage(StageName.ATTRIBUTE, log),
    ]
    pipeline = Pipeline(stages)

    # First run: stop is requested, so the first incomplete stage returns STOPPED and the
    # run halts before reaching the second stage.
    stopping = RecordingProgressReporter(stop=True)
    result = pipeline.run(_ctx(tmp_workspace, stopping))
    assert result.status == ReviewStatus.STOPPED
    assert result.stage == StageName.CORRECT
    assert log == [StageName.CORRECT]

    # Persisted state shows CORRECT is NOT complete (it stopped).
    persisted = tmp_workspace.load()
    assert persisted.stage_status.get(str(StageName.CORRECT)) != ReviewStatus.COMPLETED

    # Second run: no stop requested -> resumes, CORRECT then ATTRIBUTE complete.
    resuming = RecordingProgressReporter()
    result2 = pipeline.run(_ctx(tmp_workspace, resuming))
    assert result2.status == ReviewStatus.COMPLETED
    assert result2.stage == StageName.ATTRIBUTE
    assert log == [StageName.CORRECT, StageName.CORRECT, StageName.ATTRIBUTE]


def test_run_until_stops_after_named_stage(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    log: list[StageName] = []
    pipeline = Pipeline(
        [
            RecordingStage(StageName.CORRECT, log),
            RecordingStage(StageName.ATTRIBUTE, log),
            RecordingStage(StageName.REVIEW, log),
        ]
    )
    progress = RecordingProgressReporter()
    result = pipeline.run(_ctx(tmp_workspace, progress), until=StageName.ATTRIBUTE)
    assert result.stage == StageName.ATTRIBUTE
    assert log == [StageName.CORRECT, StageName.ATTRIBUTE]


class PartialThenStopStage(Stage):
    """Writes a recoverable partial result, then stops if asked.

    Models a real long stage (e.g. synthesize) that persists work-in-progress before
    polling ``should_stop`` — so a later resume must see that partial state, not restart
    from scratch. ``is_complete`` only flips once the stage has fully finished.
    """

    def __init__(self, name: StageName) -> None:
        self.name = name

    def is_complete(self, project: Project) -> bool:
        return project.stage_status.get(str(self.name)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        # Always record an item of partial progress and persist it first.
        done = project.tts_params.setdefault("_items_done", 0)
        project.tts_params["_items_done"] = done + 1
        ctx.store.save(project)

        if ctx.progress.should_stop():
            return StageResult(stage=self.name, status=ReviewStatus.STOPPED)

        project.stage_status[str(self.name)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(stage=self.name, status=ReviewStatus.COMPLETED)


def test_stop_persists_partial_state_then_resume_sees_it(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    # Stage is not pre-completed in the fixture, so it will actually run.
    pipeline = Pipeline([PartialThenStopStage(StageName.SYNTHESIZE)])

    # First run stops after persisting one item of partial progress.
    result = pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter(stop=True)))
    assert result.status == ReviewStatus.STOPPED

    persisted = tmp_workspace.load()
    assert persisted.tts_params["_items_done"] == 1
    assert persisted.stage_status.get(str(StageName.SYNTHESIZE)) != ReviewStatus.COMPLETED

    # Resume: the partial counter is carried forward (2, not reset to 1), proving the
    # stage resumed on top of persisted state rather than restarting.
    result2 = pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter()))
    assert result2.status == ReviewStatus.COMPLETED

    final = tmp_workspace.load()
    assert final.tts_params["_items_done"] == 2
    assert final.stage_status.get(str(StageName.SYNTHESIZE)) == ReviewStatus.COMPLETED


class NeedsReviewStage(Stage):
    """A human-gate stage (like ReviewStage) that halts the pipeline with NEEDS_REVIEW."""

    def __init__(self, name: StageName, ran_log: list[StageName]) -> None:
        self.name = name
        self._ran_log = ran_log

    def is_complete(self, project: Project) -> bool:
        return project.stage_status.get(str(self.name)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        self._ran_log.append(self.name)
        ctx.store.save(project)
        return StageResult(stage=self.name, status=ReviewStatus.NEEDS_REVIEW)


def test_needs_review_halts_pipeline_before_later_stages(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    log: list[StageName] = []
    pipeline = Pipeline(
        [
            NeedsReviewStage(StageName.REVIEW, log),
            RecordingStage(StageName.SYNTHESIZE, log),
        ]
    )
    result = pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter()))
    # The human gate stops the run; the synthesize stage must not have executed.
    assert result.status == ReviewStatus.NEEDS_REVIEW
    assert result.stage == StageName.REVIEW
    assert log == [StageName.REVIEW]

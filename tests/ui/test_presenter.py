"""Loop-free unit tests for :class:`ProjectPresenter` — no Qt, no event loop, no thread.

A :class:`FakeProjectView` records what the presenter pushes; a :class:`FakeRunExecutor`
synchronously pumps progress events and then invokes ``on_finished`` / ``on_failed`` with a
chosen :class:`StageResult`. Providers and the pipeline are the shared offline fakes, so no
real model / thread is exercised — only the presenter's reactions.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from casttrophizer.app_service import AppServiceDeps, RunOutcomeKind
from casttrophizer.app_service.status import StageRow
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project, Segment
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageResult
from casttrophizer.providers import LLMProvider, TTSProvider
from casttrophizer.review.service import ReviewService
from casttrophizer.ui.presenter import ProjectPresenter, ProjectView, RunExecutor
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeLLMProvider, FakeM4BAssembler, FakeTTSProvider

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
ProgressEvent = tuple[str, object]


class FakeProjectView:
    """Records every call the presenter makes (structurally a :class:`ProjectView`)."""

    def __init__(self) -> None:
        self.project_loaded: tuple[str, str] | None = None
        self.stage_rows: list[StageRow] | None = None
        self.next_stage: object = "UNSET"
        self.review_available: list[bool] = []
        self.voice_available: list[bool] = []
        self.running: list[bool] = []
        self.progress: list[tuple[int, int, str]] = []
        self.outcomes: list[object] = []
        self.errors: list[tuple[str, str]] = []

    def show_project_loaded(self, name: str, workspace: str) -> None:
        self.project_loaded = (name, workspace)

    def show_stage_rows(self, rows: list[StageRow]) -> None:
        self.stage_rows = rows

    def show_next_stage(self, name: StageName | None) -> None:
        self.next_stage = name

    def set_review_available(self, available: bool) -> None:
        self.review_available.append(available)

    def set_voice_available(self, available: bool) -> None:
        self.voice_available.append(available)

    def set_running(self, running: bool) -> None:
        self.running.append(running)

    def set_progress(self, done: int, total: int, message: str) -> None:
        self.progress.append((done, total, message))

    def show_outcome(self, outcome: object) -> None:
        self.outcomes.append(outcome)

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))


class FakeRunExecutor:
    """Synchronously pumps progress then delivers a terminal result (no thread)."""

    def __init__(
        self,
        *,
        result: StageResult | None = None,
        failure: str | None = None,
        progress_events: tuple[ProgressEvent, ...] = (),
    ) -> None:
        self._result = result
        self._failure = failure
        self._progress_events = progress_events
        self.started = False
        self.stop_requested = False

    def start(
        self,
        pipeline: Pipeline,
        store: WorkspaceStore,
        llm: LLMProvider,
        tts: TTSProvider,
        config: AppConfig,
        *,
        on_total: Callable[[int], None],
        on_advance: Callable[[int, str], None],
        on_message: Callable[[str], None],
        on_finished: Callable[[StageResult], None],
        on_failed: Callable[[str], None],
    ) -> None:
        self.started = True
        for kind, payload in self._progress_events:
            if kind == "total":
                on_total(int(payload))  # type: ignore[arg-type]
            elif kind == "advance":
                delta, message = payload  # type: ignore[misc]
                on_advance(int(delta), str(message))
            elif kind == "message":
                on_message(str(payload))
        if self._failure is not None:
            on_failed(self._failure)
        elif self._result is not None:
            on_finished(self._result)

    def request_stop(self) -> None:
        self.stop_requested = True


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _deps(tmp_path: Path, *, llm: LLMProvider | None = None) -> AppServiceDeps:
    return AppServiceDeps(
        llm_factory=lambda _cfg: llm or FakeLLMProvider(),
        tts_factory=lambda _cfg: FakeTTSProvider(),
        assembler=FakeM4BAssembler(),
        config=AppConfig(workspaces_root=tmp_path),
    )


def _presenter(
    view: FakeProjectView, executor: RunExecutor, deps: AppServiceDeps
) -> ProjectPresenter:
    return ProjectPresenter(view=view, executor=executor, deps=deps)


# --------------------------------------------------------------------------- #
# open / create
# --------------------------------------------------------------------------- #
def test_open_renders_six_stage_rows_and_next_stage(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(), _deps(tmp_path))

    presenter.open(tmp_workspace.layout.root)

    assert view.project_loaded == ("Review Ready", str(tmp_workspace.layout.root))
    assert view.stage_rows is not None and len(view.stage_rows) == 6
    assert view.next_stage == StageName.REVIEW


def test_create_renders_rows_with_parse_next(tmp_path: Path, sample_epub: Path) -> None:
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(), _deps(tmp_path))

    presenter.create(sample_epub, "My Book")

    assert view.project_loaded is not None
    assert view.project_loaded[0] == "My Book"
    assert view.next_stage == StageName.PARSE
    assert view.errors == []


def test_open_missing_project_shows_error(tmp_path: Path) -> None:
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(), _deps(tmp_path))

    presenter.open(tmp_path / "empty")

    assert view.project_loaded is None
    assert len(view.errors) == 1
    assert "no project" in view.errors[0][1]


def test_create_duplicate_shows_error(tmp_path: Path, sample_epub: Path) -> None:
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(), _deps(tmp_path))

    presenter.create(sample_epub, "Dup")
    presenter.create(sample_epub, "Dup")  # same workspace -> ProjectExistsError

    assert len(view.errors) == 1
    assert "already exists" in view.errors[0][1]


# --------------------------------------------------------------------------- #
# run outcomes
# --------------------------------------------------------------------------- #
def test_start_run_completed_shows_output_path(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    tmp_workspace.layout.output_dir.mkdir(parents=True, exist_ok=True)
    m4b = tmp_workspace.layout.output_dir / "book.m4b"
    m4b.write_bytes(b"FAKE-M4B")

    view = FakeProjectView()
    result = StageResult(stage=StageName.ASSEMBLE, status=ReviewStatus.COMPLETED)
    executor = FakeRunExecutor(result=result)
    presenter = _presenter(view, executor, _deps(tmp_path))
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    assert executor.started
    assert view.running == [True, False]  # running toggled on then off
    assert len(view.outcomes) == 1
    outcome = view.outcomes[0]
    assert outcome.kind == RunOutcomeKind.COMPLETED
    assert outcome.output_path == m4b


def test_start_run_needs_review_shows_readonly_blockers(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    result = StageResult(stage=StageName.REVIEW, status=ReviewStatus.NEEDS_REVIEW)
    executor = FakeRunExecutor(result=result)
    presenter = _presenter(view, executor, _deps(tmp_path))
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    outcome = view.outcomes[0]
    assert outcome.kind == RunOutcomeKind.NEEDS_REVIEW
    assert outcome.blockers is not None
    assert outcome.blockers.needs_attribution
    assert outcome.blockers.pending_suggestions
    assert outcome.blockers.unassigned_voices == ["Bob"]
    # read-only: the run did not mutate the persisted project (still one needs-review segment)
    reloaded = tmp_workspace.load()
    needs_review = [
        seg
        for ch in reloaded.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    ]
    assert len(needs_review) == 1


def test_start_run_stopped_is_resumable(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    result = StageResult(stage=StageName.SYNTHESIZE, status=ReviewStatus.STOPPED)
    presenter = _presenter(view, FakeRunExecutor(result=result), _deps(tmp_path))
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    assert view.outcomes[0].kind == RunOutcomeKind.STOPPED
    assert view.running == [True, False]


def test_start_run_failed_shows_message(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    result = StageResult(
        stage=StageName.SYNTHESIZE, status=ReviewStatus.FAILED, message="TTS unavailable"
    )
    presenter = _presenter(view, FakeRunExecutor(result=result), _deps(tmp_path))
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    outcome = view.outcomes[0]
    assert outcome.kind == RunOutcomeKind.FAILED
    assert outcome.message == "TTS unavailable"


def test_worker_failed_signal_shows_error(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    """An executor ``on_failed`` (worker raised) surfaces as an error, not an outcome."""
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(failure="boom"), _deps(tmp_path))
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    assert view.outcomes == []
    assert view.errors and view.errors[-1][1] == "boom"
    assert view.running == [True, False]


# --------------------------------------------------------------------------- #
# progress forwarding
# --------------------------------------------------------------------------- #
def test_progress_events_are_forwarded_and_accumulated(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    events: tuple[ProgressEvent, ...] = (
        ("total", 5),
        ("advance", (1, "one")),
        ("advance", (2, "three")),
        ("message", "note"),
    )
    result = StageResult(stage=StageName.ASSEMBLE, status=ReviewStatus.COMPLETED)
    presenter = _presenter(
        view, FakeRunExecutor(result=result, progress_events=events), _deps(tmp_path)
    )
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    # done accumulates across advances; total is remembered; messages ride along.
    assert (0, 5, "") in view.progress  # on_total reset done to 0 against the new total
    assert (1, 5, "one") in view.progress
    assert (3, 5, "three") in view.progress
    assert view.progress[-1] == (3, 5, "note")  # message keeps the accumulated done/total


# --------------------------------------------------------------------------- #
# provider preflight / stop
# --------------------------------------------------------------------------- #
def test_provider_unavailable_shows_error_and_does_not_start_worker(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    executor = FakeRunExecutor(
        result=StageResult(stage=StageName.PARSE, status=ReviewStatus.COMPLETED)
    )
    deps = _deps(tmp_path, llm=FakeLLMProvider(available=False))  # no key; attribute pending
    presenter = _presenter(view, executor, deps)
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    assert not executor.started  # worker never started
    assert view.running == []  # never toggled running
    assert view.errors and "ANTHROPIC_API_KEY" in view.errors[-1][1]


def test_start_run_without_project_shows_error(tmp_path: Path) -> None:
    view = FakeProjectView()
    executor = FakeRunExecutor()
    presenter = _presenter(view, executor, _deps(tmp_path))

    presenter.start_run()

    assert not executor.started
    assert view.errors


def test_stop_run_requests_stop(tmp_path: Path) -> None:
    executor = FakeRunExecutor()
    presenter = _presenter(FakeProjectView(), executor, _deps(tmp_path))

    presenter.stop_run()

    assert executor.stop_requested


# --------------------------------------------------------------------------- #
# attribution-review cross-wiring (slice 2)
# --------------------------------------------------------------------------- #
def _flagged_segment(project: Project) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    )


def test_open_fires_on_project_loaded_hook(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(), _deps(tmp_path))
    handed: list[WorkspaceStore] = []
    presenter.on_project_loaded = handed.append

    presenter.open(tmp_workspace.layout.root)

    assert handed == [presenter._store]  # the loaded store is handed to the review panel


def test_review_available_true_with_segments_false_without(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(), _deps(tmp_path))

    presenter.open(tmp_workspace.layout.root)  # review_ready_project carries segments

    assert view.review_available[-1] is True


def test_review_available_false_when_no_segments(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project, tmp_path: Path
) -> None:
    view = FakeProjectView()
    presenter = _presenter(view, FakeRunExecutor(), _deps(tmp_path))

    presenter.open(tmp_workspace.layout.root)  # parse_ready_project has no segments yet

    assert view.review_available[-1] is False


def test_refresh_after_review_rerenders_needs_review_summary_from_disk(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, tmp_path: Path
) -> None:
    """After an on_reviewed() tick, the shell re-reads project.json and drops the resolved flag."""
    view = FakeProjectView()
    result = StageResult(stage=StageName.REVIEW, status=ReviewStatus.NEEDS_REVIEW)
    presenter = _presenter(view, FakeRunExecutor(result=result), _deps(tmp_path))
    presenter.open(tmp_workspace.layout.root)
    presenter.start_run()  # sets _last_outcome_kind = NEEDS_REVIEW (one flagged segment on disk)
    assert view.outcomes[-1].blockers.needs_attribution  # the pre-edit summary has the blocker

    # Simulate the AttributionPresenter's edit: approve the flagged segment through ReviewService.
    edited = tmp_workspace.load()
    service = ReviewService(tmp_workspace, edited)
    service.approve_attribution(_flagged_segment(edited))

    presenter.refresh_after_review()  # the on_reviewed callback the app wires

    latest = view.outcomes[-1]
    assert latest.kind == RunOutcomeKind.NEEDS_REVIEW  # still blocked by the suggestion/voice
    assert latest.blockers.needs_attribution == []  # attribution count decremented, from disk


def test_structural_protocol_conformance() -> None:
    """The fakes satisfy the presenter's runtime-checkable protocols."""
    assert isinstance(FakeProjectView(), ProjectView)
    assert isinstance(FakeRunExecutor(), RunExecutor)

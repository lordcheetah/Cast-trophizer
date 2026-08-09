"""Offscreen Qt smoke tests — a handful, under ``QT_QPA_PLATFORM=offscreen``.

These confirm the real widgets wire up and update on presenter pushes, and that a real
:class:`QtRunExecutor` run tears its thread down cleanly. The behavioral coverage lives in the
loop-free ``test_presenter.py``; these only prove the Qt seams.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from shiboken6 import Shiboken

from casttrophizer.app_service import AppServiceDeps, build_pipeline, build_providers
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import StageResult
from casttrophizer.ui.main_window import MainWindow
from casttrophizer.ui.presenter import ProjectPresenter, ProjectView
from casttrophizer.ui.run_executor import QtRunExecutor
from casttrophizer.ui.workers import PipelineWorker
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeLLMProvider, FakeM4BAssembler, FakeTTSProvider
from tests.ui.test_presenter import FakeRunExecutor

pytestmark = pytest.mark.usefixtures("qapp")


def _deps() -> AppServiceDeps:
    return AppServiceDeps(
        llm_factory=lambda _cfg: FakeLLMProvider(),
        tts_factory=lambda _cfg: FakeTTSProvider(),
        assembler=FakeM4BAssembler(),
        config=AppConfig(),
    )


def test_main_window_satisfies_project_view_protocol() -> None:
    window = MainWindow()
    assert isinstance(window, ProjectView)


def test_open_updates_stage_list_and_next_label(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    window = MainWindow()
    presenter = ProjectPresenter(view=window, executor=FakeRunExecutor(), deps=_deps())

    presenter.open(tmp_workspace.layout.root)

    assert window._stage_list.count() == 6
    assert "review" in window._next_label.text()
    assert "Review Ready" in window._header.text()


def test_run_updates_progress_bar_and_outcome_panel(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    window = MainWindow()
    events = (("total", 4), ("advance", (2, "halfway")))
    result = StageResult(stage=StageName.REVIEW, status=ReviewStatus.NEEDS_REVIEW)
    executor = FakeRunExecutor(result=result, progress_events=events)
    presenter = ProjectPresenter(view=window, executor=executor, deps=_deps())
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    # A halted (NEEDS_REVIEW) run must NOT show a full "complete-looking" bar: the bar keeps
    # its mid-run value (2 of 4), it is not forced to 100%.
    assert window._progress.maximum() == 4
    assert window._progress.value() == 2
    assert "Needs review" in window._outcome.text()
    assert "halfway" in window._log.toPlainText()
    assert window._run_btn.isEnabled()  # running toggled back off on terminal
    assert not window._stop_btn.isEnabled()


def test_completed_outcome_fills_progress_bar(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """A genuine COMPLETED outcome fills the bar to 100% (the honest signal)."""
    window = MainWindow()
    result = StageResult(stage=StageName.ASSEMBLE, status=ReviewStatus.COMPLETED)
    presenter = ProjectPresenter(view=window, executor=FakeRunExecutor(result=result), deps=_deps())
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    assert window._progress.maximum() == 1
    assert window._progress.value() == 1
    assert "Complete" in window._outcome.text()


def _run_once_threaded(
    executor: QtRunExecutor,
    store: WorkspaceStore,
    qapp: object,
) -> tuple[list[StageResult], PipelineWorker]:
    """Start one real threaded run, return (terminal results, the PipelineWorker instance).

    Pumps the main event loop until the terminal callback fires. The returned worker lets the
    caller assert it was actually destroyed (its C++ object invalidated) after teardown.
    """
    deps = _deps()
    pipeline = build_pipeline(deps)
    # attribute is already COMPLETED in the fixture, so no LLM preflight is needed.
    llm, tts = build_providers(deps, AppConfig(), provider=None, preflight=False)

    finished: list[StageResult] = []
    executor.start(
        pipeline,
        store,
        llm,
        tts,
        AppConfig(),
        on_total=lambda _t: None,
        on_advance=lambda _d, _m: None,
        on_message=lambda _m: None,
        on_finished=finished.append,
        on_failed=lambda msg: finished.append(
            StageResult(StageName.REVIEW, ReviewStatus.FAILED, msg)
        ),
    )
    worker = executor._worker
    assert worker is not None

    deadline = time.monotonic() + 10.0
    while not finished and time.monotonic() < deadline:
        qapp.processEvents()  # type: ignore[attr-defined]
        time.sleep(0.005)
    # Pump a little more so the DeferredDelete events posted during teardown are serviced.
    for _ in range(20):
        qapp.processEvents()  # type: ignore[attr-defined]
    return finished, worker


def test_qt_run_executor_completes_and_destroys_worker(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, qapp: object
) -> None:
    """A real threaded run halts NEEDS_REVIEW, joins the thread, AND destroys the worker.

    Guards the lifecycle leak: the old teardown order (``quit()``/``wait()`` then an imperative
    ``worker.deleteLater()`` posted to the already-dead worker-thread loop) left the worker —
    and the StageContext it pins (store + LLM + TTS + config) — alive. This asserts the worker's
    C++ object is actually invalidated, so it fails against that order and passes with the
    ``thread.finished -> worker.deleteLater`` wiring.
    """
    executor = QtRunExecutor()
    finished, worker = _run_once_threaded(executor, tmp_workspace, qapp)

    assert finished, "the worker never delivered a terminal result"
    assert finished[0].status == ReviewStatus.NEEDS_REVIEW
    assert executor._thread is None  # teardown quit + joined the thread
    assert not Shiboken.isValid(worker)  # the worker was actually destroyed (no leak)


def test_qt_run_executor_is_reusable_across_runs(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, qapp: object
) -> None:
    """Resume = a second run on the same executor: the prior worker is gone, a fresh one runs."""
    executor = QtRunExecutor()
    _, first_worker = _run_once_threaded(executor, tmp_workspace, qapp)
    assert not Shiboken.isValid(first_worker)  # first run's worker destroyed before the second

    finished, second_worker = _run_once_threaded(executor, tmp_workspace, qapp)
    assert finished and finished[0].status == ReviewStatus.NEEDS_REVIEW
    assert second_worker is not first_worker
    assert not Shiboken.isValid(second_worker)
    assert executor._thread is None


def test_qt_run_executor_rejects_concurrent_start(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, qapp: object
) -> None:
    """A second ``start()`` while a run is in flight raises rather than orphaning the thread."""
    deps = _deps()
    pipeline = build_pipeline(deps)
    llm, tts = build_providers(deps, AppConfig(), provider=None, preflight=False)
    executor = QtRunExecutor()

    finished: list[StageResult] = []
    noop_total: Callable[[int], None] = lambda _t: None  # noqa: E731
    noop_adv: Callable[[int, str], None] = lambda _d, _m: None  # noqa: E731
    noop_msg: Callable[[str], None] = lambda _m: None  # noqa: E731
    noop_fail: Callable[[str], None] = lambda _m: None  # noqa: E731

    executor.start(
        pipeline,
        tmp_workspace,
        llm,
        tts,
        AppConfig(),
        on_total=noop_total,
        on_advance=noop_adv,
        on_message=noop_msg,
        on_finished=finished.append,
        on_failed=noop_fail,
    )
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            executor.start(
                pipeline,
                tmp_workspace,
                llm,
                tts,
                AppConfig(),
                on_total=noop_total,
                on_advance=noop_adv,
                on_message=noop_msg,
                on_finished=finished.append,
                on_failed=noop_fail,
            )
    finally:
        deadline = time.monotonic() + 10.0
        while not finished and time.monotonic() < deadline:
            qapp.processEvents()  # type: ignore[attr-defined]
            time.sleep(0.005)


def test_new_and_run_buttons_disable_while_running(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """set_running(True) disables Open/New/Run and enables Stop (checked mid-flight)."""
    window = MainWindow()

    captured: dict[str, bool] = {}

    class _MidFlightExecutor(FakeRunExecutor):
        def start(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
            # snapshot button state at the moment the worker is (would be) started
            captured["run"] = window._run_btn.isEnabled()
            captured["stop"] = window._stop_btn.isEnabled()
            captured["open"] = window._open_btn.isEnabled()

    presenter = ProjectPresenter(view=window, executor=_MidFlightExecutor(), deps=_deps())
    presenter.open(tmp_workspace.layout.root)

    presenter.start_run()

    assert captured == {"run": False, "stop": True, "open": False}


def test_review_button_disabled_while_running_and_reenabled_after(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    """The review entry point is gated on ``not running`` (guards the concurrent-write race)."""
    window = MainWindow()

    captured: dict[str, bool] = {}

    class _CaptureExecutor(FakeRunExecutor):
        def start(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
            captured["review_mid"] = window._review_btn.isEnabled()  # after set_running(True)
            super().start(*args, **kwargs)  # deliver the result -> on_finished re-enables

    result = StageResult(stage=StageName.REVIEW, status=ReviewStatus.NEEDS_REVIEW)
    presenter = ProjectPresenter(
        view=window, executor=_CaptureExecutor(result=result), deps=_deps()
    )
    presenter.open(tmp_workspace.layout.root)
    assert window._review_btn.isEnabled()  # enabled once the project has segments

    presenter.start_run()

    assert captured["review_mid"] is False  # disabled for the duration of the run
    assert window._review_btn.isEnabled()  # re-enabled after finish (segments still present)


def test_new_project_flow_via_presenter(tmp_path: Path, sample_epub: Path) -> None:
    window = MainWindow()
    deps = AppServiceDeps(
        llm_factory=lambda _cfg: FakeLLMProvider(),
        tts_factory=lambda _cfg: FakeTTSProvider(),
        assembler=FakeM4BAssembler(),
        config=AppConfig(workspaces_root=tmp_path),
    )
    presenter = ProjectPresenter(view=window, executor=FakeRunExecutor(), deps=deps)

    presenter.create(sample_epub, "Smoke Book")

    assert "Smoke Book" in window._header.text()
    assert "parse" in window._next_label.text()

"""The Qt :class:`RunExecutor`: owns a fresh QThread + worker + reporter per run.

Implements the presenter's :class:`~casttrophizer.ui.presenter.RunExecutor` protocol. A new
``QThread`` / :class:`~casttrophizer.ui.workers.PipelineWorker` /
:class:`~casttrophizer.ui.workers.QtProgressReporter` is built for **every** run — QThreads are
not cleanly restartable, so a fresh one per Run/Resume is the simplest correct choice.

Thread affinity: this executor is a ``QObject`` created on the main thread, so the worker's and
reporter's signals reach its slots via a **queued** connection — every forwarded callback
(progress, finished, failed) runs on the main thread. The presenter it forwards to therefore
never touches Qt off-thread. (Forwarding to plain Python callables directly would default to a
*direct* connection and run the callback on the worker thread — hence the QObject indirection.)
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Slot

from casttrophizer.config import AppConfig
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext, StageResult
from casttrophizer.providers import LLMProvider, TTSProvider
from casttrophizer.ui.workers import PipelineWorker, QtProgressReporter
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["QtRunExecutor"]


class QtRunExecutor(QObject):
    """Drives one pipeline pass on a background thread, forwarding results on the main thread."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: PipelineWorker | None = None
        self._reporter: QtProgressReporter | None = None
        # Callbacks for the in-flight run (only one run at a time).
        self._on_total: Callable[[int], None] | None = None
        self._on_advance: Callable[[int, str], None] | None = None
        self._on_message: Callable[[str], None] | None = None
        self._on_finished: Callable[[StageResult], None] | None = None
        self._on_failed: Callable[[str], None] | None = None

    # -- RunExecutor protocol ----------------------------------------------- #
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
        """Build the reporter + context and start the worker thread.

        Providers and config are built on the main thread by the presenter and handed in; the
        :class:`StageContext` (carrying a fresh :class:`QtProgressReporter`) is assembled here
        and the pipeline runs on the new thread. All forwarded callbacks fire on the main thread.

        Raises :class:`RuntimeError` if a run is already in flight — this executor runs exactly
        one pass at a time (a second start would orphan the live thread).
        """
        if self._thread is not None:
            raise RuntimeError("a run is already in progress")

        self._on_total = on_total
        self._on_advance = on_advance
        self._on_message = on_message
        self._on_finished = on_finished
        self._on_failed = on_failed

        reporter = QtProgressReporter()
        ctx = StageContext(store=store, progress=reporter, llm=llm, tts=tts, config=config)
        thread = QThread()
        worker = PipelineWorker(pipeline, ctx)
        worker.moveToThread(thread)

        # Progress signals (emitted from the worker thread) -> main-thread forwarders.
        reporter.total_changed.connect(self._forward_total)
        reporter.advanced.connect(self._forward_advance)
        reporter.message_emitted.connect(self._forward_message)

        thread.started.connect(worker.run)
        worker.finished.connect(self._forward_finished)
        worker.failed.connect(self._forward_failed)

        # Canonical teardown: the worker's affinity is the worker thread, so its DeferredDelete
        # is only serviced by that thread. Wiring deletion to ``thread.finished`` (before start)
        # means the worker is destroyed as the thread's loop exits — an imperative
        # ``worker.deleteLater()`` after ``quit()``/``wait()`` would post to a dead loop and
        # leak the worker (which pins its StageContext: store + LLM + TTS + config).
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        self._reporter = reporter
        thread.start()

    def request_stop(self) -> None:
        """Ask the running stage to stop at its next checkpoint (thread-safe, no-op if idle)."""
        if self._reporter is not None:
            self._reporter.request_stop()

    # -- forwarders (run on the main thread via queued connections) --------- #
    @Slot(int)
    def _forward_total(self, total: int) -> None:
        if self._on_total is not None:
            self._on_total(total)

    @Slot(int, str)
    def _forward_advance(self, delta: int, message: str) -> None:
        if self._on_advance is not None:
            self._on_advance(delta, message)

    @Slot(str)
    def _forward_message(self, message: str) -> None:
        if self._on_message is not None:
            self._on_message(message)

    @Slot(object)
    def _forward_finished(self, result: StageResult) -> None:
        callback = self._on_finished
        self._teardown()
        if callback is not None:
            callback(result)

    @Slot(str)
    def _forward_failed(self, message: str) -> None:
        callback = self._on_failed
        self._teardown()
        if callback is not None:
            callback(message)

    # -- lifecycle ---------------------------------------------------------- #
    def _teardown(self) -> None:
        """Quit + join the thread; the worker/thread self-destruct via ``thread.finished``.

        Quitting the loop makes ``thread.finished`` fire (destroying the worker as the thread
        stops, then the thread object) and ``wait()`` joins it. The reporter has main-thread
        affinity, so its ``deleteLater`` is serviced by the live main loop and stays imperative.
        State is cleared to ``None`` so the next :meth:`start` sees a clean slate.
        """
        thread, reporter = self._thread, self._reporter
        self._thread = self._worker = self._reporter = None
        if thread is not None:
            thread.quit()
            thread.wait()
        if reporter is not None:
            reporter.deleteLater()

"""Off-main-thread pipeline execution and Qt-bridged progress reporting.

Long-running work (parse / attribute / synthesize) must not block the Qt main thread.
:class:`PipelineWorker` is a ``QObject`` moved onto a ``QThread``; it runs the
:class:`~casttrophizer.pipeline.runner.Pipeline` and emits Qt signals for results.

:class:`QtProgressReporter` implements the framework-agnostic
:class:`~casttrophizer.pipeline.progress.ProgressReporter` protocol by emitting Qt
signals (so the main thread can update widgets) and exposing a thread-safe
``request_stop`` that the running stage observes via ``should_stop``.

Qt is confined to this module (and the rest of ``ui/``). The pipeline and stages remain
Qt-free; they only ever see the ``ProgressReporter`` protocol.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext, StageResult

__all__ = ["QtProgressReporter", "PipelineWorker"]


class QtProgressReporter(QObject):
    """A :class:`ProgressReporter` that emits Qt signals and supports cooperative stop.

    Satisfies the ``ProgressReporter`` protocol structurally. ``request_stop`` is
    thread-safe (a :class:`threading.Event`), so the UI thread can ask a stage running
    on the worker thread to stop at its next checkpoint.
    """

    total_changed = Signal(int)
    advanced = Signal(int, str)  # (delta, message)
    message_emitted = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stop = threading.Event()

    # -- ProgressReporter protocol ----------------------------------------- #
    def set_total(self, total: int) -> None:
        self.total_changed.emit(total)

    def advance(self, n: int = 1, *, message: str | None = None) -> None:
        self.advanced.emit(n, message or "")

    def message(self, text: str) -> None:
        self.message_emitted.emit(text)

    def should_stop(self) -> bool:
        return self._stop.is_set()

    # -- control ------------------------------------------------------------ #
    def request_stop(self) -> None:
        """Ask the running stage to stop at its next safe checkpoint (thread-safe)."""
        self._stop.set()

    def reset(self) -> None:
        """Clear a previous stop request so the reporter can be reused."""
        self._stop.clear()


class PipelineWorker(QObject):
    """Runs a :class:`Pipeline` off the main thread, emitting results as Qt signals.

    Intended usage: construct, ``moveToThread(thread)``, connect ``thread.started`` to
    :meth:`run`, and connect :attr:`finished` / :attr:`failed` for results. The injected
    ``ctx`` should carry a :class:`QtProgressReporter` as its ``progress``.
    """

    finished = Signal(object)  # emits a StageResult
    failed = Signal(str)  # emits an error message

    def __init__(
        self,
        pipeline: Pipeline,
        ctx: StageContext,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pipeline = pipeline
        self._ctx = ctx

    def run(self) -> None:
        """Run the pipeline; emit :attr:`finished` with the result or :attr:`failed`."""
        try:
            result: StageResult = self._pipeline.run(self._ctx)
        except Exception as exc:  # noqa: BLE001 - report any failure to the UI thread
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)

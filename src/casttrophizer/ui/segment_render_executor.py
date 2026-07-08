"""The Qt :class:`QtSegmentRenderExecutor`: one single-shot QThread per regenerate.

Implements the presenter's
:class:`~casttrophizer.ui.audio_presenter.SegmentRenderExecutor` protocol. A fresh ``QThread`` +
:class:`SegmentRenderWorker` is built for **every** regenerate — QThreads are not cleanly
restartable, so a fresh one per render is the simplest correct choice (mirroring
:class:`~casttrophizer.ui.run_executor.QtRunExecutor`).

Thread affinity: this executor is a ``QObject`` created on the main thread, so the worker's signals
reach its slots via a **queued** connection — both forwarded callbacks (finished, failed) run on the
main thread. The presenter it forwards to therefore never touches Qt off-thread, and the heavy
Chatterbox model load happens on the worker thread inside ``render_segment`` -> ``tts.synthesize``.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from casttrophizer.audio.loudness import LoudnessSettings
from casttrophizer.audio.synthesize import render_segment
from casttrophizer.domain.models import Project, Segment
from casttrophizer.errors import TTSProviderError
from casttrophizer.providers import TTSProvider
from casttrophizer.workspace.audio_cache import AudioCache

__all__ = ["SegmentRenderWorker", "QtSegmentRenderExecutor"]


class SegmentRenderWorker(QObject):
    """Renders ONE segment off the main thread, emitting :attr:`finished` / :attr:`failed`.

    Intended usage: construct, ``moveToThread(thread)``, connect ``thread.started`` to :meth:`run`,
    and connect :attr:`finished` / :attr:`failed`. Calls
    :func:`~casttrophizer.audio.synthesize.render_segment`, which stamps the shared :class:`Segment`
    COMPLETED in memory on success (the queued ``finished`` signal is the happens-before before the
    main thread persists + reads it).
    """

    finished = Signal()
    # (message, provider_unavailable): ``provider_unavailable`` is True only for a
    # ``TTSProviderError`` (missing clip / model-load / provider failure) so the presenter can
    # PERMANENTLY disable Regenerate; a transient render error (e.g. CUDA OOM on one long segment)
    # sends False so the user can retry that one segment.
    failed = Signal(str, bool)

    def __init__(
        self,
        tts: TTSProvider,
        project: Project,
        segment: Segment,
        cache: AudioCache,
        loudness: LoudnessSettings | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._tts = tts
        self._project = project
        self._segment = segment
        self._cache = cache
        self._loudness = loudness

    @Slot()
    def run(self) -> None:
        """Render the segment; emit :attr:`finished` on success or :attr:`failed` on any error.

        A :class:`TTSProviderError` (missing voice clip, model-load, or provider failure) signals
        ``provider_unavailable=True`` so the UI can disable Regenerate; any other exception (a
        transient render error) signals ``False`` so the user can retry that one segment. Either way
        ``render_segment`` never partially stamps, so the segment is left for the caller to mark
        FAILED — the thread never crashes.
        """
        try:
            render_segment(
                self._project, self._segment, self._cache, self._tts, loudness=self._loudness
            )
        except TTSProviderError as exc:
            self.failed.emit(str(exc), True)  # provider/model unavailable
            return
        except Exception as exc:  # noqa: BLE001 - report any failure to the UI thread
            self.failed.emit(str(exc), False)  # transient render error -> retryable
            return
        self.finished.emit()


class QtSegmentRenderExecutor(QObject):
    """Renders one segment on a background thread, forwarding both callbacks on the main thread."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: SegmentRenderWorker | None = None
        # Callbacks for the in-flight render (only one at a time).
        self._on_finished: Callable[[], None] | None = None
        self._on_failed: Callable[[str, bool], None] | None = None

    # -- SegmentRenderExecutor protocol ------------------------------------- #
    def start(
        self,
        tts: TTSProvider,
        project: Project,
        segment: Segment,
        cache: AudioCache,
        *,
        loudness: LoudnessSettings | None,
        on_finished: Callable[[], None],
        on_failed: Callable[[str, bool], None],
    ) -> None:
        """Build the worker + thread and start the single-segment render.

        Raises :class:`RuntimeError` if a render is already in flight — this executor runs exactly
        one render at a time (belt-and-braces with the presenter's ``_rendering`` guard); a second
        start would orphan the live thread.
        """
        if self._thread is not None:
            raise RuntimeError("a segment render is already in progress")

        self._on_finished = on_finished
        self._on_failed = on_failed

        thread = QThread()
        worker = SegmentRenderWorker(tts, project, segment, cache, loudness)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._forward_finished)
        worker.failed.connect(self._forward_failed)

        # Canonical teardown (slice-1's fix): the worker's affinity is the worker thread, so its
        # DeferredDelete is only serviced by that thread. Wiring deletion to ``thread.finished``
        # (before start) destroys the worker as the loop exits — an imperative
        # ``worker.deleteLater`` after ``quit()``/``wait()`` would post to a dead loop and leak the
        # worker (which pins the heavy TTS provider/model).
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        thread.start()

    # -- forwarders (run on the main thread via queued connections) --------- #
    @Slot()
    def _forward_finished(self) -> None:
        callback = self._on_finished
        self._teardown()
        if callback is not None:
            callback()

    @Slot(str, bool)
    def _forward_failed(self, message: str, provider_unavailable: bool) -> None:
        callback = self._on_failed
        self._teardown()
        if callback is not None:
            callback(message, provider_unavailable)

    # -- lifecycle ---------------------------------------------------------- #
    def _teardown(self) -> None:
        """Quit + join the thread; the worker/thread self-destruct via ``thread.finished``.

        Quitting the loop fires ``thread.finished`` (destroying the worker as the thread stops, then
        the thread object) and ``wait()`` joins it. State is cleared so the next :meth:`start` sees
        a clean slate — a fresh thread/worker per regenerate.
        """
        thread = self._thread
        self._thread = self._worker = None
        self._on_finished = self._on_failed = None
        if thread is not None:
            thread.quit()
            thread.wait()

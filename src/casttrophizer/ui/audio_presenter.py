"""The Qt-free presenter for the per-segment audio-review panel and its two protocols.

Mirroring :class:`~casttrophizer.ui.voice_presenter.VoicePresenter`, this presenter imports **no
PySide6** (and **no** ``QtMultimedia``): it drives the widget through the :class:`AudioView`
protocol, funnels every mutation through the single shared
:class:`~casttrophizer.review.service.ReviewService`, and runs the single-segment *regenerate*
render on a worker thread behind the injected :class:`SegmentRenderExecutor` protocol — exactly how
:class:`~casttrophizer.ui.presenter.ProjectPresenter` injects its :class:`RunExecutor` (slice 1).
The Qt panel (``ui/audio_panel.py``) and the Qt executor (``ui/segment_render_executor.py``) are
the only new modules that touch Qt.

Two boundaries live here as *decisions* (so both are unit-testable without a device/thread):

* **Audition** — the presenter decides *what* to play (and whether it is playable — the missing-WAV
  guard) and asks the view via :meth:`AudioView.play_clip`; it never touches audio itself.
* **Regenerate-now** — the presenter owns the whole flow (re-roll -> render on the executor ->
  commit + auto-play), the single-flight ``_rendering`` guard, the lazy TTS-provider build, and the
  graceful degrade when the ``tts`` extra is absent or the model fails to load — but it never owns a
  thread; the injected executor does, and its callbacks land on the **main thread**.

Approval and re-roll are POST-synthesize curation, so they never re-open the pre-synth review gate
(they go through the audio-review ``ReviewService`` methods, which document why). Every mutating
intent fires the one-way ``on_reviewed`` shell hook so the status shell can tick live.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from casttrophizer.app_service import AppServiceDeps, tts_extra_available
from casttrophizer.audio.loudness import LoudnessSettings
from casttrophizer.domain.ids import SegmentId
from casttrophizer.domain.models import Project, Segment
from casttrophizer.errors import CasttrophizerError
from casttrophizer.providers import TTSProvider
from casttrophizer.review.audio_view import (
    AudioSegmentRow,
    approved_count,
    audio_segment_rows,
    rendered_count,
)
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.audio_cache import AudioCache

__all__ = ["AudioView", "SegmentRenderExecutor", "AudioPresenter"]


@runtime_checkable
class AudioView(Protocol):
    """The render-only surface the presenter pushes at — a dumb, logic-free view.

    The Qt panel implements this structurally; tests substitute an in-memory fake. ``play_clip`` /
    ``stop_playback`` are the audition boundary; ``set_busy`` / ``set_regenerate_available`` gate
    the buttons the presenter's single-flight + graceful-degrade rules require.
    """

    def show_segments(self, rows: list[AudioSegmentRow]) -> None:
        """Render the (currently filtered) audio-segment rows."""
        ...

    def show_progress(self, approved: int, rendered: int) -> None:
        """Show ``approved`` of ``rendered`` segments signed off (the "N of M approved" line)."""
        ...

    def select_segment(self, index: int) -> None:
        """Scroll to + highlight the row at ``index`` in the displayed list."""
        ...

    def play_clip(self, path: str) -> None:
        """Audition ``path`` — the panel plays it (the presenter decided it is playable)."""
        ...

    def stop_playback(self) -> None:
        """Stop + release the panel's player (frees the Windows file handle before a render)."""
        ...

    def set_regenerate_available(self, available: bool) -> None:
        """Enable/disable the Regenerate button (false when the ``tts`` extra is absent)."""
        ...

    def set_busy(self, busy: bool) -> None:
        """Toggle the mid-render busy state (disables Approve/Regenerate/Back during a render)."""
        ...

    def show_error(self, title: str, message: str) -> None:
        """Surface a recoverable error / info (no WAV to play, provider absent, render fail)."""
        ...


@runtime_checkable
class SegmentRenderExecutor(Protocol):
    """Runs :func:`~casttrophizer.audio.synthesize.render_segment` off the main thread.

    :meth:`start` renders exactly one segment on a worker thread and delivers **both** callbacks on
    the **main thread** (queued connections in the Qt implementation), so the presenter never
    touches a thread and never races the worker.
    """

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
        """Render ``segment`` now; ``on_finished`` on success, ``on_failed(msg, unavailable)``.

        ``on_failed``'s ``provider_unavailable`` flag is True only for a provider/model failure
        (:class:`~casttrophizer.errors.TTSProviderError`) so the presenter can distinguish a
        permanent "disable Regenerate" degrade from a transient, retryable render error.
        """
        ...


class AudioPresenter:
    """Drives per-segment audio review for one project through the shared :class:`ReviewService`.

    Holds the shared service + the workspace :class:`AudioCache` (both adopted on :meth:`attach`),
    the unapproved-only filter (default ON), the current selection index into the *displayed*
    (filtered) rows, and the lazily-built TTS provider used for regenerate. The ``_rendering`` flag
    is the presenter half of the single-flight guard (the executor holds the other half). Contains
    no Qt and no threading primitives.
    """

    def __init__(
        self,
        view: AudioView,
        executor: SegmentRenderExecutor,
        deps: AppServiceDeps,
        *,
        on_reviewed: Callable[[], None],
    ) -> None:
        self._view = view
        self._executor = executor
        self._deps = deps
        self._on_reviewed = on_reviewed
        self._service: ReviewService | None = None
        self._cache: AudioCache | None = None
        self._tts: TTSProvider | None = None
        self._unapproved_only = True
        self._rendering = False
        self._rows: list[AudioSegmentRow] = []  # the currently displayed (filtered) rows
        self._selected = -1  # index into ``self._rows``; -1 == nothing selected

    # -- lifecycle ---------------------------------------------------------- #
    def attach(self, service: ReviewService, cache: AudioCache) -> None:
        """Adopt the shared service + workspace cache (by reference) and reset the view state.

        Called whenever a project is (re)loaded / a run finishes (via
        ``ProjectPresenter.on_project_loaded``), so the panel never edits stale state. Stops any
        lingering playback (project switch), drops the previous provider (a new project may target a
        different config), and re-probes :func:`tts_extra_available` to gate Regenerate — Play and
        Approve never need a provider, so they stay available even when the ``tts`` extra is absent.
        Does not render until :meth:`open`.
        """
        self._service = service
        self._cache = cache
        self._tts = None
        self._unapproved_only = True
        self._selected = -1
        self._rows = []
        self._rendering = False
        self._view.stop_playback()
        self._view.set_regenerate_available(tts_extra_available())

    def open(self) -> None:
        """(Re)derive rows + progress and push them to the view."""
        if self._service is None or self._cache is None:
            return
        self._render()

    # -- mutating intents --------------------------------------------------- #
    def approve(self, segment_id: SegmentId) -> None:
        """Approve a rendered take (COMPLETED -> APPROVED); a curation marker, no re-render."""
        self._apply(segment_id, lambda seg, svc: svc.approve_audio(seg))

    def reject(self, segment_id: SegmentId) -> None:
        """Reject a take: re-roll only (new seed, PENDING) and DEFER the re-render to the next Run.

        The same first step as :meth:`regenerate` minus the immediate render — for when the user
        wants a fresh take but not to wait now (or when Regenerate is unavailable). The segment
        re-renders on the next full run (the render stages are re-opened by ``reroll_audio``).
        """
        self._apply(segment_id, lambda seg, svc: svc.reroll_audio(seg))

    def regenerate(self, segment_id: SegmentId) -> None:
        """Re-roll then render THIS segment now on the worker thread, auto-playing the fresh take.

        The full regenerate-now flow (all decisions in one place): single-flight guard -> re-roll +
        persist (same as :meth:`reject`) -> if the ``tts`` extra is absent, degrade to a deferred
        re-render -> else build the provider lazily, mark busy, release the WAV handle, and start
        the injected executor. On the executor's main-thread ``on_finished`` the segment is already
        stamped COMPLETED in memory, so it commits + auto-plays; on ``on_failed`` it degrades (the
        segment stays re-rolled/PENDING for the next Run). Never renders on the main thread.
        """
        if self._service is None or self._cache is None:
            return
        if self._rendering:
            return  # single-flight: ignore a second regenerate while one is in flight
        segment = self._find_segment(segment_id)
        if segment is None:
            self._view.show_error("Regenerate failed", f"no segment {segment_id!r} in project")
            return

        # Step 1 (shared with reject): re-roll -> new seed, cleared key, PENDING, render stages
        # re-opened, persisted. From here the segment WILL re-render (now, or on the next Run).
        self._service.reroll_audio(segment)

        # Step 2: graceful degrade when Chatterbox is absent — the segment is already deferred, so
        # just tell the user it will re-render on the next Run and refresh; no worker, no crash.
        if not tts_extra_available():
            self._view.set_regenerate_available(False)
            self._view.show_error(
                "Chatterbox unavailable",
                "Chatterbox is not installed — this segment will re-render on the next full Run.",
            )
            self._after_review()
            return

        tts = self._ensure_tts()
        if tts is None:
            # Provider construction failed (e.g. a config error): degrade like the missing-extra
            # case — the segment stays re-rolled/PENDING for the next Run.
            self._view.set_regenerate_available(False)
            self._after_review()
            return

        # Step 3: hand off to the worker. Release the player's file handle FIRST (Windows blocks
        # the WAV overwrite while its source is open), mark busy so Approve/Regenerate/Back disable.
        self._rendering = True
        self._view.set_busy(True)
        self._view.stop_playback()
        loudness = LoudnessSettings.from_params(self._service.project.tts_params)
        try:
            self._executor.start(
                tts,
                self._service.project,
                segment,
                self._cache,
                loudness=loudness,
                on_finished=lambda: self._on_render_finished(segment_id),
                on_failed=lambda msg, unavailable: self._on_render_failed(
                    segment_id, msg, unavailable
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a start failure must not wedge the panel busy
            # The executor refused to start (e.g. its in-flight guard) — undo the busy state so the
            # panel isn't stuck disabled forever, mark the re-rolled segment FAILED (visible +
            # re-renders next Run), and surface the error. A start failure is not a provider outage,
            # so leave Regenerate enabled (retryable).
            self._on_render_failed(segment_id, str(exc), False)

    # -- executor callbacks (main thread) ----------------------------------- #
    def _on_render_finished(self, segment_id: SegmentId) -> None:
        """Worker stamped the segment COMPLETED in memory: persist, refresh, auto-play the take."""
        self._rendering = False
        self._view.set_busy(False)
        if self._service is None:
            return
        segment = self._find_segment(segment_id)
        if segment is not None:
            self._service.commit_rendered_audio(segment)
        self._after_review()
        self.play(segment_id)  # auto-play the fresh take

    def _on_render_failed(
        self, segment_id: SegmentId, message: str, provider_unavailable: bool
    ) -> None:
        """The worker (or executor start) failed: mark the segment FAILED, degrade, don't crash.

        The re-rolled segment is stamped FAILED (via :meth:`ReviewService.mark_audio_failed`) so its
        row stays visible with a badge instead of a PENDING row silently vanishing from the list;
        FAILED is outside ``RENDERED_STATUSES``, so the next full Run re-renders it. ``on_reviewed``
        fires (via :meth:`_after_review`) so the shell reflects the re-opened render stages. Only a
        **provider/model** failure (``provider_unavailable``) permanently disables Regenerate; a
        transient render error leaves it ENABLED so the user can retry that segment. Play/Approve
        are unaffected.
        """
        self._rendering = False
        self._view.set_busy(False)
        if provider_unavailable:
            self._view.set_regenerate_available(False)
        if self._service is not None and self._cache is not None:
            segment = self._find_segment(segment_id)
            if segment is not None:
                self._service.mark_audio_failed(segment)
            self._after_review()
        self._view.show_error("Regenerate failed", message)

    # -- audition intent (read-only; no persistence, no ``on_reviewed``) ----- #
    def play(self, segment_id: SegmentId) -> None:
        """Play a segment's rendered WAV; a non-rendered/missing-file segment -> :meth:`show_error`.

        Releases the current source (:meth:`~AudioView.stop_playback`) before playing so a replay
        of the same path doesn't hit a still-open handle on Windows.
        """
        if self._service is None or self._cache is None:
            return
        row = self._row_for(segment_id)
        if row is None or row.wav_path is None:
            self._view.show_error("Playback", "This segment has no rendered audio to play.")
            return
        self._view.stop_playback()
        self._view.play_clip(row.wav_path)

    # -- view state (no persistence) ---------------------------------------- #
    def set_filter(self, unapproved_only: bool) -> None:
        """Toggle the unapproved-only filter and re-push the (filtered) rows."""
        self._unapproved_only = unapproved_only
        self._selected = -1  # indices change under the filter; drop the stale selection
        self._render()

    def next_segment(self) -> None:
        """Advance the selection to the next displayed row (wrapping) and highlight it."""
        self._step(1)

    def prev_segment(self) -> None:
        """Move the selection to the previous displayed row (wrapping) and highlight it."""
        self._step(-1)

    def play_next(self) -> None:
        """Advance to the next displayed row and play it (the audition-walk shortcut)."""
        self._step(1)
        if 0 <= self._selected < len(self._rows):
            self.play(self._rows[self._selected].segment_id)

    def set_selected(self, index: int) -> None:
        """Record a selection the view made (a mouse click), without re-highlighting."""
        if 0 <= index < len(self._rows):
            self._selected = index

    # -- helpers ------------------------------------------------------------ #
    def _apply(
        self,
        segment_id: SegmentId,
        action: Callable[[Segment, ReviewService], object],
    ) -> None:
        """Look ``segment_id`` up, run ``action`` (a ReviewService call), then refresh + notify.

        A missing id surfaces via :meth:`AudioView.show_error` and leaves the state untouched — no
        crash, no notify. Ignored while a render is in flight (single-flight: nothing else mutates
        the busy segment).
        """
        if self._service is None or self._cache is None:
            return
        if self._rendering:
            return
        segment = self._find_segment(segment_id)
        if segment is None:
            self._view.show_error("Edit failed", f"no segment {segment_id!r} in project")
            return
        action(segment, self._service)
        self._after_review()

    def _after_review(self) -> None:
        """Re-derive + push rows/progress, re-highlight, then fire ``on_reviewed``."""
        self._render()
        self._on_reviewed()

    def _render(self) -> None:
        """Push the filtered rows + the whole-book approved/rendered progress, then re-highlight."""
        assert self._service is not None
        assert self._cache is not None
        all_rows = audio_segment_rows(self._service.project, self._cache)
        reviewable = [r for r in all_rows if r.is_rendered or r.is_failed]
        self._rows = (
            [r for r in reviewable if not r.is_approved] if self._unapproved_only else reviewable
        )
        self._view.show_segments(self._rows)
        self._view.show_progress(approved_count(all_rows), rendered_count(all_rows))
        self._reselect()

    def _reselect(self) -> None:
        """Re-highlight the clamped selection if the displayed list is non-empty."""
        if not self._rows:
            self._selected = -1
            return
        if self._selected < 0:
            return
        self._selected = min(self._selected, len(self._rows) - 1)
        self._view.select_segment(self._selected)

    def _step(self, direction: int) -> None:
        """Move the selection to the next/prev displayed row (wrapping) and highlight it."""
        if not self._rows:
            return
        if self._selected < 0:
            target = 0 if direction > 0 else len(self._rows) - 1
        else:
            target = (self._selected + direction) % len(self._rows)
        self._selected = target
        self._view.select_segment(target)

    def _ensure_tts(self) -> TTSProvider | None:
        """Lazily build the TTS provider (cheap — no model load), or ``None`` if construction fails.

        The heavy model load happens inside the worker on the first ``synthesize``; construction
        here is just the (cheap) provider object. A construction failure (e.g. a config error)
        degrades to ``None`` so the caller can defer rather than crash.

        VERIFY: the provider is built once and reused across successive single-shot worker threads
        (single-flight makes the access strictly sequential, so there is no data race). A
        torch/CUDA model first *loaded* on one worker thread and later *invoked* from a different
        worker thread can misbehave in some CUDA setups — confirm on real hardware that repeated
        regenerates across fresh threads reuse the model cleanly (else rebuild per render).
        """
        if self._tts is None:
            try:
                self._tts = self._deps.tts_factory(self._deps.resolved_config())
            except (CasttrophizerError, ImportError):
                return None
        return self._tts

    def _find_segment(self, segment_id: SegmentId) -> Segment | None:
        assert self._service is not None
        for chapter in self._service.project.book.chapters:
            for line in chapter.lines:
                for segment in line.segments:
                    if segment.id == segment_id:
                        return segment
        return None

    def _row_for(self, segment_id: SegmentId) -> AudioSegmentRow | None:
        """Derive the current row for ``segment_id`` (from ALL rows, not just the filtered set)."""
        assert self._service is not None
        assert self._cache is not None
        for row in audio_segment_rows(self._service.project, self._cache):
            if row.segment_id == segment_id:
                return row
        return None

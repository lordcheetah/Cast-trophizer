"""The Qt-free presenter for the voice-assignment panel and its view protocol.

Mirroring :class:`~casttrophizer.ui.attribution_presenter.AttributionPresenter`, this presenter
imports **no PySide6** (and **no** ``QtMultimedia``): it drives the widget through the
:class:`VoiceView` protocol and funnels every voice mutation through the single
:class:`~casttrophizer.review.service.ReviewService`. The Qt panel in ``ui/voice_panel.py`` is a
dumb structural implementer of :class:`VoiceView` — it (and it alone) owns the ``QMediaPlayer``.

Audition boundary: the presenter owns the *decision* of **what** clip to play (and whether it is
playable — the missing-file guard) and asks the view to play it via the render-only
:meth:`VoiceView.play_clip` hook; it never touches audio itself, so "what to play / is it
playable" is unit-testable without an audio device.

Thread ownership: everything here runs on the Qt main thread — each edit is an in-memory
dataclass mutation plus one small atomic ``project.json`` write (bulk-assign is many in-memory
assigns + a single save via ``assign_voices``), which :class:`ReviewService`'s docstring
sanctions as too cheap for a worker. No threading primitives live here.

The one coupling to the shell is a one-way ``on_reviewed`` callback fired after every mutating
edit, so :meth:`ProjectPresenter.refresh_after_review` can re-read the store and tick the live
"voices needed for: X" blocker summary. Audition intents are read-only and never fire it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from casttrophizer.app_service.voices import apply_bulk_voice, plan_bulk_voice
from casttrophizer.audio.synthesize import referenced_speaker_ids
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import VoiceCategory
from casttrophizer.domain.ids import SpeakerId
from casttrophizer.domain.models import Speaker
from casttrophizer.review.service import ReviewService
from casttrophizer.review.voice_view import (
    CategoryOption,
    SpeakerVoiceRow,
    category_options,
    needs_voice_count,
    speaker_voice_rows,
)

__all__ = ["VoiceView", "VoicePresenter"]


@runtime_checkable
class VoiceView(Protocol):
    """The render-only surface the presenter pushes at — a dumb, logic-free view.

    The Qt panel implements this structurally; tests substitute an in-memory fake. The
    ``play_clip`` / ``stop_playback`` hooks are the audition boundary: the panel plays; the
    presenter only decides what/whether to play.
    """

    def show_speakers(self, rows: list[SpeakerVoiceRow]) -> None:
        """Render the (currently filtered) speaker rows."""
        ...

    def show_category_options(self, options: list[CategoryOption]) -> None:
        """Populate the per-speaker category combo (one entry per ``VoiceCategory``)."""
        ...

    def show_needs_voice(self, needs_voice: int, referenced_total: int) -> None:
        """Show ``needs_voice`` of ``referenced_total`` referenced speakers still unvoiced."""
        ...

    def select_speaker(self, index: int) -> None:
        """Scroll to + highlight the row at ``index`` in the displayed list."""
        ...

    def play_clip(self, path: str) -> None:
        """Audition ``path`` — the panel plays it (the presenter decided it is playable)."""
        ...

    def stop_playback(self) -> None:
        """Stop + reset the panel's player (called on project switch so no clip lingers)."""
        ...

    def show_error(self, title: str, message: str) -> None:
        """Surface a recoverable error (bad clip path, uncovered bulk category)."""
        ...


class VoicePresenter:
    """Drives voice assignment for one loaded project through the shared :class:`ReviewService`.

    Holds the shared :class:`ReviewService` (adopted by reference on every :meth:`attach`, so all
    review panels edit one in-memory ``Project`` and no snapshot can diverge), the needs-voice-only
    filter state, and the current selection index into the *displayed* (filtered) rows. Contains
    no Qt and no voice logic — each intent looks the speaker up by id and delegates to the matching
    ``ReviewService`` method (or, for bulk, to the shared
    :mod:`casttrophizer.app_service.voices` planner).
    """

    def __init__(
        self,
        view: VoiceView,
        *,
        config: AppConfig,
        on_reviewed: Callable[[], None],
    ) -> None:
        self._view = view
        self._config = config
        self._on_reviewed = on_reviewed
        self._service: ReviewService | None = None
        self._needs_voice_only = True
        self._rows: list[SpeakerVoiceRow] = []  # the currently displayed (filtered) rows
        self._selected_id: SpeakerId | None = None  # the selected speaker (survives re-render)

    # -- lifecycle ---------------------------------------------------------- #
    def attach(self, service: ReviewService) -> None:
        """Adopt the shared :class:`ReviewService` (by reference) and reset the player.

        Called whenever a project is (re)loaded (via ``ProjectPresenter.on_project_loaded``) with
        the one service ``ProjectPresenter`` owns, so a later run adding speakers — or a re-open —
        never leaves the panel editing stale state, and asks the view to
        :meth:`~VoiceView.stop_playback` so a clip from the previous project isn't left playing.
        Does not render until :meth:`open`.
        """
        self._service = service
        self._needs_voice_only = True
        self._selected_id = None
        self._rows = []
        self._view.stop_playback()

    def open(self) -> None:
        """(Re)derive rows + category options and push them, plus the progress, to the view."""
        if self._service is None:
            return
        self._view.show_category_options(category_options())
        self._render()

    # -- mutating intents --------------------------------------------------- #
    def assign(self, speaker_id: SpeakerId, wav_path: str) -> None:
        """Register ``wav_path`` (labelled by the speaker) and assign it to the speaker.

        A missing/renamed file raises ``ValueError`` from ``register_voice_clip`` (it checks
        existence) → :meth:`~VoiceView.show_error`, nothing committed. The Qt panel opens the
        file dialog and passes a real path string; the presenter never opens dialogs.
        """

        def action(speaker: Speaker, service: ReviewService) -> None:
            clip = service.register_voice_clip(wav_path, label=speaker.name)
            service.assign_voice(speaker, clip)

        self._apply(speaker_id, action)

    def unassign(self, speaker_id: SpeakerId) -> None:
        """Clear a speaker's voice (re-opens the review gate)."""
        self._apply(speaker_id, lambda sp, svc: svc.unassign_voice(sp))

    def set_category(self, speaker_id: SpeakerId, category: VoiceCategory) -> None:
        """Set a speaker's voice category (no gate/cache effect; drives bulk default selection)."""
        self._apply(speaker_id, lambda sp, svc: svc.set_speaker_category(sp, category))

    def bulk_assign_by_category(self, overrides: dict[str, str | None]) -> None:
        """Bulk-assign each still-unvoiced referenced speaker its category's clip.

        Plans against exactly ``unresolved_speakers`` (the gate predicate) via the shared
        :func:`~casttrophizer.app_service.voices.plan_bulk_voice`; an uncovered category surfaces
        via :meth:`~VoiceView.show_error` and assigns **nothing**. Otherwise applies the plan and
        refreshes. A non-existent clip path raises ``ValueError`` → ``show_error`` (nothing
        committed — ``apply_bulk_voice`` validates every path before the single save).
        """
        if self._service is None:
            return
        plan = plan_bulk_voice(self._service.project, overrides, self._config)
        if plan.uncovered:
            detail = "; ".join(
                f"{category} (speakers: {', '.join(names)})"
                for category, names in plan.uncovered.items()
            )
            self._view.show_error("No clip for categories", detail)
            return
        try:
            apply_bulk_voice(self._service, plan)
        except ValueError as exc:
            self._view.show_error("Bulk assign failed", str(exc))
            return
        self._after_edit()

    # -- audition intents (read-only; no persistence, no ``on_reviewed``) ---- #
    def audition_speaker(self, speaker_id: SpeakerId) -> None:
        """Play the selected speaker's assigned clip; unvoiced/missing → :meth:`show_error`."""
        if self._service is None:
            return
        speaker = self._find_speaker(speaker_id)
        if speaker is None:
            self._view.show_error("Audition failed", f"no speaker {speaker_id!r} in project")
            return
        clip = next(
            (vc for vc in self._service.project.voice_clips if vc.id == speaker.voice_clip_id),
            None,
        )
        if clip is None:
            self._view.show_error("No clip", f"{speaker.name} has no assigned voice clip to play.")
            return
        self._play_if_present(clip.source_path)

    def audition_path(self, path: str) -> None:
        """Play a bulk-category picker clip the user already chose; missing → :meth:`show_error`."""
        self._play_if_present(path)

    # -- view state (no persistence) ---------------------------------------- #
    def set_filter(self, needs_voice_only: bool) -> None:
        """Toggle the needs-voice-only filter and re-push the (filtered) rows."""
        self._needs_voice_only = needs_voice_only
        self._render()

    def set_selected(self, index: int) -> None:
        """Record a selection the view made (a mouse click) so it survives a re-render."""
        if 0 <= index < len(self._rows):
            self._selected_id = self._rows[index].speaker_id

    # -- helpers ------------------------------------------------------------ #
    def _apply(
        self,
        speaker_id: SpeakerId,
        action: Callable[[Speaker, ReviewService], object],
    ) -> None:
        """Look ``speaker_id`` up, run ``action`` (a ReviewService call), then refresh + notify.

        A missing id or a ``ValueError`` from the service (bad clip path, etc.) surfaces via
        :meth:`~VoiceView.show_error` and leaves the state untouched — no crash, no notify.
        """
        if self._service is None:
            return
        speaker = self._find_speaker(speaker_id)
        if speaker is None:
            self._view.show_error("Edit failed", f"no speaker {speaker_id!r} in project")
            return
        try:
            action(speaker, self._service)
        except ValueError as exc:
            self._view.show_error("Edit failed", str(exc))
            return
        self._selected_id = speaker_id  # keep the cursor on the just-edited speaker
        self._after_edit()

    def _after_edit(self) -> None:
        """Re-derive + push rows/progress, re-highlight the selection, then fire ``on_reviewed``."""
        self._render()
        self._on_reviewed()

    def _render(self) -> None:
        """Push the filtered rows + the whole-cast progress, then re-highlight the selection."""
        assert self._service is not None
        project = self._service.project
        all_rows = speaker_voice_rows(project)
        self._rows = [r for r in all_rows if r.needs_voice] if self._needs_voice_only else all_rows
        self._view.show_speakers(self._rows)
        self._view.show_needs_voice(
            needs_voice_count(all_rows), len(referenced_speaker_ids(project))
        )
        self._reselect()

    def _reselect(self) -> None:
        """Re-highlight the last-selected speaker if it is still in the displayed rows."""
        if self._selected_id is None:
            return
        for index, row in enumerate(self._rows):
            if row.speaker_id == self._selected_id:
                self._view.select_speaker(index)
                return

    def _play_if_present(self, path: str) -> None:
        """Ask the view to play ``path`` iff the file exists; else surface a friendly error."""
        if not Path(path).is_file():
            self._view.show_error("Missing clip", f"voice clip file not found: {path}")
            return
        self._view.play_clip(path)

    def _find_speaker(self, speaker_id: SpeakerId) -> Speaker | None:
        assert self._service is not None
        return next((sp for sp in self._service.project.speakers if sp.id == speaker_id), None)

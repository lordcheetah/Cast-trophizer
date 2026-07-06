"""The Qt-free presenter for the attribution-review panel and its view protocol.

Mirroring :class:`~casttrophizer.ui.presenter.ProjectPresenter`, this presenter imports **no
PySide6**: it drives the widget through the :class:`AttributionView` protocol and funnels every
edit through the single :class:`~casttrophizer.review.service.ReviewService` (which mutates the
in-memory project and performs one atomic save per edit). The Qt panel in
``ui/attribution_panel.py`` is a dumb structural implementer of :class:`AttributionView`.

Thread ownership: everything here runs on the Qt main thread — each action is an in-memory
dataclass mutation plus one small atomic ``project.json`` write, which :class:`ReviewService`'s
docstring sanctions as too cheap to warrant a worker. No threading primitives live here.

The one coupling to the shell is a one-way ``on_reviewed`` callback fired after every mutating
edit, so :meth:`ProjectPresenter.refresh_after_review` can re-read the store and tick the
live blocker summary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from casttrophizer.domain.enums import SpeakerRole
from casttrophizer.domain.ids import SegmentId, SpeakerId
from casttrophizer.domain.models import Segment, find_narrator
from casttrophizer.review.attribution_view import (
    SegmentRow,
    SpeakerOption,
    needs_review_count,
    segment_rows,
    speaker_options,
)
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["AttributionView", "AttributionPresenter"]


@runtime_checkable
class AttributionView(Protocol):
    """The render-only surface the presenter pushes at — a dumb, logic-free view.

    The Qt panel implements this structurally (exactly as ``MainWindow`` implements
    :class:`~casttrophizer.ui.presenter.ProjectView`); tests substitute an in-memory fake.
    """

    def show_segments(self, rows: list[SegmentRow]) -> None:
        """Render the (currently filtered) segment rows."""
        ...

    def show_speaker_options(self, options: list[SpeakerOption]) -> None:
        """Populate the reassign combo (narrator first, then characters)."""
        ...

    def show_progress(self, needs_review: int, total: int) -> None:
        """Show ``needs_review`` of ``total`` segments still needing attribution review."""
        ...

    def select_segment(self, index: int) -> None:
        """Scroll to + highlight the row at ``index`` in the displayed list (keyboard nav)."""
        ...

    def show_error(self, title: str, message: str) -> None:
        """Surface a recoverable error (bad reassign id, empty new-speaker name)."""
        ...


class AttributionPresenter:
    """Drives attribution review for one loaded project through a :class:`ReviewService`.

    Holds the active :class:`ReviewService` (rebuilt on every :meth:`attach` so the panel never
    edits a stale snapshot), the needs-review-only filter state, and the current selection index
    into the *displayed* (filtered) rows. Contains no Qt and no attribution logic — each intent
    looks the segment up by id and delegates to the matching ``ReviewService`` method.
    """

    def __init__(self, view: AttributionView, *, on_reviewed: Callable[[], None]) -> None:
        self._view = view
        self._on_reviewed = on_reviewed
        self._service: ReviewService | None = None
        self._needs_review_only = True
        self._rows: list[SegmentRow] = []  # the currently displayed (filtered) rows
        self._selected = -1  # index into ``self._rows``; -1 == nothing selected

    # -- lifecycle ---------------------------------------------------------- #
    def attach(self, store: WorkspaceStore) -> None:
        """Load the project from ``store`` and build a fresh :class:`ReviewService`.

        Called whenever a project is (re)loaded (via ``ProjectPresenter.on_project_loaded``); it
        rebuilds the service on a freshly-loaded snapshot so a later run adding segments — or a
        re-open — never leaves the panel editing stale state. Does not render until :meth:`open`.
        """
        self._service = ReviewService(store, store.load())
        self._needs_review_only = True
        self._selected = -1
        self._rows = []

    def open(self) -> None:
        """(Re)derive rows + speaker options and push them, the progress, to the view."""
        if self._service is None:
            return
        self._view.show_speaker_options(speaker_options(self._service.project))
        self._render()

    # -- intents ------------------------------------------------------------ #
    def approve(self, segment_id: SegmentId) -> None:
        """Approve a segment's proposed attribution (resolves a NEEDS_REVIEW blocker)."""
        self._apply(segment_id, lambda seg, svc: svc.approve_attribution(seg))

    def reject(self, segment_id: SegmentId) -> None:
        """Reject a segment's attribution (REJECTED does not itself block; reassign next)."""
        self._apply(segment_id, lambda seg, svc: svc.reject_attribution(seg))

    def reassign_existing(self, segment_id: SegmentId, speaker_id: SpeakerId) -> None:
        """Reassign a segment to an existing speaker (narrator -> NARRATOR, else CHARACTER)."""

        def action(segment: Segment, service: ReviewService) -> None:
            role = self._role_for_speaker(speaker_id)
            service.set_segment_speaker(segment, speaker_id=speaker_id, role=role, approve=True)

        self._apply(segment_id, action)

    def reassign_new(self, segment_id: SegmentId, name: str) -> None:
        """Reassign a segment to a (possibly new) named character; the view collects ``name``."""
        if not name.strip():
            self._view.show_error("New speaker", "Enter a name for the new speaker.")
            return
        # Only this path can add a speaker, so it is the only one that refreshes the combo.
        self._apply(
            segment_id,
            lambda seg, svc: svc.reassign_segment_to_new_speaker(seg, name.strip()),
            refresh_options=True,
        )

    # -- view state (no persistence) ---------------------------------------- #
    def set_filter(self, needs_review_only: bool) -> None:
        """Toggle the needs-review-only filter and re-push the (filtered) rows."""
        self._needs_review_only = needs_review_only
        self._selected = -1  # indices change under the filter; drop the stale selection
        self._render()

    def next_flagged(self) -> None:
        """Advance the selection to the next flagged row (wrapping) and highlight it."""
        self._step_flagged(1)

    def prev_flagged(self) -> None:
        """Move the selection to the previous flagged row (wrapping) and highlight it."""
        self._step_flagged(-1)

    def set_selected(self, index: int) -> None:
        """Record a selection the view made (a mouse click), without re-highlighting."""
        if 0 <= index < len(self._rows):
            self._selected = index

    # -- helpers ------------------------------------------------------------ #
    def _apply(
        self,
        segment_id: SegmentId,
        action: Callable[[Segment, ReviewService], object],
        *,
        refresh_options: bool = False,
    ) -> None:
        """Look ``segment_id`` up, run ``action`` (a ReviewService call), then refresh + notify.

        A missing id or a ``ValueError`` from the service (bad speaker id, etc.) surfaces via
        :meth:`AttributionView.show_error` and leaves the state untouched — no crash, no notify.
        ``refresh_options`` re-pushes the speaker combo (only reassign-to-new can change it).
        """
        if self._service is None:
            return
        segment = self._find_segment(segment_id)
        if segment is None:
            self._view.show_error("Edit failed", f"no segment {segment_id!r} in project")
            return
        try:
            action(segment, self._service)
        except ValueError as exc:
            self._view.show_error("Edit failed", str(exc))
            return
        self._after_edit(refresh_options=refresh_options)

    def _after_edit(self, *, refresh_options: bool) -> None:
        """Re-derive + push rows/progress, re-highlight the next flag, then fire ``on_reviewed``.

        The combo is only re-pushed for reassign-to-new (the sole action that can add a speaker);
        approve/reject/reassign-existing leave the user's combo selection untouched.
        """
        assert self._service is not None
        if refresh_options:
            self._view.show_speaker_options(speaker_options(self._service.project))
        self._render()
        self._reselect_after_edit()  # keep the cursor on the next flag for fast triage
        self._on_reviewed()

    def _render(self) -> None:
        """Push the filtered rows + the whole-book progress (selection handled separately)."""
        assert self._service is not None
        all_rows = segment_rows(self._service.project)
        self._rows = (
            [r for r in all_rows if r.needs_review] if self._needs_review_only else all_rows
        )
        self._view.show_segments(self._rows)
        self._view.show_progress(needs_review_count(all_rows), len(all_rows))

    def _reselect_after_edit(self) -> None:
        """After an edit repopulates the list, land the cursor on the next still-flagged row.

        Repopulating the list drops the widget's selection, so the presenter re-highlights a
        sensible next target (keeping ``_selected`` and the view's current row in sync): the next
        flagged row at/after the old position (wrapping), else the clamped old position, else
        nothing when the list is empty.
        """
        if not self._rows:
            self._selected = -1
            return
        start = 0 if self._selected < 0 else min(self._selected, len(self._rows) - 1)
        flagged = [i for i, row in enumerate(self._rows) if row.needs_review]
        if flagged:
            ahead = [i for i in flagged if i >= start]
            target = ahead[0] if ahead else flagged[0]
        else:
            target = start
        self._selected = target
        self._view.select_segment(target)

    def _step_flagged(self, direction: int) -> None:
        """Move the selection to the next/prev ``needs_review`` displayed row (wrapping)."""
        flagged = [i for i, row in enumerate(self._rows) if row.needs_review]
        if not flagged:
            return
        if direction > 0:
            ahead = [i for i in flagged if i > self._selected]
            target = ahead[0] if ahead else flagged[0]
        else:
            behind = [i for i in flagged if i < self._selected or self._selected < 0]
            target = behind[-1] if behind else flagged[-1]
        self._selected = target
        self._view.select_segment(target)

    def _role_for_speaker(self, speaker_id: SpeakerId) -> SpeakerRole:
        """NARRATOR iff ``speaker_id`` is the reserved narrator's id, else CHARACTER."""
        assert self._service is not None
        narrator = find_narrator(self._service.project)
        if narrator is not None and speaker_id == narrator.id:
            return SpeakerRole.NARRATOR
        return SpeakerRole.CHARACTER

    def _find_segment(self, segment_id: SegmentId) -> Segment | None:
        assert self._service is not None
        for chapter in self._service.project.book.chapters:
            for line in chapter.lines:
                for segment in line.segments:
                    if segment.id == segment_id:
                        return segment
        return None

"""The Qt-free presenter for the text-suggestion review panel and its view protocol.

Mirroring :class:`~casttrophizer.ui.attribution_presenter.AttributionPresenter`, this presenter
imports **no PySide6**: it drives the widget through the :class:`SuggestionView` protocol and
funnels every edit through the **shared** :class:`~casttrophizer.review.service.ReviewService`
(adopted by reference on :meth:`attach`, so text edits, attribution, and voice all mutate one
in-memory project). The Qt panel in ``ui/suggestion_panel.py`` is a dumb structural implementer
of :class:`SuggestionView`.

Thread ownership: everything here runs on the Qt main thread — each accept/reject is an in-memory
dataclass mutation plus one small atomic ``project.json`` write, and a line edit adds a pure,
deterministic re-segmentation (no TTS/LLM), which :class:`ReviewService`'s docstring sanctions as
too cheap for a worker. The actual re-rendering happens later, on the next pipeline run.

The one coupling to the shell is a one-way ``on_reviewed`` callback fired after every mutating
edit, so :meth:`ProjectPresenter.refresh_after_review` can re-read the shared project and tick the
live blocker summary — accepting resolves a criterion-2 blocker; a whole-line edit can *add* a
criterion-1 blocker (its re-segmented quotes return to attribution review).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from casttrophizer.domain.ids import LineId, SuggestionId
from casttrophizer.domain.models import Line, TextSuggestion
from casttrophizer.review.service import ReviewService
from casttrophizer.review.suggestion_view import (
    SuggestionRow,
    pending_count,
    suggestion_rows,
)

__all__ = ["SuggestionView", "SuggestionPresenter"]


@runtime_checkable
class SuggestionView(Protocol):
    """The render-only surface the presenter pushes at — a dumb, logic-free view.

    The Qt panel implements this structurally (exactly as ``AttributionPanel`` implements
    :class:`~casttrophizer.ui.attribution_presenter.AttributionView`); tests substitute an
    in-memory fake.
    """

    def show_suggestions(self, rows: list[SuggestionRow]) -> None:
        """Render the (currently filtered) suggestion rows."""
        ...

    def show_progress(self, pending: int, total: int) -> None:
        """Show ``pending`` of ``total`` suggestions still awaiting a decision."""
        ...

    def select_suggestion(self, index: int) -> None:
        """Scroll to + highlight the row at ``index`` in the displayed list (keyboard nav)."""
        ...

    def show_error(self, title: str, message: str) -> None:
        """Surface a recoverable error (bad suggestion/line id, blank line edit)."""
        ...


class SuggestionPresenter:
    """Drives text-suggestion review for one project through the shared :class:`ReviewService`.

    Holds the shared :class:`ReviewService` (adopted by reference on every :meth:`attach`), the
    pending-only filter state (default ON), and the current selection index into the *displayed*
    (filtered) rows. Contains no Qt and no review logic — accept/reject look the suggestion up by
    id, the line edit looks the line up by id, and each delegates to the matching
    ``ReviewService`` method.
    """

    def __init__(self, view: SuggestionView, *, on_reviewed: Callable[[], None]) -> None:
        self._view = view
        self._on_reviewed = on_reviewed
        self._service: ReviewService | None = None
        self._pending_only = True
        self._rows: list[SuggestionRow] = []  # the currently displayed (filtered) rows
        self._selected = -1  # index into ``self._rows``; -1 == nothing selected

    # -- lifecycle ---------------------------------------------------------- #
    def attach(self, service: ReviewService) -> None:
        """Adopt the shared :class:`ReviewService` (by reference) and reset the view state.

        Called whenever a project is (re)loaded (via ``ProjectPresenter.on_project_loaded``) with
        the one service ``ProjectPresenter`` owns, so a later run — or a re-open — never leaves the
        panel editing stale state and the other panels' concurrent edits are visible on the same
        object. Does not render until :meth:`open`.
        """
        self._service = service
        self._pending_only = True
        self._selected = -1
        self._rows = []

    def open(self) -> None:
        """(Re)derive rows + progress and push them to the view."""
        if self._service is None:
            return
        self._render()

    # -- intents ------------------------------------------------------------ #
    def accept(self, suggestion_id: SuggestionId) -> None:
        """Accept a suggestion: fix the line **and** the matching segment (resolves a blocker)."""
        self._apply_suggestion(
            suggestion_id, lambda line, sug, svc: svc.accept_suggestion(line, sug.id)
        )

    def reject(self, suggestion_id: SuggestionId) -> None:
        """Reject a suggestion: keep the original text, status -> REJECTED (resolves a blocker)."""
        self._apply_suggestion(
            suggestion_id, lambda line, sug, svc: svc.reject_suggestion(line, sug.id)
        )

    def edit_line(self, line_id: LineId, new_text: str) -> None:
        """Free-text-edit a whole line (re-segmenting it); a blank edit surfaces an error.

        Re-segmenting can return the line's quotes to attribution review (criterion 1), so the
        service invalidates the review flag when it rebuilt — the shell blocker summary ticks up
        via ``on_reviewed``.
        """
        if self._service is None:
            return
        if not new_text.strip():
            self._view.show_error("Edit line", "Line text cannot be blank.")
            return
        line = self._find_line(line_id)
        if line is None:
            self._view.show_error("Edit failed", f"no line {line_id!r} in project")
            return
        try:
            self._service.edit_line_text(line, new_text)
        except ValueError as exc:
            self._view.show_error("Edit failed", str(exc))
            return
        self._after_edit()

    # -- view state (no persistence) ---------------------------------------- #
    def set_filter(self, pending_only: bool) -> None:
        """Toggle the pending-only filter and re-push the (filtered) rows."""
        self._pending_only = pending_only
        self._selected = -1  # indices change under the filter; drop the stale selection
        self._render()

    def next_pending(self) -> None:
        """Advance the selection to the next pending row (wrapping) and highlight it."""
        self._step_pending(1)

    def prev_pending(self) -> None:
        """Move the selection to the previous pending row (wrapping) and highlight it."""
        self._step_pending(-1)

    def set_selected(self, index: int) -> None:
        """Record a selection the view made (a mouse click), without re-highlighting."""
        if 0 <= index < len(self._rows):
            self._selected = index

    # -- helpers ------------------------------------------------------------ #
    def _apply_suggestion(
        self,
        suggestion_id: SuggestionId,
        action: Callable[[Line, TextSuggestion, ReviewService], object],
    ) -> None:
        """Look ``suggestion_id`` up, run ``action`` (a ReviewService call), then refresh + notify.

        A missing id or a ``ValueError`` from the service surfaces via
        :meth:`SuggestionView.show_error` and leaves the state untouched — no crash, no notify.
        """
        if self._service is None:
            return
        found = self._find_suggestion(suggestion_id)
        if found is None:
            self._view.show_error("Edit failed", f"no suggestion {suggestion_id!r} in project")
            return
        line, suggestion = found
        try:
            action(line, suggestion, self._service)
        except ValueError as exc:
            self._view.show_error("Edit failed", str(exc))
            return
        self._after_edit()

    def _after_edit(self) -> None:
        """Re-push rows/progress, re-highlight the next pending, then fire ``on_reviewed``."""
        self._render()
        self._reselect_after_edit()  # keep the cursor on the next pending item for fast triage
        self._on_reviewed()

    def _render(self) -> None:
        """Push the filtered rows + the whole-book progress (selection handled separately)."""
        assert self._service is not None
        all_rows = suggestion_rows(self._service.project)
        self._rows = [r for r in all_rows if r.is_pending] if self._pending_only else all_rows
        self._view.show_suggestions(self._rows)
        self._view.show_progress(pending_count(all_rows), len(all_rows))

    def _reselect_after_edit(self) -> None:
        """After an edit repopulates the list, land the cursor on the next still-pending row.

        Mirrors the attribution panel: the next pending row at/after the old position (wrapping),
        else the clamped old position, else nothing when the list is empty.
        """
        if not self._rows:
            self._selected = -1
            return
        start = 0 if self._selected < 0 else min(self._selected, len(self._rows) - 1)
        pending = [i for i, row in enumerate(self._rows) if row.is_pending]
        if pending:
            ahead = [i for i in pending if i >= start]
            target = ahead[0] if ahead else pending[0]
        else:
            target = start
        self._selected = target
        self._view.select_suggestion(target)

    def _step_pending(self, direction: int) -> None:
        """Move the selection to the next/prev ``is_pending`` displayed row (wrapping)."""
        pending = [i for i, row in enumerate(self._rows) if row.is_pending]
        if not pending:
            return
        if direction > 0:
            ahead = [i for i in pending if i > self._selected]
            target = ahead[0] if ahead else pending[0]
        else:
            behind = [i for i in pending if i < self._selected or self._selected < 0]
            target = behind[-1] if behind else pending[-1]
        self._selected = target
        self._view.select_suggestion(target)

    def _find_suggestion(self, suggestion_id: SuggestionId) -> tuple[Line, TextSuggestion] | None:
        assert self._service is not None
        for chapter in self._service.project.book.chapters:
            for line in chapter.lines:
                for suggestion in line.suggestions:
                    if suggestion.id == suggestion_id:
                        return line, suggestion
        return None

    def _find_line(self, line_id: LineId) -> Line | None:
        assert self._service is not None
        for chapter in self._service.project.book.chapters:
            for line in chapter.lines:
                if line.id == line_id:
                    return line
        return None

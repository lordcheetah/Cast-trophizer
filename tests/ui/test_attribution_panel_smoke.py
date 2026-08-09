"""Offscreen Qt smoke tests for the attribution panel — a handful, ``QT_QPA_PLATFORM=offscreen``.

These prove the real widget satisfies :class:`AttributionView`, that a real
:class:`AttributionPresenter` renders rows into it, that the action buttons drive the presenter,
and that ``MainWindow``'s ``QStackedWidget`` navigates between the shell and the review page. The
behavioral coverage lives in the loop-free ``test_attribution_presenter.py``.
"""

from __future__ import annotations

import pytest

from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.models import Project
from casttrophizer.review.service import ReviewService
from casttrophizer.ui.attribution_panel import AttributionPanel
from casttrophizer.ui.attribution_presenter import AttributionPresenter, AttributionView
from casttrophizer.ui.main_window import MainWindow
from casttrophizer.workspace.store import WorkspaceStore

pytestmark = pytest.mark.usefixtures("qapp")


def _wire(store: WorkspaceStore) -> tuple[AttributionPanel, AttributionPresenter, list[int]]:
    """A panel + presenter wired as ``ui/app.py`` does, plus a reviewed-call counter list."""
    panel = AttributionPanel()
    reviewed: list[int] = []
    presenter = AttributionPresenter(view=panel, on_reviewed=lambda: reviewed.append(1))
    presenter.attach(ReviewService(store, store.load()))
    panel.filter_changed = presenter.set_filter
    panel.approve_requested = presenter.approve
    panel.reassign_existing_requested = presenter.reassign_existing
    panel.reject_requested = presenter.reject
    panel.selection_changed = presenter.set_selected
    return panel, presenter, reviewed


def _seg_id(project: Project, text: str) -> str:
    return next(
        seg.id
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text == text
    )


def test_panel_satisfies_attribution_view_protocol() -> None:
    assert isinstance(AttributionPanel(), AttributionView)


def test_open_renders_flagged_rows_and_progress(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    panel, presenter, _ = _wire(tmp_workspace)

    presenter.open()

    assert panel._list.count() == 1  # needs-review-only default: just Bob's flagged quote
    assert "1 of 4" in panel._progress.text()
    assert panel._speaker_combo.count() == 3  # narrator + Alice + Bob


def test_filter_toggle_shows_all_rows(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    panel, presenter, _ = _wire(tmp_workspace)
    presenter.open()

    panel._filter_box.setChecked(False)  # emits toggled -> filter_changed(False)

    assert panel._list.count() == 4


def test_approve_button_drives_presenter_and_updates_widget(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    panel, presenter, reviewed = _wire(tmp_workspace)
    presenter.open()
    panel._list.setCurrentRow(0)  # select the only flagged row

    panel._approve_btn.click()

    # The approved row leaves the flagged-only list, progress ticks, and on_reviewed fired.
    assert panel._list.count() == 0
    assert "0 of 4" in panel._progress.text()
    assert reviewed == [1]
    reloaded = tmp_workspace.load()
    assert _seg_id(reloaded, '"Hi,"')  # still present
    approved = next(
        seg
        for ch in reloaded.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text == '"Hi,"'
    )
    assert approved.review_status == ReviewStatus.APPROVED


def test_main_window_navigates_between_shell_and_attribution_pages(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    window = MainWindow()

    assert window._stack.currentIndex() == 0  # shell first
    window.show_attribution_page()
    assert window._stack.currentIndex() == 1
    assert window._stack.currentWidget() is window.attribution_panel
    window.show_shell_page()
    assert window._stack.currentIndex() == 0


def test_review_button_enabled_only_with_segments(
    tmp_workspace: WorkspaceStore, review_ready_project: Project, sample_epub: object
) -> None:
    window = MainWindow()
    assert not window._review_btn.isEnabled()  # no project yet

    window.set_review_available(True)
    assert window._review_btn.isEnabled()
    window.set_review_available(False)
    assert not window._review_btn.isEnabled()

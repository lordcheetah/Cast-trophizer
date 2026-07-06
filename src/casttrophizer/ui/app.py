"""Application entry point: construct the QApplication, presenter, executor, and window.

``main`` is the console/GUI script target declared in ``pyproject.toml``
(``casttrophizer = "casttrophizer.ui.app:main"``). It assembles the Model-View-Presenter
graph: a Qt-free :class:`~casttrophizer.ui.presenter.ProjectPresenter` driving the
:class:`~casttrophizer.ui.main_window.MainWindow` (the view) through the
:class:`~casttrophizer.ui.run_executor.QtRunExecutor` (the threading), then wires the window's
intent callbacks to presenter methods.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from casttrophizer.app_service import AppServiceDeps
from casttrophizer.ui.attribution_presenter import AttributionPresenter
from casttrophizer.ui.main_window import MainWindow
from casttrophizer.ui.presenter import ProjectPresenter
from casttrophizer.ui.run_executor import QtRunExecutor

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Launch the Cast-trophizer GUI. Returns the Qt application exit code."""
    args = list(sys.argv if argv is None else argv)
    app = QApplication(args)

    deps = AppServiceDeps()  # config resolved lazily from the environment
    window = MainWindow()
    executor = QtRunExecutor()
    presenter = ProjectPresenter(view=window, executor=executor, deps=deps)

    # The attribution-review panel + its Qt-free presenter (slice 2). ``on_reviewed`` ticks the
    # shell's live blocker summary; ``on_project_loaded`` re-attaches a fresh ReviewService
    # snapshot whenever a project loads or a run finishes.
    panel = window.attribution_panel
    attribution_presenter = AttributionPresenter(
        view=panel, on_reviewed=presenter.refresh_after_review
    )
    presenter.on_project_loaded = attribution_presenter.attach

    # Wire the window's intents to the presenter (the view stays logic-free).
    window.open_requested = presenter.open
    window.new_requested = lambda epub, name: presenter.create(epub, name)
    window.run_requested = presenter.start_run
    window.stop_requested = presenter.stop_run

    # Navigate into the review page (render, then switch the stack) and back.
    def _open_review_page() -> None:
        attribution_presenter.open()
        window.show_attribution_page()

    window.review_attributions_requested = _open_review_page
    panel.back_requested = window.show_shell_page

    # Wire the panel's intents to the attribution presenter.
    panel.filter_changed = attribution_presenter.set_filter
    panel.approve_requested = attribution_presenter.approve
    panel.reassign_existing_requested = attribution_presenter.reassign_existing
    panel.reassign_new_requested = attribution_presenter.reassign_new
    panel.reject_requested = attribution_presenter.reject
    panel.next_flagged_requested = attribution_presenter.next_flagged
    panel.prev_flagged_requested = attribution_presenter.prev_flagged
    panel.selection_changed = attribution_presenter.set_selected

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

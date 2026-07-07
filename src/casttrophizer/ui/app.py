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
from casttrophizer.review.service import ReviewService
from casttrophizer.ui.attribution_presenter import AttributionPresenter
from casttrophizer.ui.main_window import MainWindow
from casttrophizer.ui.presenter import ProjectPresenter
from casttrophizer.ui.run_executor import QtRunExecutor
from casttrophizer.ui.suggestion_presenter import SuggestionPresenter
from casttrophizer.ui.voice_presenter import VoicePresenter

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
    # shell's live blocker summary; ``on_project_loaded`` hands it the shared ReviewService
    # whenever a project loads or a run finishes.
    panel = window.attribution_panel
    attribution_presenter = AttributionPresenter(
        view=panel, on_reviewed=presenter.refresh_after_review
    )

    # The voice-assignment panel + its Qt-free presenter (slice 3). ``on_reviewed`` reuses the
    # same live-blocker refresh; ``config`` feeds the env-default fallback in bulk resolution.
    voice_panel = window.voice_panel
    voice_presenter = VoicePresenter(
        view=voice_panel,
        config=deps.resolved_config(),
        on_reviewed=presenter.refresh_after_review,
    )

    # The text-suggestion review panel + its Qt-free presenter (slice 4). ``on_reviewed`` reuses
    # the same live-blocker refresh (accepting a suggestion / editing a line can move criterion 1
    # and 2 counts).
    suggestion_panel = window.suggestion_panel
    suggestion_presenter = SuggestionPresenter(
        view=suggestion_panel, on_reviewed=presenter.refresh_after_review
    )

    # One load hook attaches ALL THREE review panels to the single shared ReviewService on every
    # project load / run finish, so they edit one in-memory Project by reference — no per-panel
    # snapshot can diverge, so no whole-project save can clobber another panel's edit. The voice
    # panel's player is reset on project switch (its ``attach`` calls ``view.stop_playback()``).
    def _on_project_loaded(service: ReviewService) -> None:
        attribution_presenter.attach(service)
        voice_presenter.attach(service)
        suggestion_presenter.attach(service)

    presenter.on_project_loaded = _on_project_loaded

    # Wire the window's intents to the presenter (the view stays logic-free).
    window.open_requested = presenter.open
    window.new_requested = lambda epub, name: presenter.create(epub, name)
    window.run_requested = presenter.start_run
    window.stop_requested = presenter.stop_run

    # Navigate into a review page. No re-attach is needed: all three panels already share the one
    # ReviewService (attached on project load / run finish), so entering a page just re-renders
    # from the live shared Project and the shell blocker summary ticks off ``on_reviewed``.
    def _open_review_page() -> None:
        attribution_presenter.open()
        window.show_attribution_page()

    def _open_voice_page() -> None:
        voice_presenter.open()
        window.show_voice_page()

    def _open_text_page() -> None:
        suggestion_presenter.open()
        window.show_text_page()

    window.review_text_requested = _open_text_page
    window.review_attributions_requested = _open_review_page
    window.assign_voices_requested = _open_voice_page
    panel.back_requested = window.show_shell_page
    voice_panel.back_requested = window.show_shell_page
    suggestion_panel.back_requested = window.show_shell_page

    # Wire the attribution panel's intents to the attribution presenter.
    panel.filter_changed = attribution_presenter.set_filter
    panel.approve_requested = attribution_presenter.approve
    panel.reassign_existing_requested = attribution_presenter.reassign_existing
    panel.reassign_new_requested = attribution_presenter.reassign_new
    panel.reject_requested = attribution_presenter.reject
    panel.next_flagged_requested = attribution_presenter.next_flagged
    panel.prev_flagged_requested = attribution_presenter.prev_flagged
    panel.selection_changed = attribution_presenter.set_selected

    # Wire the voice panel's intents to the voice presenter.
    voice_panel.filter_changed = voice_presenter.set_filter
    voice_panel.assign_requested = voice_presenter.assign
    voice_panel.unassign_requested = voice_presenter.unassign
    voice_panel.set_category_requested = voice_presenter.set_category
    voice_panel.audition_speaker_requested = voice_presenter.audition_speaker
    voice_panel.audition_path_requested = voice_presenter.audition_path
    voice_panel.bulk_assign_requested = voice_presenter.bulk_assign_by_category
    voice_panel.selection_changed = voice_presenter.set_selected

    # Wire the suggestion panel's intents to the suggestion presenter.
    suggestion_panel.filter_changed = suggestion_presenter.set_filter
    suggestion_panel.accept_requested = suggestion_presenter.accept
    suggestion_panel.reject_requested = suggestion_presenter.reject
    suggestion_panel.edit_line_requested = suggestion_presenter.edit_line
    suggestion_panel.next_pending_requested = suggestion_presenter.next_pending
    suggestion_panel.prev_pending_requested = suggestion_presenter.prev_pending
    suggestion_panel.selection_changed = suggestion_presenter.set_selected

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

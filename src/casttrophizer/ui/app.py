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
from casttrophizer.ui.voice_presenter import VoicePresenter
from casttrophizer.workspace.store import WorkspaceStore

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

    # The voice-assignment panel + its Qt-free presenter (slice 3). ``on_reviewed`` reuses the
    # same live-blocker refresh; ``config`` feeds the env-default fallback in bulk resolution.
    voice_panel = window.voice_panel
    voice_presenter = VoicePresenter(
        view=voice_panel,
        config=deps.resolved_config(),
        on_reviewed=presenter.refresh_after_review,
    )

    # Combine the load hook so BOTH review panels re-attach a fresh ReviewService snapshot on
    # every project load / run finish, and the voice panel's player is reset on project switch
    # (voice ``attach`` calls ``view.stop_playback()``).
    def _on_project_loaded(store: WorkspaceStore) -> None:
        attribution_presenter.attach(store)
        voice_presenter.attach(store)

    presenter.on_project_loaded = _on_project_loaded

    # Wire the window's intents to the presenter (the view stays logic-free).
    window.open_requested = presenter.open
    window.new_requested = lambda epub, name: presenter.create(epub, name)
    window.run_requested = presenter.start_run
    window.stop_requested = presenter.stop_run

    # Navigate into a review page. Re-attach the entering panel to a freshly-loaded snapshot
    # first: both review presenters persist the whole project on save, so without a reload the
    # panel you enter would edit state from the last project-load and its next save would clobber
    # edits the *other* panel made in between (e.g. approve an attribution -> erase a voice just
    # assigned). Re-attaching on entry makes navigation a reload, so both panels' edits survive.
    def _open_review_page() -> None:
        if presenter.store is not None:
            attribution_presenter.attach(presenter.store)
        attribution_presenter.open()
        window.show_attribution_page()

    def _open_voice_page() -> None:
        if presenter.store is not None:
            voice_presenter.attach(presenter.store)
        voice_presenter.open()
        window.show_voice_page()

    window.review_attributions_requested = _open_review_page
    window.assign_voices_requested = _open_voice_page
    panel.back_requested = window.show_shell_page
    voice_panel.back_requested = window.show_shell_page

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

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

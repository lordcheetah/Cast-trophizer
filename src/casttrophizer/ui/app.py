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

    # Wire the window's intents to the presenter (the view stays logic-free).
    window.open_requested = presenter.open
    window.new_requested = lambda epub, name: presenter.create(epub, name)
    window.run_requested = presenter.start_run
    window.stop_requested = presenter.stop_run

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

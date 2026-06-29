"""Application entry point: construct the QApplication and show the MainWindow.

``main`` is the console/GUI script target declared in ``pyproject.toml``
(``casttrophizer = "casttrophizer.ui.app:main"``).
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from casttrophizer.ui.main_window import MainWindow

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Launch the Cast-trophizer GUI. Returns the Qt application exit code."""
    args = list(sys.argv if argv is None else argv)
    app = QApplication(args)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

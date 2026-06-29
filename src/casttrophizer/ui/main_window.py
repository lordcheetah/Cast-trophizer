"""The main application window (shell).

Qt lives **only** in this ``ui`` package. This is a minimal shell: a window with a
title and a placeholder central widget. The real per-stage panels (parse/review/
synthesize/assemble) are added later.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QWidget

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    """Top-level window shell for Cast-trophizer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cast-trophizer")
        self.resize(1024, 768)
        self._build_ui()

    def _build_ui(self) -> None:
        """Build the placeholder central widget. Real panels are added later."""
        placeholder = QLabel("Cast-trophizer\n\nProject UI coming soon.")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(placeholder)

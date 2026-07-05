"""The main application window — the dumb :class:`ProjectView` for the review-UI shell.

Qt lives **only** in this ``ui`` package. This window holds no business logic: it renders
exactly what :class:`~casttrophizer.ui.presenter.ProjectPresenter` pushes and emits intent
callbacks (Open / New / Run / Stop) the app wires to presenter methods. It satisfies the
:class:`~casttrophizer.ui.presenter.ProjectView` protocol structurally.

Scope (slice 1): open/create a project, show per-stage status for all six stages, run the
pipeline with a live progress bar + log, Stop/Resume, and render the terminal outcome. The
NEEDS_REVIEW blockers are shown as a **read-only summary only** — the editing panels
(text/attribution/voice/audio) come in later slices.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from casttrophizer.app_service import RunOutcome, RunOutcomeKind, StageRow
from casttrophizer.domain.enums import StageName

__all__ = ["MainWindow"]


def _noop() -> None:  # default intent callback until the app wires the presenter
    pass


class MainWindow(QMainWindow):
    """Top-level window: project header, six-stage status, progress/log, Run/Stop, outcome."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cast-trophizer")
        self.resize(1024, 768)

        # Intent callbacks (wired by ``ui/app.py`` to presenter methods).
        self.open_requested: Callable[[str], None] = lambda _dir: None
        self.new_requested: Callable[[str, str | None], None] = lambda _epub, _name: None
        self.run_requested: Callable[[], None] = _noop
        self.stop_requested: Callable[[], None] = _noop

        self._build_ui()

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)

        # Toolbar-ish action row: Open / New.
        actions = QHBoxLayout()
        self._open_btn = QPushButton("Open Project…")
        self._open_btn.clicked.connect(self._on_open_clicked)
        self._new_btn = QPushButton("New Project…")
        self._new_btn.clicked.connect(self._on_new_clicked)
        actions.addWidget(self._open_btn)
        actions.addWidget(self._new_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        # Project header.
        self._header = QLabel("No project open")
        root.addWidget(self._header)

        # Six-stage status list + next-stage label.
        self._stage_list = QListWidget()
        root.addWidget(self._stage_list)
        self._next_label = QLabel("next: -")
        root.addWidget(self._next_label)

        # Run / Stop controls.
        controls = QHBoxLayout()
        self._run_btn = QPushButton("Run")
        self._run_btn.clicked.connect(lambda: self.run_requested())
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(lambda: self.stop_requested())
        self._stop_btn.setEnabled(False)
        controls.addWidget(self._run_btn)
        controls.addWidget(self._stop_btn)
        controls.addStretch(1)
        root.addLayout(controls)

        # Progress bar + log.
        self._progress = QProgressBar()
        root.addWidget(self._progress)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        root.addWidget(self._log, stretch=1)

        # Outcome panel (read-only).
        self._outcome = QLabel("")
        self._outcome.setWordWrap(True)
        self._outcome.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self._outcome)

        self.setCentralWidget(central)

    # -- intent handlers (open file dialogs, then delegate) ----------------- #
    def _on_open_clicked(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Open project workspace")
        if directory:
            self.open_requested(directory)

    def _on_new_clicked(self) -> None:
        epub, _ = QFileDialog.getOpenFileName(
            self, "Select an EPUB", filter="EPUB files (*.epub);;All files (*)"
        )
        if epub:
            self.new_requested(epub, None)

    # -- ProjectView protocol ----------------------------------------------- #
    def show_project_loaded(self, name: str, workspace: str) -> None:
        self._header.setText(f"{name}\n{workspace}")
        self._outcome.setText("")

    def show_stage_rows(self, rows: list[StageRow]) -> None:
        self._stage_list.clear()
        for row in rows:
            marker = "▶ " if row.is_next else "   "
            status = row.status.value if row.status is not None else "-"
            self._stage_list.addItem(f"{marker}{row.name.value:11} {status}")

    def show_next_stage(self, name: StageName | None) -> None:
        self._next_label.setText(f"next: {name.value if name is not None else 'complete'}")

    def set_running(self, running: bool) -> None:
        self._run_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._open_btn.setEnabled(not running)
        self._new_btn.setEnabled(not running)

    def set_progress(self, done: int, total: int, message: str) -> None:
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(done)
        else:
            self._progress.setRange(0, 0)  # indeterminate until a total is known
        if message:
            self._log.appendPlainText(message)

    def show_outcome(self, outcome: RunOutcome) -> None:
        # Only a genuine completion fills the bar; FAILED/STOPPED/NEEDS_REVIEW leave it at its
        # current value so a halted run never shows a misleading "100% complete" bar.
        if outcome.kind == RunOutcomeKind.COMPLETED:
            self._progress.setRange(0, 1)
            self._progress.setValue(1)
        self._outcome.setText(self._format_outcome(outcome))

    def show_error(self, title: str, message: str) -> None:
        self._log.appendPlainText(f"{title}: {message}")
        QMessageBox.warning(self, title, message)

    # -- rendering helpers -------------------------------------------------- #
    @staticmethod
    def _format_outcome(outcome: RunOutcome) -> str:
        """Render a terminal :class:`RunOutcome` as read-only summary text."""
        if outcome.kind == RunOutcomeKind.COMPLETED:
            if outcome.output_path is not None:
                return f"Complete. Output: {outcome.output_path}"
            return "Complete."
        if outcome.kind == RunOutcomeKind.NEEDS_REVIEW:
            return f"Needs review (resolve, then Run again): {outcome.summary}"
        if outcome.kind == RunOutcomeKind.STOPPED:
            return "Stopped — resumable. Run again to continue."
        return f"Failed: {outcome.message}"

"""The main application window — the dumb :class:`ProjectView` for the review-UI shell.

Qt lives **only** in this ``ui`` package. This window holds no business logic: it renders
exactly what :class:`~casttrophizer.ui.presenter.ProjectPresenter` pushes and emits intent
callbacks (Open / New / Run / Stop) the app wires to presenter methods. It satisfies the
:class:`~casttrophizer.ui.presenter.ProjectView` protocol structurally.

Scope (slice 1): open/create a project, show per-stage status for all six stages, run the
pipeline with a live progress bar + log, Stop/Resume, and render the terminal outcome.

Slice 2 adds a ``QStackedWidget``: page 0 is the shell above, page 1 embeds the
:class:`~casttrophizer.ui.attribution_panel.AttributionPanel` for attribution review, so the
shell's live blocker summary stays visible alongside the review work. The remaining editing
panels (text/voice/audio) come in later slices.
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
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from casttrophizer.app_service import RunOutcome, RunOutcomeKind, StageRow
from casttrophizer.domain.enums import StageName
from casttrophizer.ui.attribution_panel import AttributionPanel
from casttrophizer.ui.suggestion_panel import SuggestionPanel
from casttrophizer.ui.voice_panel import VoicePanel

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
        self.review_text_requested: Callable[[], None] = _noop
        self.review_attributions_requested: Callable[[], None] = _noop
        self.assign_voices_requested: Callable[[], None] = _noop

        self._build_ui()

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        # A four-page stack: the slice-1 shell (page 0), the attribution panel (page 1), the
        # voice-assignment panel (page 2), and the text-suggestion panel (page 3).
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_shell_page())
        self.attribution_panel = AttributionPanel()
        self._stack.addWidget(self.attribution_panel)
        self.voice_panel = VoicePanel()
        self._stack.addWidget(self.voice_panel)
        self.suggestion_panel = SuggestionPanel()
        self._stack.addWidget(self.suggestion_panel)
        self.setCentralWidget(self._stack)

    def _build_shell_page(self) -> QWidget:
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

        # Outcome panel (read-only) + the entry point into the attribution-review page.
        self._outcome = QLabel("")
        self._outcome.setWordWrap(True)
        self._outcome.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self._outcome)

        review_row = QHBoxLayout()
        self._text_btn = QPushButton("Review text")
        self._text_btn.setEnabled(False)  # enabled once the project has text suggestions
        self._text_btn.clicked.connect(lambda: self.review_text_requested())
        review_row.addWidget(self._text_btn)
        self._review_btn = QPushButton("Review attributions")
        self._review_btn.setEnabled(False)  # enabled once the project has segments
        self._review_btn.clicked.connect(lambda: self.review_attributions_requested())
        review_row.addWidget(self._review_btn)
        self._voice_btn = QPushButton("Assign voices")
        self._voice_btn.setEnabled(False)  # enabled once the project has referenced speakers
        self._voice_btn.clicked.connect(lambda: self.assign_voices_requested())
        review_row.addWidget(self._voice_btn)
        review_row.addStretch(1)
        root.addLayout(review_row)

        return central

    # -- page navigation ---------------------------------------------------- #
    def show_shell_page(self) -> None:
        """Switch the stack back to the slice-1 shell (page 0)."""
        self._stack.setCurrentIndex(0)

    def show_attribution_page(self) -> None:
        """Switch the stack to the embedded attribution-review panel (page 1)."""
        self._stack.setCurrentIndex(1)

    def show_voice_page(self) -> None:
        """Switch the stack to the embedded voice-assignment panel (page 2)."""
        self._stack.setCurrentIndex(2)

    def show_text_page(self) -> None:
        """Switch the stack to the embedded text-suggestion panel (page 3)."""
        self._stack.setCurrentIndex(3)

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

    def set_text_available(self, available: bool) -> None:
        self._text_btn.setEnabled(available)

    def set_review_available(self, available: bool) -> None:
        self._review_btn.setEnabled(available)

    def set_voice_available(self, available: bool) -> None:
        self._voice_btn.setEnabled(available)

    def set_running(self, running: bool) -> None:
        self._run_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._open_btn.setEnabled(not running)
        self._new_btn.setEnabled(not running)
        if running:
            # Gate text/review/voice navigation on ``not running`` so an edit's ReviewService
            # save can't race the worker's project.json write (or clobber segments the run just
            # produced). ``_on_finished -> _refresh_status -> set_text_available /
            # set_review_available / set_voice_available`` re-enables them after.
            self._text_btn.setEnabled(False)
            self._review_btn.setEnabled(False)
            self._voice_btn.setEnabled(False)

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
            return (
                f"Needs review: {outcome.summary}. "
                "Use 'Review attributions' to resolve them, then Run again."
            )
        if outcome.kind == RunOutcomeKind.STOPPED:
            return "Stopped — resumable. Run again to continue."
        return f"Failed: {outcome.message}"

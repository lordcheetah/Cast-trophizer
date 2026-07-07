"""The dumb Qt widget for attribution review — a structural :class:`AttributionView`.

Qt lives **only** in this ``ui`` package. This panel holds no attribution logic: it renders the
rows :class:`~casttrophizer.ui.attribution_presenter.AttributionPresenter` pushes and emits
intent callbacks (approve / reassign / reject / filter / nav / back) the app wires to presenter
methods, exactly as :class:`~casttrophizer.ui.main_window.MainWindow` does for the shell.

It satisfies :class:`AttributionView` structurally (``show_segments`` / ``show_speaker_options``
/ ``show_progress`` / ``select_segment`` / ``show_error``).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.review.attribution_view import SegmentRow, SpeakerOption

__all__ = ["AttributionPanel"]


class AttributionPanel(QWidget):
    """Flat, filterable segment list + a per-selection action row for attribution review."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Intent callbacks (wired by ``ui/app.py`` to presenter methods).
        self.filter_changed: Callable[[bool], None] = lambda _on: None
        self.approve_requested: Callable[[str], None] = lambda _sid: None
        self.reassign_existing_requested: Callable[[str, str], None] = lambda _sid, _spk: None
        self.reassign_new_requested: Callable[[str, str], None] = lambda _sid, _name: None
        self.reject_requested: Callable[[str], None] = lambda _sid: None
        self.next_flagged_requested: Callable[[], None] = lambda: None
        self.prev_flagged_requested: Callable[[], None] = lambda: None
        self.selection_changed: Callable[[int], None] = lambda _idx: None
        self.back_requested: Callable[[], None] = lambda: None

        self._rows: list[SegmentRow] = []  # what's currently displayed (parallel to the list)

        self._build_ui()

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Header row: Back + the needs-review filter + progress.
        header = QHBoxLayout()
        self._back_btn = QPushButton("← Back to project")
        self._back_btn.clicked.connect(lambda: self.back_requested())
        header.addWidget(self._back_btn)
        self._filter_box = QCheckBox("Needs review only")
        self._filter_box.setChecked(True)  # default ON — keeps the list ~the flagged count
        self._filter_box.toggled.connect(lambda on: self.filter_changed(on))
        header.addWidget(self._filter_box)
        header.addStretch(1)
        self._progress = QLabel("")
        header.addWidget(self._progress)
        root.addLayout(header)

        # The flat segment list. It takes focus after a selection and would otherwise eat the
        # nav letter keys (N/J/P/K) for its own type-ahead search, so we filter its key events.
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        self._list.installEventFilter(self)
        root.addWidget(self._list, stretch=1)

        # Per-selection action row.
        actions = QHBoxLayout()
        self._approve_btn = QPushButton("Approve")
        self._approve_btn.clicked.connect(self._on_approve)
        actions.addWidget(self._approve_btn)

        self._speaker_combo = QComboBox()
        actions.addWidget(self._speaker_combo)
        self._reassign_btn = QPushButton("Reassign")
        self._reassign_btn.clicked.connect(self._on_reassign_existing)
        actions.addWidget(self._reassign_btn)

        self._new_btn = QPushButton("New speaker…")
        self._new_btn.clicked.connect(self._on_reassign_new)
        actions.addWidget(self._new_btn)

        self._reject_btn = QPushButton("Reject")
        self._reject_btn.clicked.connect(self._on_reject)
        actions.addWidget(self._reject_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        # A standing hint: reject alone leaves the old (possibly wrong) speaker; reassign next.
        self._hint = QLabel(
            "Reassign is the primary path. Reject only clears the proposal — reassign a "
            "rejected line so it doesn't render with the wrong voice. A new speaker still "
            "needs a voice clip (assigned in a later step) before the run can finish."
        )
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

    # -- intent handlers ---------------------------------------------------- #
    def _selected_segment_id(self) -> str | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row].segment_id
        return None

    def _on_current_row_changed(self, index: int) -> None:
        self.selection_changed(index)

    def _on_approve(self) -> None:
        seg_id = self._selected_segment_id()
        if seg_id is not None:
            self.approve_requested(seg_id)

    def _on_reject(self) -> None:
        seg_id = self._selected_segment_id()
        if seg_id is not None:
            self.reject_requested(seg_id)

    def _on_reassign_existing(self) -> None:
        seg_id = self._selected_segment_id()
        speaker_id = self._speaker_combo.currentData()
        if seg_id is not None and speaker_id is not None:
            self.reassign_existing_requested(seg_id, str(speaker_id))

    def _on_reassign_new(self) -> None:
        seg_id = self._selected_segment_id()
        if seg_id is None:
            return
        name, ok = QInputDialog.getText(self, "New speaker", "Character name:")
        if ok and name.strip():
            self.reassign_new_requested(seg_id, name)

    # -- keyboard navigation ------------------------------------------------ #
    def _handle_nav_key(self, key: object) -> bool:
        """N/J -> next flagged, P/K -> previous flagged, Enter -> approve. True iff consumed."""
        if key in (Qt.Key.Key_N, Qt.Key.Key_J):
            self.next_flagged_requested()
        elif key in (Qt.Key.Key_P, Qt.Key.Key_K):
            self.prev_flagged_requested()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_approve()
        else:
            return False
        return True

    def keyPressEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        """Handle nav keys when the panel itself has focus (see also :meth:`eventFilter`)."""
        if not self._handle_nav_key(event.key()):  # type: ignore[attr-defined]
            super().keyPressEvent(event)  # type: ignore[arg-type]

    def eventFilter(self, watched: object, event: object) -> bool:  # noqa: N802 (Qt override)
        """Intercept nav keys on the focused list before its type-ahead search swallows them."""
        if watched is self._list and event.type() == QEvent.Type.KeyPress:  # type: ignore[attr-defined]
            if self._handle_nav_key(event.key()):  # type: ignore[attr-defined]
                return True
        return super().eventFilter(watched, event)  # type: ignore[arg-type]

    # -- AttributionView protocol ------------------------------------------- #
    def show_segments(self, rows: list[SegmentRow]) -> None:
        self._rows = rows
        self._list.blockSignals(True)  # repopulating fires spurious currentRowChanged
        try:
            self._list.clear()
            for row in rows:
                self._list.addItem(_row_item(row))
        finally:
            self._list.blockSignals(False)

    def show_speaker_options(self, options: list[SpeakerOption]) -> None:
        self._speaker_combo.clear()
        for option in options:
            self._speaker_combo.addItem(option.display, userData=option.speaker_id)

    def show_progress(self, needs_review: int, total: int) -> None:
        self._progress.setText(f"{needs_review} of {total} attributions need review")

    def select_segment(self, index: int) -> None:
        if 0 <= index < self._list.count():
            self._list.setCurrentRow(index)

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)


def _row_item(row: SegmentRow) -> QListWidgetItem:
    """Render one :class:`SegmentRow` as a list item; flagged rows read prominently."""
    tag = ""
    if row.is_narrator_fallback:
        tag = "  (auto→narrator)"
    elif row.needs_review:
        tag = "  (needs review)"
    text = _truncate(row.text)
    label = (
        f"Ch {row.chapter_index + 1} · line {row.line_order + 1}   "
        f"{text}   — {row.speaker_display} [{row.role.value}] "
        f"conf {row.confidence:.2f}{tag}"
    )
    item = QListWidgetItem(label)
    if row.needs_review:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    if row.review_status == ReviewStatus.REJECTED:
        item.setText(label + "  (rejected — reassign)")
    return item


def _truncate(text: str, limit: int = 60) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"

"""The dumb Qt widget for text-suggestion review — a structural :class:`SuggestionView`.

Qt lives **only** in this ``ui`` package. This panel holds no review logic: it renders the rows
:class:`~casttrophizer.ui.suggestion_presenter.SuggestionPresenter` pushes and emits intent
callbacks (accept / reject / edit-line / filter / nav / back) the app wires to presenter methods,
exactly as :class:`~casttrophizer.ui.attribution_panel.AttributionPanel` does.

It satisfies :class:`SuggestionView` structurally (``show_suggestions`` / ``show_progress`` /
``select_suggestion`` / ``show_error``).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox,
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

from casttrophizer.review.suggestion_view import SuggestionRow

__all__ = ["SuggestionPanel"]


class SuggestionPanel(QWidget):
    """Flat, filterable text-suggestion list + a per-selection action row for text review."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Intent callbacks (wired by ``ui/app.py`` to presenter methods).
        self.filter_changed: Callable[[bool], None] = lambda _on: None
        self.accept_requested: Callable[[str], None] = lambda _sid: None
        self.reject_requested: Callable[[str], None] = lambda _sid: None
        self.edit_line_requested: Callable[[str, str], None] = lambda _lid, _text: None
        self.next_pending_requested: Callable[[], None] = lambda: None
        self.prev_pending_requested: Callable[[], None] = lambda: None
        self.selection_changed: Callable[[int], None] = lambda _idx: None
        self.back_requested: Callable[[], None] = lambda: None

        self._rows: list[SuggestionRow] = []  # what's currently displayed (parallel to the list)

        self._build_ui()

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Header row: Back + the pending-only filter + progress.
        header = QHBoxLayout()
        self._back_btn = QPushButton("← Back to project")
        self._back_btn.clicked.connect(lambda: self.back_requested())
        header.addWidget(self._back_btn)
        self._filter_box = QCheckBox("Pending only")
        self._filter_box.setChecked(True)  # default ON — keeps the list ~the pending count
        self._filter_box.toggled.connect(lambda on: self.filter_changed(on))
        header.addWidget(self._filter_box)
        header.addStretch(1)
        self._progress = QLabel("")
        header.addWidget(self._progress)
        root.addLayout(header)

        # The flat suggestion list. It takes focus after a selection and would otherwise eat the
        # nav letter keys (N/J/P/K) for its own type-ahead search, so we filter its key events.
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        self._list.installEventFilter(self)
        root.addWidget(self._list, stretch=1)

        # Per-selection action row.
        actions = QHBoxLayout()
        self._accept_btn = QPushButton("Accept")
        self._accept_btn.clicked.connect(self._on_accept)
        actions.addWidget(self._accept_btn)

        self._reject_btn = QPushButton("Reject")
        self._reject_btn.clicked.connect(self._on_reject)
        actions.addWidget(self._reject_btn)

        self._edit_btn = QPushButton("Edit line…")
        self._edit_btn.clicked.connect(self._on_edit_line)
        actions.addWidget(self._edit_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        # A standing hint: propagation is now live, so be honest about what each action costs.
        self._hint = QLabel(
            "Accepting a suggestion fixes both the line and the spoken segment, which re-renders "
            "on the next run. Editing a whole line re-segments it — its quotes return to "
            "attribution review and it will re-render."
        )
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

    # -- intent handlers ---------------------------------------------------- #
    def _selected_row(self) -> SuggestionRow | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _on_current_row_changed(self, index: int) -> None:
        self.selection_changed(index)

    def _on_accept(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.accept_requested(row.suggestion_id)

    def _on_reject(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.reject_requested(row.suggestion_id)

    def _on_edit_line(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        text, ok = QInputDialog.getText(self, "Edit line", "Line text:", text=row.line_text)
        if ok and text.strip():
            self.edit_line_requested(row.line_id, text)

    # -- keyboard navigation ------------------------------------------------ #
    def _handle_nav_key(self, key: object) -> bool:
        """N/J -> next pending, P/K -> previous pending, Enter -> accept. True iff consumed."""
        if key in (Qt.Key.Key_N, Qt.Key.Key_J):
            self.next_pending_requested()
        elif key in (Qt.Key.Key_P, Qt.Key.Key_K):
            self.prev_pending_requested()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_accept()
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

    # -- SuggestionView protocol -------------------------------------------- #
    def show_suggestions(self, rows: list[SuggestionRow]) -> None:
        self._rows = rows
        self._list.blockSignals(True)  # repopulating fires spurious currentRowChanged
        try:
            self._list.clear()
            for row in rows:
                self._list.addItem(_row_item(row))
        finally:
            self._list.blockSignals(False)

    def show_progress(self, pending: int, total: int) -> None:
        self._progress.setText(f"{pending} of {total} suggestions pending")

    def select_suggestion(self, index: int) -> None:
        if 0 <= index < self._list.count():
            self._list.setCurrentRow(index)

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)


def _row_item(row: SuggestionRow) -> QListWidgetItem:
    """Render one :class:`SuggestionRow` as a list item; pending rows read prominently."""
    tag = "  (pending)" if row.is_pending else f"  ({row.status.value})"
    label = (
        f"Ch {row.chapter_index + 1} · line {row.line_order + 1}   "
        f"{_truncate(row.line_text)}   — {row.original!r} → {row.suggested!r} "
        f"[{row.reason}] conf {row.confidence:.2f}{tag}"
    )
    item = QListWidgetItem(label)
    if row.is_pending:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


def _truncate(text: str, limit: int = 60) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"

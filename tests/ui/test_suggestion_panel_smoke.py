"""Offscreen Qt smoke tests for the suggestion panel — a handful, ``QT_QPA_PLATFORM=offscreen``.

These prove the real widget satisfies :class:`SuggestionView`, that pushed rows render, that the
action buttons emit the intents with the right ids (with the Edit-line dialog prefilled from the
selected row), and that ``MainWindow``'s ``QStackedWidget`` navigates to the text page. The
behavioral coverage lives in the loop-free ``test_suggestion_presenter.py``.
"""

from __future__ import annotations

import pytest

from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.review.suggestion_view import SuggestionRow
from casttrophizer.ui import suggestion_panel as suggestion_panel_module
from casttrophizer.ui.main_window import MainWindow
from casttrophizer.ui.suggestion_panel import SuggestionPanel
from casttrophizer.ui.suggestion_presenter import SuggestionView

pytestmark = pytest.mark.usefixtures("qapp")


def _row(sug_id: str, line_id: str, *, pending: bool = True) -> SuggestionRow:
    return SuggestionRow(
        suggestion_id=sug_id,
        line_id=line_id,
        chapter_index=0,
        chapter_title="One",
        line_order=0,
        line_text="The mie sat.",
        original="mie",
        suggested="mouse",
        reason="spellcheck",
        confidence=0.7,
        status=ReviewStatus.PENDING if pending else ReviewStatus.REJECTED,
        is_pending=pending,
    )


def test_panel_satisfies_suggestion_view_protocol() -> None:
    assert isinstance(SuggestionPanel(), SuggestionView)


def test_push_rows_and_accept_reject_fire_intents() -> None:
    panel = SuggestionPanel()
    accepted: list[str] = []
    rejected: list[str] = []
    panel.accept_requested = accepted.append
    panel.reject_requested = rejected.append

    panel.show_suggestions([_row("sug_1", "line_1")])
    assert panel._list.count() == 1
    panel._list.setCurrentRow(0)

    panel._accept_btn.click()
    assert accepted == ["sug_1"]
    panel._reject_btn.click()
    assert rejected == ["sug_1"]


def test_edit_line_uses_prefilled_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = SuggestionPanel()
    edits: list[tuple[str, str]] = []
    panel.edit_line_requested = lambda lid, text: edits.append((lid, text))
    panel.show_suggestions([_row("sug_1", "line_1")])
    panel._list.setCurrentRow(0)

    captured: dict[str, str] = {}

    def _fake_get_text(parent: object, title: str, label: str, text: str = "") -> tuple[str, bool]:
        captured["prefill"] = text
        return "Edited text.", True

    monkeypatch.setattr(
        suggestion_panel_module.QInputDialog, "getText", staticmethod(_fake_get_text)
    )
    panel._edit_btn.click()

    assert captured["prefill"] == "The mie sat."  # dialog prefilled with the row's line_text
    assert edits == [("line_1", "Edited text.")]


def test_keyboard_nav_and_enter_drive_intents() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    panel = SuggestionPanel()
    nexts: list[int] = []
    prevs: list[int] = []
    accepted: list[str] = []
    panel.next_pending_requested = lambda: nexts.append(1)
    panel.prev_pending_requested = lambda: prevs.append(1)
    panel.accept_requested = accepted.append
    panel.show_suggestions([_row("sug_1", "line_1")])
    panel._list.setCurrentRow(0)

    def _press(key: Qt.Key) -> None:
        panel.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))

    _press(Qt.Key.Key_N)
    _press(Qt.Key.Key_P)
    _press(Qt.Key.Key_Return)
    assert nexts == [1]
    assert prevs == [1]
    assert accepted == ["sug_1"]


def test_nav_keys_fire_when_the_list_has_focus() -> None:
    """The real-focus path: keys delivered to the focused list route through the event filter.

    Regression for the nav being dead when the ``QListWidget`` has focus (it would otherwise
    consume N/J/P/K for its own type-ahead search before the panel's ``keyPressEvent`` saw them).
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    panel = SuggestionPanel()
    nexts: list[int] = []
    accepted: list[str] = []
    panel.next_pending_requested = lambda: nexts.append(1)
    panel.accept_requested = accepted.append
    panel.show_suggestions([_row("sug_1", "line_1")])
    panel._list.setCurrentRow(0)

    def _send(key: Qt.Key) -> None:
        # Deliver to the list (where focus lives) -> the panel's installed eventFilter intercepts.
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(panel._list, event)

    _send(Qt.Key.Key_N)
    _send(Qt.Key.Key_Return)
    assert nexts == [1]  # nav reached the presenter despite the list having focus
    assert accepted == ["sug_1"]


def test_progress_label_reflects_pending_over_total() -> None:
    panel = SuggestionPanel()
    panel.show_suggestions([_row("s1", "l1"), _row("s2", "l2", pending=False)])
    panel.show_progress(1, 2)
    assert "1 of 2" in panel._progress.text()


def test_main_window_navigates_to_text_page() -> None:
    window = MainWindow()

    assert window._stack.currentIndex() == 0  # shell first
    window.show_text_page()
    assert window._stack.currentIndex() == 3
    assert window._stack.currentWidget() is window.suggestion_panel
    window.show_shell_page()
    assert window._stack.currentIndex() == 0


def test_text_button_enabled_only_with_suggestions() -> None:
    window = MainWindow()
    assert not window._text_btn.isEnabled()  # no project yet

    window.set_text_available(True)
    assert window._text_btn.isEnabled()
    window.set_text_available(False)
    assert not window._text_btn.isEnabled()

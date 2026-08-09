"""The dumb Qt widget for per-segment audio review — a structural :class:`AudioView`.

Qt lives **only** in this ``ui`` package, and — alongside ``ui/voice_panel.py`` — this is one of
the only modules importing ``PySide6.QtMultimedia`` (``QMediaPlayer`` / ``QAudioOutput``): the
audition boundary. The panel holds no review logic: it renders the rows
:class:`~casttrophizer.ui.audio_presenter.AudioPresenter` pushes and emits intent callbacks (play /
approve / regenerate / reject / play-next / filter / back) the app wires to presenter methods,
exactly as :class:`~casttrophizer.ui.voice_panel.VoicePanel` does.

It satisfies :class:`AudioView` structurally (``show_segments`` / ``show_progress`` /
``select_segment`` / ``play_clip`` / ``stop_playback`` / ``set_regenerate_available`` / ``set_busy``
/ ``show_error``).

Player lifecycle mirrors ``voice_panel``: one panel-owned :class:`QMediaPlayer` +
:class:`QAudioOutput`, built lazily on first play and released on project switch
(``stop_playback``) and teardown (``closeEvent``). ``stop_playback`` calls ``setSource(QUrl())`` to
free the Windows file handle **before** a regenerate overwrites the WAV and **before** a replay.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from casttrophizer.review.audio_view import AudioSegmentRow

__all__ = ["AudioPanel"]


class AudioPanel(QWidget):
    """Flat, filterable segment list + a per-selection action row for audio review."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Intent callbacks (wired by ``ui/app.py`` to presenter methods).
        self.filter_changed: Callable[[bool], None] = lambda _on: None
        self.play_requested: Callable[[str], None] = lambda _sid: None
        self.approve_requested: Callable[[str], None] = lambda _sid: None
        self.regenerate_requested: Callable[[str], None] = lambda _sid: None
        self.reject_requested: Callable[[str], None] = lambda _sid: None
        self.play_next_requested: Callable[[], None] = lambda: None
        self.selection_changed: Callable[[int], None] = lambda _idx: None
        self.back_requested: Callable[[], None] = lambda: None

        self._rows: list[AudioSegmentRow] = []  # what's currently displayed (parallel to the list)
        self._busy = False
        self._regenerate_available = True

        # The audition player is created lazily on first play (see ``_ensure_player``).
        self._player: QMediaPlayer | None = None
        self._audio_output: QAudioOutput | None = None

        self._build_ui()

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Header row: Back + the unapproved-only filter + progress.
        header = QHBoxLayout()
        self._back_btn = QPushButton("← Back to project")
        self._back_btn.clicked.connect(lambda: self.back_requested())
        header.addWidget(self._back_btn)
        self._filter_box = QCheckBox("Unapproved only")
        self._filter_box.setChecked(True)  # default ON — focuses on takes still needing sign-off
        self._filter_box.toggled.connect(lambda on: self.filter_changed(on))
        header.addWidget(self._filter_box)
        header.addStretch(1)
        self._progress = QLabel("")
        header.addWidget(self._progress)
        root.addLayout(header)

        # The flat segment list.
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        root.addWidget(self._list, stretch=1)

        # Per-selection action row.
        actions = QHBoxLayout()
        self._play_btn = QPushButton("▶ Play")
        self._play_btn.setToolTip("Play the selected segment's rendered take")
        self._play_btn.clicked.connect(self._on_play)
        actions.addWidget(self._play_btn)

        self._play_next_btn = QPushButton("Play next")
        self._play_next_btn.clicked.connect(lambda: self.play_next_requested())
        actions.addWidget(self._play_next_btn)

        self._approve_btn = QPushButton("Approve")
        self._approve_btn.clicked.connect(self._on_approve)
        actions.addWidget(self._approve_btn)

        self._regenerate_btn = QPushButton("Regenerate")
        self._regenerate_btn.setToolTip("Render this segment again now with a fresh seed")
        self._regenerate_btn.clicked.connect(self._on_regenerate)
        actions.addWidget(self._regenerate_btn)

        self._reject_btn = QPushButton("Re-render on next run")
        self._reject_btn.setToolTip(
            "Re-roll this segment now; it re-renders on the next full Run (no Chatterbox needed)"
        )
        self._reject_btn.clicked.connect(self._on_reject)
        actions.addWidget(self._reject_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        # A standing hint.
        self._hint = QLabel(
            "Play a segment's take, Approve a good one, or Regenerate a bad one (renders now with "
            "a fresh seed and auto-plays). Approval is a sign-off marker — it never re-renders."
        )
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

    # -- intent handlers ---------------------------------------------------- #
    def _selected_row(self) -> AudioSegmentRow | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _on_current_row_changed(self, index: int) -> None:
        self.selection_changed(index)

    def _on_play(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.play_requested(row.segment_id)

    def _on_approve(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.approve_requested(row.segment_id)

    def _on_regenerate(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.regenerate_requested(row.segment_id)

    def _on_reject(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.reject_requested(row.segment_id)

    # -- AudioView protocol ------------------------------------------------- #
    def show_segments(self, rows: list[AudioSegmentRow]) -> None:
        self._rows = rows
        self._list.blockSignals(True)  # repopulating fires spurious currentRowChanged
        try:
            self._list.clear()
            for row in rows:
                self._list.addItem(_row_item(row))
        finally:
            self._list.blockSignals(False)

    def show_progress(self, approved: int, rendered: int) -> None:
        self._progress.setText(f"{approved} of {rendered} rendered segments approved")

    def select_segment(self, index: int) -> None:
        if 0 <= index < self._list.count():
            self._list.setCurrentRow(index)

    def play_clip(self, path: str) -> None:
        player = self._ensure_player()
        player.stop()
        player.setSource(QUrl.fromLocalFile(path))
        player.play()

    def stop_playback(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())  # release the file handle before overwrite/replay

    def set_regenerate_available(self, available: bool) -> None:
        self._regenerate_available = available
        self._sync_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_buttons()

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    # -- button enablement -------------------------------------------------- #
    def _sync_buttons(self) -> None:
        """Reflect ``_busy`` / ``_regenerate_available`` into the action buttons.

        While busy (a render in flight) every action — Play, Play-next, Approve, Regenerate, Reject
        **and** Back — is disabled: a second render can't start, the user can't navigate away
        mid-render, and Play can't fire on the segment being regenerated (now PENDING with a cleared
        key) and pop a spurious "no rendered audio" dialog. Regenerate additionally requires the
        ``tts`` extra (``set_regenerate_available``); Reject (defer to the next Run) needs no
        provider, so it is enabled whenever not busy.
        """
        self._back_btn.setEnabled(not self._busy)
        self._play_btn.setEnabled(not self._busy)
        self._play_next_btn.setEnabled(not self._busy)
        self._approve_btn.setEnabled(not self._busy)
        self._reject_btn.setEnabled(not self._busy)
        self._regenerate_btn.setEnabled(self._regenerate_available and not self._busy)

    # -- audition player lifecycle ------------------------------------------ #
    def _ensure_player(self) -> QMediaPlayer:
        """Lazily construct the single panel-owned player + audio output on first play."""
        if self._player is None:
            self._player = QMediaPlayer(self)
            self._audio_output = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.errorOccurred.connect(self._on_player_error)
        return self._player

    def _on_player_error(self, _error: object, error_string: str) -> None:
        """Surface a backend/playback error (deleted file, no audio backend) as a friendly error."""
        message = error_string or "Could not play the selected clip."
        self.show_error("Playback error", message)

    # -- teardown ----------------------------------------------------------- #
    def closeEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        """Stop + release the player on teardown so no take keeps playing / holds a file handle."""
        self.stop_playback()
        super().closeEvent(event)  # type: ignore[arg-type]


def _row_item(row: AudioSegmentRow) -> QListWidgetItem:
    """Render one :class:`AudioSegmentRow` as a list item; failed rows read prominently."""
    if row.is_failed:
        state = "FAILED"
    elif row.is_approved:
        state = "approved"
    elif row.is_playable:
        state = "rendered"
    else:
        state = "not rendered"
    label = (
        f"Ch {row.chapter_index + 1} · line {row.line_order + 1}   "
        f"{row.speaker_display}: {_truncate(row.text)}   [{state}]"
    )
    item = QListWidgetItem(label)
    if row.is_failed:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


def _truncate(text: str, limit: int = 60) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"

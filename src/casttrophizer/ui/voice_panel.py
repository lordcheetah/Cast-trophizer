"""The dumb Qt widget for voice assignment — a structural :class:`VoiceView`.

Qt lives **only** in this ``ui`` package, and this is the **only** module importing
``PySide6.QtMultimedia`` (``QMediaPlayer`` / ``QAudioOutput``) — the audition boundary. The
panel holds no voice logic: it renders the rows
:class:`~casttrophizer.ui.voice_presenter.VoicePresenter` pushes and emits intent callbacks
(assign / unassign / set-category / audition / bulk / filter / back) the app wires to presenter
methods, exactly as :class:`~casttrophizer.ui.attribution_panel.AttributionPanel` does.

It satisfies :class:`VoiceView` structurally (``show_speakers`` / ``show_category_options`` /
``show_needs_voice`` / ``select_speaker`` / ``play_clip`` / ``stop_playback`` / ``show_error``).

Audition player: one panel-owned :class:`QMediaPlayer` + :class:`QAudioOutput`, constructed
lazily on first play and torn down on project switch (``stop_playback``) and widget teardown
(``closeEvent``), with ``errorOccurred`` surfaced as a friendly error — so a deleted file or a
headless box with no audio backend never crashes playback.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from casttrophizer.app_service.voices import REST_CATEGORY_KEYS
from casttrophizer.domain.enums import VoiceCategory
from casttrophizer.review.voice_view import CategoryOption, SpeakerVoiceRow

__all__ = ["VoicePanel"]

#: The bulk-assign picker rows (per-category keys + the shared ``default``), in display order.
_BULK_KEYS = (*REST_CATEGORY_KEYS, "default")


class VoicePanel(QWidget):
    """Cast list + per-speaker action row + bulk-by-category pickers for voice assignment."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Intent callbacks (wired by ``ui/app.py`` to presenter methods).
        self.filter_changed: Callable[[bool], None] = lambda _on: None
        self.assign_requested: Callable[[str, str], None] = lambda _sid, _path: None
        self.unassign_requested: Callable[[str], None] = lambda _sid: None
        self.set_category_requested: Callable[[str, VoiceCategory], None] = lambda _sid, _cat: None
        self.audition_speaker_requested: Callable[[str], None] = lambda _sid: None
        self.audition_path_requested: Callable[[str], None] = lambda _path: None
        self.bulk_assign_requested: Callable[[dict[str, str | None]], None] = lambda _ov: None
        self.selection_changed: Callable[[int], None] = lambda _idx: None
        self.back_requested: Callable[[], None] = lambda: None

        self._rows: list[SpeakerVoiceRow] = []  # what's currently displayed (parallel to the list)
        self._bulk_paths: dict[str, str | None] = {key: None for key in _BULK_KEYS}
        self._bulk_labels: dict[str, QLabel] = {}
        self._bulk_play_btns: dict[str, QPushButton] = {}

        # The audition player is created lazily on first play (see ``_ensure_player``).
        self._player: QMediaPlayer | None = None
        self._audio_output: QAudioOutput | None = None

        self._build_ui()

    # -- construction ------------------------------------------------------- #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Header row: Back + the needs-voice filter + progress.
        header = QHBoxLayout()
        self._back_btn = QPushButton("← Back to project")
        self._back_btn.clicked.connect(lambda: self.back_requested())
        header.addWidget(self._back_btn)
        self._filter_box = QCheckBox("Needs voice only")
        self._filter_box.setChecked(True)  # default ON — keeps the list ~the unvoiced count
        self._filter_box.toggled.connect(lambda on: self.filter_changed(on))
        header.addWidget(self._filter_box)
        header.addStretch(1)
        self._progress = QLabel("")
        header.addWidget(self._progress)
        root.addLayout(header)

        # The flat cast list.
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        root.addWidget(self._list, stretch=1)

        # Per-selection action row.
        actions = QHBoxLayout()
        self._assign_btn = QPushButton("Assign clip…")
        self._assign_btn.clicked.connect(self._on_assign)
        actions.addWidget(self._assign_btn)

        self._unassign_btn = QPushButton("Unassign")
        self._unassign_btn.clicked.connect(self._on_unassign)
        actions.addWidget(self._unassign_btn)

        actions.addWidget(QLabel("Category:"))
        self._category_combo = QComboBox()
        self._category_combo.activated.connect(self._on_category_changed)
        actions.addWidget(self._category_combo)

        self._play_btn = QPushButton("▶")
        self._play_btn.setToolTip("Audition the selected speaker's assigned clip")
        self._play_btn.clicked.connect(self._on_audition_speaker)
        actions.addWidget(self._play_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        # Bulk-assign-by-category panel.
        root.addWidget(self._build_bulk_group())

        # Progress/summary hint.
        self._hint = QLabel(
            "Assign each referenced speaker a reference clip, or bulk-assign the remaining "
            "unvoiced cast by category. A speaker with no usable voice clip blocks the run."
        )
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

    def _build_bulk_group(self) -> QGroupBox:
        group = QGroupBox("Bulk-assign the remaining unvoiced cast by category")
        outer = QVBoxLayout(group)

        self._bulk_summary = QLabel("still need voices: (none)")
        outer.addWidget(self._bulk_summary)

        for key in _BULK_KEYS:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{key}:"))
            pick_btn = QPushButton("Pick…")
            pick_btn.clicked.connect(lambda _checked=False, k=key: self._on_pick_bulk(k))
            row.addWidget(pick_btn)
            path_label = QLabel("(none)")
            self._bulk_labels[key] = path_label
            row.addWidget(path_label, stretch=1)
            play_btn = QPushButton("▶")
            play_btn.setEnabled(False)  # enabled once a clip is picked
            play_btn.clicked.connect(lambda _checked=False, k=key: self._on_audition_bulk(k))
            self._bulk_play_btns[key] = play_btn
            row.addWidget(play_btn)
            outer.addLayout(row)

        apply_btn = QPushButton("Apply to unvoiced")
        apply_btn.clicked.connect(self._on_apply_bulk)
        outer.addWidget(apply_btn)
        return group

    # -- selection helpers -------------------------------------------------- #
    def _selected_row(self) -> SpeakerVoiceRow | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _on_current_row_changed(self, index: int) -> None:
        self.selection_changed(index)
        self._sync_action_row()

    def _sync_action_row(self) -> None:
        """Reflect the selected row into the category combo + the ▶ enablement (no signals out)."""
        row = self._selected_row()
        if row is not None:
            data_index = self._category_combo.findData(row.category)
            if data_index >= 0:
                self._category_combo.blockSignals(True)
                self._category_combo.setCurrentIndex(data_index)
                self._category_combo.blockSignals(False)
        self._play_btn.setEnabled(row is not None and row.voice_clip_path is not None)

    # -- per-speaker intent handlers ---------------------------------------- #
    def _on_assign(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select a voice clip", filter="WAV (*.wav)")
        if path:
            self.assign_requested(row.speaker_id, path)

    def _on_unassign(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.unassign_requested(row.speaker_id)

    def _on_category_changed(self, _index: int) -> None:
        row = self._selected_row()
        category = self._category_combo.currentData()
        if row is not None and isinstance(category, VoiceCategory):
            self.set_category_requested(row.speaker_id, category)

    def _on_audition_speaker(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.audition_speaker_requested(row.speaker_id)

    # -- bulk intent handlers ----------------------------------------------- #
    def _on_pick_bulk(self, key: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select a default clip for {key}", filter="WAV (*.wav)"
        )
        if path:
            self._bulk_paths[key] = path
            self._bulk_labels[key].setText(path)
            self._bulk_play_btns[key].setEnabled(True)

    def _on_audition_bulk(self, key: str) -> None:
        path = self._bulk_paths.get(key)
        if path:
            self.audition_path_requested(path)

    def _on_apply_bulk(self) -> None:
        self.bulk_assign_requested(dict(self._bulk_paths))

    # -- VoiceView protocol ------------------------------------------------- #
    def show_speakers(self, rows: list[SpeakerVoiceRow]) -> None:
        self._rows = rows
        self._list.blockSignals(True)  # repopulating fires spurious currentRowChanged
        try:
            self._list.clear()
            for row in rows:
                self._list.addItem(_row_item(row))
        finally:
            self._list.blockSignals(False)
        self._update_bulk_summary(rows)
        self._sync_action_row()

    def show_category_options(self, options: list[CategoryOption]) -> None:
        self._category_combo.clear()
        for option in options:
            self._category_combo.addItem(option.display, userData=option.value)

    def show_needs_voice(self, needs_voice: int, referenced_total: int) -> None:
        self._progress.setText(
            f"{needs_voice} of {referenced_total} referenced speakers still need a voice"
        )

    def select_speaker(self, index: int) -> None:
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
            self._player.setSource(QUrl())  # clear the source so no handle lingers

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

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

    def _update_bulk_summary(self, rows: list[SpeakerVoiceRow]) -> None:
        """Show a live 'still need voices: man ×3, woman ×2' summary of what Apply will touch.

        Counts the needs-voice rows within ``rows`` by category; because a needs-voice row is
        present regardless of the filter (it is the filter's own subset), this is the complete
        set of speakers a bulk-assign would target, so the summary never under-reports.
        """
        counts: dict[str, int] = {}
        for row in rows:
            if row.needs_voice:
                counts[row.category.value] = counts.get(row.category.value, 0) + 1
        if counts:
            detail = ", ".join(f"{cat} ×{n}" for cat, n in sorted(counts.items()))
        else:
            detail = "(none)"
        self._bulk_summary.setText(f"still need voices: {detail}")

    # -- teardown ----------------------------------------------------------- #
    def closeEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        """Stop + release the player on teardown so no clip keeps playing / holds a file handle."""
        self.stop_playback()
        super().closeEvent(event)  # type: ignore[arg-type]


def _row_item(row: SpeakerVoiceRow) -> QListWidgetItem:
    """Render one :class:`SpeakerVoiceRow` as a list item; needs-voice rows read prominently."""
    if row.clip_file_missing:
        voice_state = "assigned but file missing"
    elif row.voice_clip_label is not None:
        voice_state = row.voice_clip_label
    else:
        voice_state = "NO VOICE"
    ref = "referenced" if row.is_referenced else "unreferenced"
    tag = "  ⚠ NEEDS VOICE" if row.needs_voice else ""
    label = f"{row.name} ({row.role.value}) [{row.category.value}] " f"— {voice_state} · {ref}{tag}"
    item = QListWidgetItem(label)
    if row.needs_voice:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item

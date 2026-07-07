"""Offscreen Qt smoke tests for the voice panel — a handful, ``QT_QPA_PLATFORM=offscreen``.

These prove the real widget satisfies :class:`VoiceView`, that a real :class:`VoicePresenter`
renders rows into it, that the action buttons drive the presenter, and that ``MainWindow``'s
``QStackedWidget`` navigates to (and locks out) the voice page. Audition is exercised with a
**mocked** ``QMediaPlayer`` so no audio backend is required — asserting ``setSource`` / ``play``
on ▶ and ``stop`` on project switch. The behavioral coverage lives in the loop-free
``test_voice_presenter.py``.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from casttrophizer.config import AppConfig
from casttrophizer.domain.models import Project
from casttrophizer.review.service import ReviewService
from casttrophizer.ui import voice_panel as voice_panel_module
from casttrophizer.ui.main_window import MainWindow
from casttrophizer.ui.voice_panel import VoicePanel
from casttrophizer.ui.voice_presenter import VoicePresenter, VoiceView
from casttrophizer.workspace.store import WorkspaceStore

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------------------- #
# a mocked QMediaPlayer / QAudioOutput (no audio backend)
# --------------------------------------------------------------------------- #
class _FakeSignal:
    def connect(self, _slot: object) -> None:  # errorOccurred.connect(...)
        pass


class _FakePlayer:
    """Records the audition calls without touching a real multimedia backend."""

    instances: list[_FakePlayer] = []

    def __init__(self, _parent: object = None) -> None:
        self.sources: list[object] = []
        self.plays = 0
        self.stops = 0
        self.errorOccurred = _FakeSignal()
        _FakePlayer.instances.append(self)

    def setAudioOutput(self, _out: object) -> None:  # noqa: N802 (Qt API)
        pass

    def setSource(self, url: object) -> None:  # noqa: N802 (Qt API)
        self.sources.append(url)

    def play(self) -> None:
        self.plays += 1

    def stop(self) -> None:
        self.stops += 1


class _FakeAudioOutput:
    def __init__(self, _parent: object = None) -> None:
        pass


@pytest.fixture
def mock_player(monkeypatch: pytest.MonkeyPatch) -> type[_FakePlayer]:
    """Swap the panel's ``QMediaPlayer`` / ``QAudioOutput`` for backend-free fakes."""
    _FakePlayer.instances = []
    monkeypatch.setattr(voice_panel_module, "QMediaPlayer", _FakePlayer)
    monkeypatch.setattr(voice_panel_module, "QAudioOutput", _FakeAudioOutput)
    return _FakePlayer


def _wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 128)
    return str(path)


def _wire(store: WorkspaceStore) -> tuple[VoicePanel, VoicePresenter, list[int]]:
    """A panel + presenter wired as ``ui/app.py`` does, plus a reviewed-call counter list."""
    panel = VoicePanel()
    reviewed: list[int] = []
    presenter = VoicePresenter(
        view=panel, config=AppConfig(), on_reviewed=lambda: reviewed.append(1)
    )
    presenter.attach(ReviewService(store, store.load()))
    panel.filter_changed = presenter.set_filter
    panel.assign_requested = presenter.assign
    panel.unassign_requested = presenter.unassign
    panel.set_category_requested = presenter.set_category
    panel.audition_speaker_requested = presenter.audition_speaker
    panel.audition_path_requested = presenter.audition_path
    panel.bulk_assign_requested = presenter.bulk_assign_by_category
    panel.selection_changed = presenter.set_selected
    return panel, presenter, reviewed


# --------------------------------------------------------------------------- #
# structural / render
# --------------------------------------------------------------------------- #
def test_panel_satisfies_voice_view_protocol() -> None:
    assert isinstance(VoicePanel(), VoiceView)


def test_open_renders_needs_voice_rows_and_progress(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    panel, presenter, _ = _wire(tmp_workspace)

    presenter.open()

    assert panel._list.count() == 1  # needs-voice-only default: just Bob (unvoiced)
    assert "1 of 3" in panel._progress.text()
    assert panel._category_combo.count() == 5  # every VoiceCategory
    assert "unknown ×1" in panel._bulk_summary.text()  # Bob (unvoiced) is category `unknown`


def test_filter_toggle_shows_all_rows(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    panel, presenter, _ = _wire(tmp_workspace)
    presenter.open()

    panel._filter_box.setChecked(False)  # emits toggled -> filter_changed(False)

    assert panel._list.count() == 3  # narrator + Alice + Bob


def test_assign_button_drives_presenter_and_updates_widget(
    tmp_workspace: WorkspaceStore,
    review_ready_project: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel, presenter, reviewed = _wire(tmp_workspace)
    presenter.open()
    panel._list.setCurrentRow(0)  # select the only unvoiced row (Bob)

    clip = _wav(tmp_path / "bob.wav")
    monkeypatch.setattr(
        voice_panel_module.QFileDialog, "getOpenFileName", lambda *a, **k: (clip, "")
    )
    panel._assign_btn.click()

    assert panel._list.count() == 0  # Bob left the needs-voice-only list
    assert "0 of 3" in panel._progress.text()
    assert reviewed == [1]
    reloaded_bob = next(sp for sp in tmp_workspace.load().speakers if sp.name == "Bob")
    assert reloaded_bob.voice_clip_id is not None


def test_assign_button_noop_when_dialog_cancelled(
    tmp_workspace: WorkspaceStore,
    review_ready_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel, presenter, reviewed = _wire(tmp_workspace)
    presenter.open()
    panel._list.setCurrentRow(0)

    monkeypatch.setattr(voice_panel_module.QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    panel._assign_btn.click()

    assert reviewed == []  # a cancelled dialog assigns nothing


# --------------------------------------------------------------------------- #
# audition (mocked player)
# --------------------------------------------------------------------------- #
def test_play_clip_sets_source_and_plays_on_mocked_player(
    mock_player: type[_FakePlayer],
) -> None:
    panel = VoicePanel()

    panel.play_clip("/some/clip.wav")

    assert len(mock_player.instances) == 1  # lazily constructed once
    player = mock_player.instances[0]
    assert player.plays == 1
    assert player.sources  # setSource called with a QUrl
    assert player.sources[-1].toLocalFile().endswith("clip.wav")


def test_audition_button_plays_selected_voiced_clip(
    tmp_workspace: WorkspaceStore,
    review_ready_project: Project,
    mock_player: type[_FakePlayer],
) -> None:
    panel, presenter, _ = _wire(tmp_workspace)
    presenter.open()
    panel._filter_box.setChecked(False)  # show all so Alice (voiced) is selectable
    alice_index = next(i for i, r in enumerate(panel._rows) if r.name == "Alice")
    panel._list.setCurrentRow(alice_index)

    assert panel._play_btn.isEnabled()  # enabled for a row with an assigned clip
    panel._play_btn.click()

    assert mock_player.instances and mock_player.instances[0].plays == 1


def test_stop_playback_on_project_switch_stops_without_error(
    mock_player: type[_FakePlayer],
) -> None:
    panel = VoicePanel()
    panel.play_clip("/some/clip.wav")  # lazily builds the player
    player = mock_player.instances[0]

    panel.stop_playback()  # what ``attach`` calls on project switch

    assert player.stops >= 1


def test_stop_playback_is_safe_before_any_play() -> None:
    panel = VoicePanel()
    panel.stop_playback()  # no player yet -> must not raise


# --------------------------------------------------------------------------- #
# shell navigation + run-lockout
# --------------------------------------------------------------------------- #
def test_main_window_navigates_to_voice_page() -> None:
    window = MainWindow()

    assert window._stack.currentIndex() == 0  # shell first
    window.show_voice_page()
    assert window._stack.currentIndex() == 2
    assert window._stack.currentWidget() is window.voice_panel
    window.show_shell_page()
    assert window._stack.currentIndex() == 0


def test_voice_button_enabled_only_with_segments_and_locked_out_while_running() -> None:
    window = MainWindow()
    assert not window._voice_btn.isEnabled()  # no project yet

    window.set_voice_available(True)
    assert window._voice_btn.isEnabled()

    window.set_running(True)  # run-lockout disables the entry point
    assert not window._voice_btn.isEnabled()

    window.set_running(False)
    window.set_voice_available(True)  # ``_refresh_status`` re-enables after a run
    assert window._voice_btn.isEnabled()

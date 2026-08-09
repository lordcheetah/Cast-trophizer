"""Offscreen Qt smoke tests for the audio panel — a handful, ``QT_QPA_PLATFORM=offscreen``.

These prove the real widget satisfies :class:`AudioView`, that ▶/Approve/Regenerate/Reject wire to
the intents, that ``stop_playback`` clears the source, that ``set_busy`` /
``set_regenerate_available`` toggle button enablement, and that ``MainWindow``'s ``QStackedWidget``
navigates to (and locks out) the audio page. Playback uses a **mocked** ``QMediaPlayer`` so no audio
backend is required. Behavioral coverage lives in the loop-free ``test_audio_presenter.py``.
"""

from __future__ import annotations

import pytest

from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.review.audio_view import AudioSegmentRow
from casttrophizer.ui import audio_panel as audio_panel_module
from casttrophizer.ui.audio_panel import AudioPanel
from casttrophizer.ui.audio_presenter import AudioView
from casttrophizer.ui.main_window import MainWindow

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------------------- #
# a mocked QMediaPlayer / QAudioOutput (no audio backend)
# --------------------------------------------------------------------------- #
class _FakeSignal:
    def connect(self, _slot: object) -> None:
        pass


class _FakePlayer:
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
    _FakePlayer.instances = []
    monkeypatch.setattr(audio_panel_module, "QMediaPlayer", _FakePlayer)
    monkeypatch.setattr(audio_panel_module, "QAudioOutput", _FakeAudioOutput)
    return _FakePlayer


def _row(text: str, *, approved: bool = False, playable: bool = True) -> AudioSegmentRow:
    return AudioSegmentRow(
        segment_id=f"seg-{text}",
        chapter_index=0,
        chapter_title="One",
        line_order=0,
        text=text,
        speaker_display="narrator",
        audio_status=ReviewStatus.APPROVED if approved else ReviewStatus.COMPLETED,
        is_approved=approved,
        is_failed=False,
        is_rendered=True,
        is_playable=playable,
        wav_path="/tmp/x.wav" if playable else None,
    )


# --------------------------------------------------------------------------- #
# structural / render / intents
# --------------------------------------------------------------------------- #
def test_panel_satisfies_audio_view_protocol() -> None:
    assert isinstance(AudioPanel(), AudioView)


def test_show_segments_and_progress_render() -> None:
    panel = AudioPanel()
    panel.show_segments([_row("a"), _row("b")])
    panel.show_progress(1, 2)
    assert panel._list.count() == 2
    assert "1 of 2" in panel._progress.text()


def test_buttons_drive_intents() -> None:
    panel = AudioPanel()
    played: list[str] = []
    approved: list[str] = []
    regenerated: list[str] = []
    rejected: list[str] = []
    panel.play_requested = played.append
    panel.approve_requested = approved.append
    panel.regenerate_requested = regenerated.append
    panel.reject_requested = rejected.append
    panel.show_segments([_row("a")])
    panel._list.setCurrentRow(0)

    panel._play_btn.click()
    panel._approve_btn.click()
    panel._regenerate_btn.click()
    panel._reject_btn.click()

    assert played == ["seg-a"]
    assert approved == ["seg-a"]
    assert regenerated == ["seg-a"]
    assert rejected == ["seg-a"]


# --------------------------------------------------------------------------- #
# player (mocked)
# --------------------------------------------------------------------------- #
def test_play_clip_sets_source_and_plays(mock_player: type[_FakePlayer]) -> None:
    panel = AudioPanel()
    panel.play_clip("/some/take.wav")
    assert len(mock_player.instances) == 1
    player = mock_player.instances[0]
    assert player.plays == 1
    assert player.sources[-1].toLocalFile().endswith("take.wav")


def test_stop_playback_clears_source(mock_player: type[_FakePlayer]) -> None:
    panel = AudioPanel()
    panel.play_clip("/some/take.wav")
    player = mock_player.instances[0]
    panel.stop_playback()
    assert player.stops >= 1
    assert player.sources[-1].isEmpty()  # setSource(QUrl()) released the handle


def test_stop_playback_safe_before_any_play() -> None:
    AudioPanel().stop_playback()  # no player yet -> must not raise


# --------------------------------------------------------------------------- #
# button enablement: busy + regenerate availability
# --------------------------------------------------------------------------- #
def test_set_busy_disables_all_actions_including_play() -> None:
    panel = AudioPanel()
    panel.set_busy(True)
    assert not panel._approve_btn.isEnabled()
    assert not panel._regenerate_btn.isEnabled()
    assert not panel._reject_btn.isEnabled()
    assert not panel._back_btn.isEnabled()
    # Play + Play-next are disabled mid-render too: the segment being regenerated is PENDING with a
    # cleared key, so a Play click would pop a spurious "no rendered audio" dialog.
    assert not panel._play_btn.isEnabled()
    assert not panel._play_next_btn.isEnabled()

    panel.set_busy(False)
    assert panel._approve_btn.isEnabled()
    assert panel._back_btn.isEnabled()
    assert panel._play_btn.isEnabled()


def test_set_regenerate_available_gates_only_regenerate() -> None:
    panel = AudioPanel()
    panel.set_regenerate_available(False)
    assert not panel._regenerate_btn.isEnabled()
    assert panel._reject_btn.isEnabled()  # defer never needs the provider
    assert panel._approve_btn.isEnabled()

    panel.set_regenerate_available(True)
    assert panel._regenerate_btn.isEnabled()


# --------------------------------------------------------------------------- #
# shell navigation + run-lockout
# --------------------------------------------------------------------------- #
def test_main_window_navigates_to_audio_page() -> None:
    window = MainWindow()
    assert window._stack.currentIndex() == 0
    window.show_audio_page()
    assert window._stack.currentIndex() == 4
    assert window._stack.currentWidget() is window.audio_panel
    window.show_shell_page()
    assert window._stack.currentIndex() == 0


def test_audio_button_enabled_only_with_rendered_audio_and_locked_out_while_running() -> None:
    window = MainWindow()
    assert not window._audio_btn.isEnabled()  # no project yet

    window.set_audio_available(True)
    assert window._audio_btn.isEnabled()

    window.set_running(True)  # run-lockout disables the entry point
    assert not window._audio_btn.isEnabled()

    window.set_running(False)
    window.set_audio_available(True)  # ``_refresh_status`` re-enables after a run
    assert window._audio_btn.isEnabled()

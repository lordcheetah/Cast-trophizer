"""Loop-free unit tests for :class:`AudioPresenter` — no Qt, no event loop, no GPU/TTS.

A :class:`FakeAudioView` records what the presenter pushes; a :class:`FakeRenderExecutor` stands in
for the worker (invoking its callbacks synchronously to model the main-thread delivery); a **real**
shared :class:`~casttrophizer.review.service.ReviewService` mutates an in-memory project on disk.
Covers: approve (COMPLETED->APPROVED, persisted); the full regenerate flow (new seed, reset key,
popped SYNTHESIZE+ASSEMBLE, executor invoked with the seed-in-params, commit + auto-play on finish);
the single-flight guard; graceful-unavailable (defer, no executor) and on_failed degrade; play; and
the filter / selection nav.
"""

from __future__ import annotations

import wave
from collections.abc import Callable
from pathlib import Path

import pytest

from casttrophizer.app_service import AppServiceDeps
from casttrophizer.audio.loudness import LoudnessSettings
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    VoiceClip,
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.providers import TTSProvider
from casttrophizer.review.audio_view import AudioSegmentRow
from casttrophizer.review.service import ReviewService
from casttrophizer.ui import audio_presenter as audio_presenter_module
from casttrophizer.ui.audio_presenter import AudioPresenter, AudioView, SegmentRenderExecutor
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeTTSProvider


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeAudioView:
    """Records every call the presenter makes (structurally an :class:`AudioView`)."""

    def __init__(self) -> None:
        self.segments: list[list[AudioSegmentRow]] = []
        self.progress: list[tuple[int, int]] = []
        self.selected: list[int] = []
        self.played: list[str] = []
        self.stopped = 0
        self.regenerate_available: list[bool] = []
        self.busy: list[bool] = []
        self.errors: list[tuple[str, str]] = []

    def show_segments(self, rows: list[AudioSegmentRow]) -> None:
        self.segments.append(rows)

    def show_progress(self, approved: int, rendered: int) -> None:
        self.progress.append((approved, rendered))

    def select_segment(self, index: int) -> None:
        self.selected.append(index)

    def play_clip(self, path: str) -> None:
        self.played.append(path)

    def stop_playback(self) -> None:
        self.stopped += 1

    def set_regenerate_available(self, available: bool) -> None:
        self.regenerate_available.append(available)

    def set_busy(self, busy: bool) -> None:
        self.busy.append(busy)

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))


class FakeRenderExecutor:
    """Records ``start`` args; runs the real render (or a canned failure) synchronously.

    ``mode="finish"`` calls ``on_finished`` after actually running the (fake) TTS render so the
    segment is stamped COMPLETED in memory — exactly what the Qt worker does before the queued
    finish signal. ``mode="fail"`` calls ``on_failed`` without rendering.
    """

    def __init__(
        self, tts: TTSProvider, *, mode: str = "finish", provider_unavailable: bool = True
    ) -> None:
        self._tts = tts
        self._mode = mode
        self._provider_unavailable = provider_unavailable
        self.calls: list[dict[str, object]] = []
        self.started = 0

    def start(
        self,
        tts: TTSProvider,
        project: Project,
        segment: Segment,
        cache: AudioCache,
        *,
        loudness: LoudnessSettings | None,
        on_finished: Callable[[], None],
        on_failed: Callable[[str, bool], None],
    ) -> None:
        self.started += 1
        self.calls.append(
            {
                "tts": tts,
                "project": project,
                "segment": segment,
                "cache": cache,
                "loudness": loudness,
                "seed": segment.audio_seed,
            }
        )
        if self._mode == "fail":
            on_failed("boom", self._provider_unavailable)
            return
        # Model the worker: actually render (stamps the segment COMPLETED), then fire on_finished.
        from casttrophizer.audio.synthesize import render_segment

        render_segment(project, segment, cache, tts, loudness=loudness)
        on_finished()


def _wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 128)
    return str(path)


def _project(store: WorkspaceStore, voice_clip: Path) -> Project:
    """A synthesized project: narrator voiced, two COMPLETED segments with on-disk WAVs."""
    clip = VoiceClip(id=new_id("voice"), source_path=str(voice_clip), label="Narrator")
    narrator = Speaker(
        id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR, voice_clip_id=clip.id
    )

    def _seg(text: str) -> Segment:
        return Segment(
            id=new_id("seg"),
            text=text,
            speaker_id=narrator.id,
            role=SpeakerRole.NARRATOR,
            confidence=1.0,
            review_status=ReviewStatus.APPROVED,
            audio_status=ReviewStatus.COMPLETED,
        )

    seg_a = _seg("First line.")
    seg_b = _seg("Second line.")
    ch_id = new_id("ch")
    line = Line(id=new_id("line"), chapter_id=ch_id, order=0, text="x", segments=[seg_a, seg_b])
    chapter = Chapter(id=ch_id, order=0, title="One", lines=[line])
    book = Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter])
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Audio",
        workspace_dir=str(store.layout.root),
        book=book,
        speakers=[narrator],
        voice_clips=[clip],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
            str(StageName.REVIEW): ReviewStatus.COMPLETED,
            str(StageName.SYNTHESIZE): ReviewStatus.COMPLETED,
            str(StageName.ASSEMBLE): ReviewStatus.COMPLETED,
        },
        tts_params={"seed": 7},
    )
    cache = AudioCache(store.layout)
    for seg in (seg_a, seg_b):
        key = cache.key_for(seg, project)
        _wav(cache.path_for_key(key))
        seg.audio_cache_key = key
    store.save(project)
    return project


def _presenter(
    store: WorkspaceStore,
    *,
    executor: SegmentRenderExecutor,
    tts: TTSProvider,
) -> tuple[AudioPresenter, FakeAudioView, list[int]]:
    view = FakeAudioView()
    reviewed: list[int] = []
    deps = AppServiceDeps(tts_factory=lambda _cfg: tts)
    presenter = AudioPresenter(
        view=view, executor=executor, deps=deps, on_reviewed=lambda: reviewed.append(1)
    )
    presenter.attach(ReviewService(store, store.load()), AudioCache(store.layout))
    return presenter, view, reviewed


def _seg_id(project: Project, text: str) -> str:
    return next(
        s.id
        for ch in project.book.chapters
        for ln in ch.lines
        for s in ln.segments
        if s.text == text
    )


@pytest.fixture
def _tts_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``tts_extra_available`` True in the presenter module (CI has no chatterbox)."""
    monkeypatch.setattr(audio_presenter_module, "tts_extra_available", lambda: True)


# --------------------------------------------------------------------------- #
# structural / lifecycle
# --------------------------------------------------------------------------- #
def test_structural_protocol_conformance() -> None:
    assert isinstance(FakeAudioView(), AudioView)
    assert isinstance(FakeRenderExecutor(FakeTTSProvider()), SegmentRenderExecutor)


def test_attach_stops_playback_and_probes_availability(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    _project(tmp_workspace, fake_voice_clips[0])
    _, view, _ = _presenter(
        tmp_workspace, executor=FakeRenderExecutor(FakeTTSProvider()), tts=FakeTTSProvider()
    )
    assert view.stopped == 1  # project switch resets the player
    assert view.regenerate_available  # a probe happened (True or False per environment)


def test_open_renders_rows_and_progress(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    _project(tmp_workspace, fake_voice_clips[0])
    presenter, view, _ = _presenter(
        tmp_workspace, executor=FakeRenderExecutor(FakeTTSProvider()), tts=FakeTTSProvider()
    )
    presenter.open()
    # Default filter is unapproved-only: both COMPLETED (not yet approved) rows show.
    assert {r.text for r in view.segments[-1]} == {"First line.", "Second line."}
    assert view.progress[-1] == (0, 2)  # 0 approved of 2 rendered


# --------------------------------------------------------------------------- #
# approve
# --------------------------------------------------------------------------- #
def test_approve_flips_completed_to_approved_and_persists(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    presenter, view, reviewed = _presenter(
        tmp_workspace, executor=FakeRenderExecutor(FakeTTSProvider()), tts=FakeTTSProvider()
    )
    presenter.open()
    sid = _seg_id(project, "First line.")

    presenter.approve(sid)

    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)
    assert seg.audio_status == ReviewStatus.APPROVED
    assert reviewed == [1]
    reloaded = next(
        s for s in tmp_workspace.load().book.chapters[0].lines[0].segments if s.id == sid
    )
    assert reloaded.audio_status == ReviewStatus.APPROVED
    # progress now shows 1 approved of 2 rendered
    assert view.progress[-1] == (1, 2)


# --------------------------------------------------------------------------- #
# regenerate (render-now)
# --------------------------------------------------------------------------- #
def test_regenerate_rerolls_renders_commits_and_autoplays(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], _tts_available: None
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    tts = FakeTTSProvider()
    executor = FakeRenderExecutor(tts)
    presenter, view, reviewed = _presenter(tmp_workspace, executor=executor, tts=tts)
    presenter.open()
    sid = _seg_id(project, "First line.")
    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)
    old_seed = seg.audio_seed

    presenter.regenerate(sid)

    # Re-roll: a NEW seed, and the render stages were re-opened.
    assert seg.audio_seed is not None and seg.audio_seed != old_seed
    assert str(StageName.SYNTHESIZE) not in live.stage_status
    assert str(StageName.ASSEMBLE) not in live.stage_status
    # The executor was invoked with the right (project, segment, cache) and the seed in params.
    assert executor.started == 1
    call = executor.calls[-1]
    assert call["segment"] is seg
    assert call["project"] is live
    assert call["seed"] == seg.audio_seed
    assert tts.synthesize_calls[-1].params["seed"] == seg.audio_seed
    # Busy toggled on then off; the WAV handle was released before the render.
    assert view.busy[:2] == [True, False]
    # On finish: committed COMPLETED and auto-played the fresh take.
    assert seg.audio_status == ReviewStatus.COMPLETED
    reloaded = next(
        s for s in tmp_workspace.load().book.chapters[0].lines[0].segments if s.id == sid
    )
    assert reloaded.audio_status == ReviewStatus.COMPLETED
    assert reloaded.audio_seed == seg.audio_seed  # persisted after the worker render
    assert view.played  # auto-played
    assert reviewed  # shell ticked


def test_regenerate_single_flight_ignores_second_call(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], _tts_available: None
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    tts = FakeTTSProvider()

    # An executor that captures the callbacks but does NOT fire them, so ``_rendering`` stays True.
    class _HangingExecutor:
        def __init__(self) -> None:
            self.started = 0

        def start(self, *a: object, **k: object) -> None:
            self.started += 1

    executor = _HangingExecutor()
    presenter, view, _ = _presenter(tmp_workspace, executor=executor, tts=tts)  # type: ignore[arg-type]
    presenter.open()
    sid = _seg_id(project, "First line.")

    presenter.regenerate(sid)  # starts (and hangs) the render
    presenter.regenerate(sid)  # second call must be a no-op (single-flight)

    assert executor.started == 1


def test_regenerate_unavailable_defers_without_executor(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    monkeypatch.setattr(audio_presenter_module, "tts_extra_available", lambda: False)
    tts = FakeTTSProvider()
    executor = FakeRenderExecutor(tts)
    presenter, view, reviewed = _presenter(tmp_workspace, executor=executor, tts=tts)
    presenter.open()
    sid = _seg_id(project, "First line.")
    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)

    presenter.regenerate(sid)

    # Re-rolled + deferred: PENDING, render stages re-opened, but NO worker was started.
    assert seg.audio_status == ReviewStatus.PENDING
    assert seg.audio_seed is not None
    assert executor.started == 0
    assert False in view.regenerate_available  # disabled
    assert view.errors  # an info message shown
    assert reviewed  # shell ticked


def test_provider_unavailable_failure_stamps_failed_and_disables_regenerate(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], _tts_available: None
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    tts = FakeTTSProvider()
    # A TTSProviderError -> provider_unavailable=True.
    executor = FakeRenderExecutor(tts, mode="fail", provider_unavailable=True)
    presenter, view, reviewed = _presenter(tmp_workspace, executor=executor, tts=tts)
    presenter.open()
    sid = _seg_id(project, "First line.")
    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)

    presenter.regenerate(sid)

    # Provider failure: the segment is stamped FAILED (stays VISIBLE, re-renders next Run), busy
    # cleared, Regenerate PERMANENTLY disabled, error shown, and on_reviewed fired.
    assert seg.audio_status == ReviewStatus.FAILED
    assert view.busy[-1] is False
    assert False in view.regenerate_available
    assert view.errors
    assert reviewed  # shell ticked (render stages re-opened)
    # The failed row is still in the list (FAILED is reviewable), not a silent black hole.
    assert any(r.segment_id == sid and r.is_failed for r in view.segments[-1])
    # Persisted FAILED on disk.
    reloaded = next(
        s for s in tmp_workspace.load().book.chapters[0].lines[0].segments if s.id == sid
    )
    assert reloaded.audio_status == ReviewStatus.FAILED


def test_transient_failure_stamps_failed_but_keeps_regenerate_enabled(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], _tts_available: None
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    tts = FakeTTSProvider()
    # A generic (non-TTSProviderError) failure -> provider_unavailable=False (retryable).
    executor = FakeRenderExecutor(tts, mode="fail", provider_unavailable=False)
    presenter, view, _ = _presenter(tmp_workspace, executor=executor, tts=tts)
    presenter.open()
    sid = _seg_id(project, "First line.")
    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)

    presenter.regenerate(sid)

    # Transient failure: segment FAILED + visible, error shown, but Regenerate STAYS enabled so the
    # user can retry — set_regenerate_available(False) must NOT be called by the failure path.
    assert seg.audio_status == ReviewStatus.FAILED
    assert view.errors
    assert False not in view.regenerate_available  # never disabled on a transient error


def test_executor_start_raising_resets_busy_and_marks_failed(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], _tts_available: None
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    tts = FakeTTSProvider()

    class _RaisingExecutor:
        started = 0

        def start(self, *a: object, **k: object) -> None:
            _RaisingExecutor.started += 1
            raise RuntimeError("a segment render is already in progress")

    presenter, view, _ = _presenter(
        tmp_workspace, executor=_RaisingExecutor(), tts=tts  # type: ignore[arg-type]
    )
    presenter.open()
    sid = _seg_id(project, "First line.")
    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)

    presenter.regenerate(sid)

    # A start failure must not wedge the panel busy: busy reset, segment FAILED (visible), error
    # shown, Regenerate left ENABLED (a start failure is not a provider outage), and not stuck.
    assert view.busy[-1] is False
    assert seg.audio_status == ReviewStatus.FAILED
    assert view.errors
    assert False not in view.regenerate_available
    assert not presenter._rendering  # single-flight guard cleared -> a retry can start


def test_play_and_approve_unaffected_when_regenerate_unavailable(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    monkeypatch.setattr(audio_presenter_module, "tts_extra_available", lambda: False)
    tts = FakeTTSProvider()
    presenter, view, reviewed = _presenter(tmp_workspace, executor=FakeRenderExecutor(tts), tts=tts)
    presenter.open()
    sid = _seg_id(project, "First line.")

    presenter.play(sid)
    assert view.played  # play works without a provider

    presenter.approve(sid)
    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)
    assert seg.audio_status == ReviewStatus.APPROVED  # approve works without a provider


# --------------------------------------------------------------------------- #
# play
# --------------------------------------------------------------------------- #
def test_play_fires_play_clip_with_the_wav_path(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    presenter, view, _ = _presenter(
        tmp_workspace, executor=FakeRenderExecutor(FakeTTSProvider()), tts=FakeTTSProvider()
    )
    presenter.open()
    sid = _seg_id(project, "First line.")
    cache = AudioCache(tmp_workspace.layout)
    seg = next(s for s in project.book.chapters[0].lines[0].segments if s.id == sid)

    presenter.play(sid)

    assert view.played[-1] == str(cache.path_for_key(seg.audio_cache_key))  # type: ignore[arg-type]


def test_play_missing_wav_shows_error_not_play(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    presenter, view, _ = _presenter(
        tmp_workspace, executor=FakeRenderExecutor(FakeTTSProvider()), tts=FakeTTSProvider()
    )
    presenter.open()
    # Remove the WAV so the segment is non-playable.
    sid = _seg_id(project, "First line.")
    live = presenter._service.project  # type: ignore[union-attr]
    seg = next(s for s in live.book.chapters[0].lines[0].segments if s.id == sid)
    AudioCache(tmp_workspace.layout).path_for_key(seg.audio_cache_key).unlink()  # type: ignore[arg-type]

    presenter.play(sid)

    assert view.played == []
    assert view.errors


# --------------------------------------------------------------------------- #
# filter / selection nav
# --------------------------------------------------------------------------- #
def test_filter_toggles_between_unapproved_only_and_all(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    project = _project(tmp_workspace, fake_voice_clips[0])
    presenter, view, _ = _presenter(
        tmp_workspace, executor=FakeRenderExecutor(FakeTTSProvider()), tts=FakeTTSProvider()
    )
    presenter.open()
    sid = _seg_id(project, "First line.")
    presenter.approve(sid)  # First line now APPROVED

    # Unapproved-only (default): only Second line shows.
    assert {r.text for r in view.segments[-1]} == {"Second line."}
    presenter.set_filter(False)
    assert {r.text for r in view.segments[-1]} == {"First line.", "Second line."}


def test_selection_nav_wraps_and_play_next_plays(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    _project(tmp_workspace, fake_voice_clips[0])
    presenter, view, _ = _presenter(
        tmp_workspace, executor=FakeRenderExecutor(FakeTTSProvider()), tts=FakeTTSProvider()
    )
    presenter.open()

    presenter.next_segment()
    assert view.selected[-1] == 0
    presenter.next_segment()
    assert view.selected[-1] == 1
    presenter.next_segment()
    assert view.selected[-1] == 0  # wraps

    presenter.play_next()  # advances to index 1 and plays it
    assert view.selected[-1] == 1
    assert view.played

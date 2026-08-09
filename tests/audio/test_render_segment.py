"""Unit tests for the single-segment audio-review render :func:`audio.synthesize.render_segment`.

Offline: the deterministic :class:`FakeTTSProvider` writes a silent WAV, a real
:class:`AudioCache` over a temp workspace resolves the target path. Covers: renders to
``cache.path_for_key(key)`` + stamps ``audio_cache_key``/COMPLETED; threads the segment's
``audio_seed`` into ``params["seed"]`` (overriding the global) for BOTH the re-rolled and
un-re-rolled cases; a missing voice clip raises ``TTSProviderError`` with no stamp; a
``TTSProviderError`` from the provider propagates with no partial stamp; loudness normalization is
applied to the just-rendered WAV.
"""

from __future__ import annotations

import pytest

from casttrophizer.audio.loudness import LoudnessSettings
from casttrophizer.audio.synthesize import render_segment
from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.models import Project, Segment
from casttrophizer.errors import TTSProviderError
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeTTSProvider


def _alice_segment(project: Project) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text == '"Hello,"'
    )


def test_render_segment_writes_wav_and_stamps_completed(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    project = synthesize_ready_project
    cache = AudioCache(tmp_workspace.layout)
    seg = _alice_segment(project)
    tts = FakeTTSProvider()

    render_segment(project, seg, cache, tts, loudness=None)

    key = cache.key_for(seg, project)
    assert seg.audio_cache_key == key
    assert seg.audio_status == ReviewStatus.COMPLETED
    assert cache.has(key)  # WAV written at the key path
    assert len(tts.synthesize_calls) == 1


def test_render_segment_uses_global_seed_when_not_re_rolled(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    project = synthesize_ready_project  # tts_params has seed=7
    cache = AudioCache(tmp_workspace.layout)
    seg = _alice_segment(project)  # audio_seed defaults to None
    tts = FakeTTSProvider()

    render_segment(project, seg, cache, tts, loudness=None)

    assert tts.synthesize_calls[-1].params["seed"] == 7  # the global seed, no override


def test_render_segment_threads_re_rolled_seed_into_params(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    project = synthesize_ready_project
    cache = AudioCache(tmp_workspace.layout)
    seg = _alice_segment(project)
    seg.audio_seed = 555  # a re-roll pins a per-segment seed
    tts = FakeTTSProvider()

    render_segment(project, seg, cache, tts, loudness=None)

    # The re-rolled seed overrides the global one, and the render lands at the seed-folded key.
    assert tts.synthesize_calls[-1].params["seed"] == 555
    assert seg.audio_cache_key == cache.key_for(seg, project)


def test_render_segment_missing_clip_raises_without_stamp(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    project = synthesize_ready_project
    cache = AudioCache(tmp_workspace.layout)
    seg = _alice_segment(project)
    # Strip Alice's voice so clip resolution fails.
    alice = next(sp for sp in project.speakers if sp.id == seg.speaker_id)
    alice.voice_clip_id = None
    tts = FakeTTSProvider()

    with pytest.raises(TTSProviderError):
        render_segment(project, seg, cache, tts, loudness=None)

    assert seg.audio_status == ReviewStatus.PENDING  # no partial stamp
    assert seg.audio_cache_key is None
    assert tts.synthesize_calls == []  # never reached the provider


def test_render_segment_provider_error_propagates_without_partial_stamp(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    project = synthesize_ready_project
    cache = AudioCache(tmp_workspace.layout)
    seg = _alice_segment(project)
    tts = FakeTTSProvider(fail_for={seg.text})

    with pytest.raises(TTSProviderError):
        render_segment(project, seg, cache, tts, loudness=None)

    assert seg.audio_status == ReviewStatus.PENDING  # render_segment never partially stamps
    assert seg.audio_cache_key is None


def test_render_segment_applies_loudness(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    from casttrophizer.audio.wavfile import read_wav_mono_float

    project = synthesize_ready_project
    cache = AudioCache(tmp_workspace.layout)
    seg = _alice_segment(project)
    tts = FakeTTSProvider(loud=True)  # writes a constant-amplitude (peak 0.9) WAV
    loudness = LoudnessSettings(enabled=True, target_lufs=-23.0)

    render_segment(project, seg, cache, tts, loudness=loudness)

    key = cache.key_for(seg, project)
    samples, _ = read_wav_mono_float(cache.path_for_key(key))
    peak = max(abs(s) for s in samples)
    assert peak < 0.9  # normalized down toward target (not left at the raw loud amplitude)

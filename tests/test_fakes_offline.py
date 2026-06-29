"""The test doubles satisfy the provider ABCs and run fully offline (scaffold §6, §9.8).

These guard the harness itself: if a fake stops conforming to the ABC, every test built
on it is suspect. We also exercise the offline synth path (FakeTTS -> AudioCache) and the
low-confidence attribution surfacing (FakeLLM) so the review path the pipeline depends on
is demonstrably reachable without a real model.
"""

from __future__ import annotations

import wave
from pathlib import Path

from casttrophizer.providers.base import (
    LLMMessage,
    LLMProvider,
    SynthesisRequest,
    TTSProvider,
)
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeLLMProvider, FakeTTSProvider


def test_fakes_conform_to_abcs(fake_llm: FakeLLMProvider, fake_tts: FakeTTSProvider) -> None:
    assert isinstance(fake_llm, LLMProvider)
    assert isinstance(fake_tts, TTSProvider)
    assert fake_llm.is_available() is True
    assert fake_tts.is_available() is True


def test_fake_llm_surfaces_a_low_confidence_candidate(fake_llm: FakeLLMProvider) -> None:
    # The pipeline must be able to exercise the "low-confidence -> review" path offline.
    candidates = fake_llm.attribute_speakers(
        context='"Hello," said Alice.',
        candidates=["seg-1", "seg-2"],
        known_speakers=["Alice"],
    )
    assert [c.segment_id for c in candidates] == ["seg-1", "seg-2"]
    low = [c for c in candidates if c.confidence < 0.5]
    assert low, "fake LLM must yield at least one low-confidence candidate for review"


def test_fake_llm_complete_is_deterministic() -> None:
    llm = FakeLLMProvider(completion="canned")
    out = llm.complete([LLMMessage(role="user", content="anything")])
    assert out == "canned"
    assert llm.complete([LLMMessage(role="user", content="other")]) == "canned"


def test_fake_tts_writes_valid_wav_into_audio_cache(
    tmp_workspace: WorkspaceStore, fake_tts: FakeTTSProvider, fake_voice_clips: list[Path]
) -> None:
    # Synthesize to the AudioCache path keyed for a segment; the result must be a readable
    # WAV on disk and the cache must then report a hit. No real model, no network.
    cache = AudioCache(tmp_workspace.layout)
    key = AudioCache.compute_key("hello", "voice-1", {"seed": 1})
    out_path = cache.path_for_key(key)
    assert not cache.has(key)

    result = fake_tts.synthesize(
        SynthesisRequest(text="hello", voice_clip_path=fake_voice_clips[0], params={"seed": 1}),
        out_path,
    )

    assert result.audio_path == out_path
    assert cache.has(key)
    with wave.open(str(out_path), "rb") as wav:
        assert wav.getframerate() == result.sample_rate
        assert wav.getnframes() > 0
    assert fake_tts.synthesize_calls and fake_tts.synthesize_calls[0].text == "hello"

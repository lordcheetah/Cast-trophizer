"""Offline fake TTS provider for tests.

Writes a tiny silent WAV via the stdlib ``wave`` module and returns a fixed sample rate
and duration. No model load, no audio dependencies, no network.
"""

from __future__ import annotations

import wave
from pathlib import Path

from casttrophizer.providers.base import SynthesisRequest, SynthesisResult, TTSProvider

__all__ = ["FakeTTSProvider"]

_SAMPLE_RATE = 22050
_DURATION_S = 0.1


class FakeTTSProvider(TTSProvider):
    """Writes a short silent WAV instead of synthesizing real speech."""

    name = "fake-tts"

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.synthesize_calls: list[SynthesisRequest] = []

    def synthesize(self, request: SynthesisRequest, out_path: Path) -> SynthesisResult:
        self.synthesize_calls.append(request)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        n_frames = int(_SAMPLE_RATE * _DURATION_S)
        with wave.open(str(out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(_SAMPLE_RATE)
            wav.writeframes(b"\x00\x00" * n_frames)
        return SynthesisResult(
            audio_path=out_path,
            sample_rate=_SAMPLE_RATE,
            duration_s=_DURATION_S,
        )

    def is_available(self) -> bool:
        return self._available

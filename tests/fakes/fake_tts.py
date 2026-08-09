"""Offline fake TTS provider for tests.

Writes a tiny silent WAV via the stdlib ``wave`` module and returns a fixed sample rate
and duration. No model load, no audio dependencies, no network. ``fail_for`` / ``fail_all``
force deterministic per-segment and all-failed synthesis failures for the resilience tests.
``loud=True`` instead writes a constant-amplitude WAV (peak ``_LOUD_AMPLITUDE``) so the
loudness-normalization stage tests can assert the rendered file is attenuated toward target.
"""

from __future__ import annotations

import wave
from collections.abc import Iterable
from pathlib import Path

from casttrophizer.audio.wavfile import write_wav_mono16
from casttrophizer.errors import TTSProviderError
from casttrophizer.providers.base import SynthesisRequest, SynthesisResult, TTSProvider

__all__ = ["FakeTTSProvider"]

_SAMPLE_RATE = 22050
_DURATION_S = 0.1
#: Peak amplitude for the ``loud=True`` mode. A constant (DC) signal always takes the
#: deterministic RMS fallback in ``normalize_samples`` (K-weighting filters DC to silence),
#: so this test signal is independent of whether ``pyloudnorm`` is installed.
_LOUD_AMPLITUDE = 0.9


class FakeTTSProvider(TTSProvider):
    """Writes a short silent WAV instead of synthesizing real speech.

    ``synthesize_calls`` records every request (in order) for call-count and voice-path
    assertions. ``fail_for`` is a set of exact segment texts whose ``synthesize`` raises
    :class:`~casttrophizer.errors.TTSProviderError`; ``fail_all`` raises for every request.
    A raising call is still recorded in ``synthesize_calls`` (the attempt happened) but writes
    no file.
    """

    name = "fake-tts"

    def __init__(
        self,
        *,
        available: bool = True,
        fail_for: Iterable[str] | None = None,
        fail_all: bool = False,
        loud: bool = False,
    ) -> None:
        self._available = available
        self._fail_for: set[str] = set(fail_for or ())
        self._fail_all = fail_all
        self._loud = loud
        self.synthesize_calls: list[SynthesisRequest] = []

    def synthesize(self, request: SynthesisRequest, out_path: Path) -> SynthesisResult:
        self.synthesize_calls.append(request)
        if self._fail_all or request.text in self._fail_for:
            raise TTSProviderError(f"fake synth failure for {request.text!r}")
        n_frames = int(_SAMPLE_RATE * _DURATION_S)
        if self._loud:
            write_wav_mono16(out_path, [_LOUD_AMPLITUDE] * n_frames, _SAMPLE_RATE)
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
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

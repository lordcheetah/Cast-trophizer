"""Stdlib-only 16-bit PCM mono WAV I/O — the single home for our WAV read/write.

Every WAV Cast-trophizer *produces* is mono, 16-bit PCM (the TTS providers write it, the
assemble stage reads it for chapter timing). Keeping that read/write in one dependency-light
module lets the Chatterbox provider *and* the loudness normalizer share the exact same
quantization, with no ``torchaudio``/``numpy`` dependency.

Why not ``torchaudio.save``: torchaudio >= ~2.9 dispatches ``save`` to TorchCodec, a separate
native dependency that is not part of the ``tts`` extra (it raises ``ImportError: TorchCodec
is required``). A stdlib ``wave`` write avoids that entirely.
"""

from __future__ import annotations

import sys
import wave
from array import array
from collections.abc import Sequence
from pathlib import Path

__all__ = ["read_wav_mono_float", "write_wav_mono16"]


def _to_pcm16(sample: float) -> int:
    """Clamp a float sample in [-1, 1] and scale to a signed 16-bit PCM integer."""
    value = int(sample * 32767.0)
    if value < -32768:
        return -32768
    if value > 32767:
        return 32767
    return value


def write_wav_mono16(path: Path, samples: Sequence[float], sample_rate: int) -> None:
    """Write ``samples`` (float, mono) as a 16-bit PCM WAV via the stdlib ``wave`` module.

    Samples are clamped to [-1, 1] and scaled to signed 16-bit. The parent directory is
    created if needed. This is the one writer shared by the Chatterbox provider and the
    loudness normalizer.
    """
    pcm = array("h", (_to_pcm16(s) for s in samples))
    if sys.byteorder == "big":  # WAV is little-endian; array is native-endian
        pcm.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())


def read_wav_mono_float(path: Path) -> tuple[list[float], int]:
    """Read a 16-bit PCM mono WAV back to a float list in [-1, 1] plus its sample rate.

    Raises :class:`ValueError` for a WAV that is not mono 16-bit PCM (we only ever produce
    mono16, so any other shape is a caller / format mismatch the normalizer treats as
    "leave untouched"). Real I/O / malformed-file errors propagate as :class:`wave.Error`
    or :class:`OSError`.
    """
    with wave.open(str(path), "rb") as wav_file:
        n_channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        n_frames = wav_file.getnframes()
        if n_channels != 1 or sample_width != 2:
            raise ValueError(
                f"expected mono 16-bit PCM WAV, got channels={n_channels} width={sample_width}"
            )
        raw = wav_file.readframes(n_frames)

    pcm = array("h")
    pcm.frombytes(raw)
    if sys.byteorder == "big":  # WAV is little-endian; array is native-endian
        pcm.byteswap()
    samples = [value / 32768.0 for value in pcm]
    return samples, sample_rate

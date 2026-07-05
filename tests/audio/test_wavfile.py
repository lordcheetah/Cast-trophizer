"""Unit tests for the stdlib-only mono16 WAV I/O (``casttrophizer.audio.wavfile``).

Offline, dependency-light: everything here uses the stdlib ``wave`` module directly to build
the malformed inputs the reader must reject, and the module under test to build the happy-path
WAVs. No numpy/torchaudio. These guard the exact quantization + header contract that both the
Chatterbox provider and the loudness normalizer depend on.
"""

from __future__ import annotations

import wave
from array import array
from pathlib import Path

import pytest

from casttrophizer.audio.wavfile import read_wav_mono_float, write_wav_mono16

_SR = 16000

# The writer scales by 32767 and truncates toward zero; the reader divides by 32768. That
# 32767-write / 32768-read asymmetry means the worst-case round-trip error for a large-amplitude
# sample is just under 2 LSB (2 / 32768), not 1 LSB. That is the true bound, well below audibility
# for 16-bit PCM, so we assert against it rather than an idealized 1-LSB bound.
_QUANT_TOL = 2.0 / 32768.0


def test_round_trip_within_quantization_error(tmp_path: Path) -> None:
    path = tmp_path / "rt.wav"
    original = [0.0, 0.25, -0.25, 0.5, -0.5, 0.9, -0.9, 0.123456, -0.987654]
    write_wav_mono16(path, original, _SR)

    read_back, sr = read_wav_mono_float(path)

    assert sr == _SR
    assert len(read_back) == len(original)
    for src, got in zip(original, read_back, strict=True):
        assert abs(src - got) <= _QUANT_TOL, f"{src} -> {got} exceeds quantization tolerance"


def test_written_wav_has_correct_header(tmp_path: Path) -> None:
    path = tmp_path / "hdr.wav"
    samples = [0.1, -0.2, 0.3, -0.4, 0.5]
    write_wav_mono16(path, samples, _SR)

    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2  # 16-bit
        assert wav.getframerate() == _SR
        assert wav.getnframes() == len(samples)


def test_out_of_range_samples_are_clamped(tmp_path: Path) -> None:
    # Values outside [-1, 1] must clamp to the 16-bit rails, never wrap/overflow.
    path = tmp_path / "clamp.wav"
    write_wav_mono16(path, [2.0, -2.0, 1.5, -1.5], _SR)
    read_back, _ = read_wav_mono_float(path)
    # +full-scale reads back at 32767/32768; -full-scale at -32768/32768 == -1.0 exactly.
    assert read_back[0] == pytest.approx(32767 / 32768.0)
    assert read_back[1] == -1.0
    assert max(read_back) <= 32767 / 32768.0
    assert min(read_back) >= -1.0


def test_read_rejects_stereo_wav(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)  # not mono
        wav.setsampwidth(2)
        wav.setframerate(_SR)
        wav.writeframes(array("h", [0, 0, 0, 0]).tobytes())

    with pytest.raises(ValueError):
        read_wav_mono_float(path)


def test_read_rejects_non_16bit_wav(tmp_path: Path) -> None:
    path = tmp_path / "eight_bit.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)  # 8-bit, not 16-bit
        wav.setframerate(_SR)
        wav.writeframes(b"\x80\x80\x80\x80")

    with pytest.raises(ValueError):
        read_wav_mono_float(path)


def test_empty_samples_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "empty.wav"
    write_wav_mono16(path, [], _SR)
    read_back, sr = read_wav_mono_float(path)
    assert read_back == []
    assert sr == _SR

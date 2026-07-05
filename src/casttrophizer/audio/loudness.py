"""Per-segment loudness normalization (ITU-R BS.1770 with a pure-Python RMS fallback).

Runs in the synthesize *orchestration* (see :mod:`casttrophizer.audio.synthesize`), not the
TTS provider: each just-rendered mono16 WAV is re-read, its float samples are normalized to a
consistent target loudness, and rewritten in place. This is provider-agnostic (works for any
future TTS provider *and* the offline ``FakeTTSProvider``) and fully offline-unit-testable on
synthetic signals.

Heavy deps stay lazy: ``numpy`` and ``pyloudnorm`` are imported **inside**
:func:`_measure_loudness` only, so importing this module is cheap and CI installs without the
``tts`` extra (and possibly without numpy) never need them. When those deps are absent, the
segment is shorter than the BS.1770 gating block, or the measurement is non-finite, a
pure-Python RMS-to-target fallback runs instead — that is the deterministic path the unit
tests assert against. The real ``pyloudnorm`` measurement is marked ``# VERIFY:`` (confirmed
only on a real render, like the Chatterbox ``generate`` call).

The settings live in ``project.tts_params["loudness"]`` so the existing
:meth:`~casttrophizer.workspace.audio_cache.AudioCache.compute_key` folds them into the
per-segment cache key — enabling normalization (or changing the target) re-renders exactly the
affected segments. No ``schema_version`` bump: the block rides along in the free-form dict.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from casttrophizer.audio.wavfile import read_wav_mono_float, write_wav_mono16
from casttrophizer.config import (
    DEFAULT_LOUDNESS_ENABLED,
    DEFAULT_LOUDNESS_MAX_GAIN_DB,
    DEFAULT_LOUDNESS_PEAK_DBFS,
    DEFAULT_LOUDNESS_TARGET_LUFS,
)

if TYPE_CHECKING:
    from casttrophizer.config import AppConfig

__all__ = [
    "DEFAULT_TARGET_LUFS",
    "DEFAULT_PEAK_CEILING_DBFS",
    "DEFAULT_MAX_GAIN_DB",
    "LoudnessSettings",
    "normalize_samples",
    "normalize_wav_file",
]

#: Public aliases of the canonical config defaults (single source of truth in ``config``).
DEFAULT_TARGET_LUFS = DEFAULT_LOUDNESS_TARGET_LUFS
DEFAULT_PEAK_CEILING_DBFS = DEFAULT_LOUDNESS_PEAK_DBFS
DEFAULT_MAX_GAIN_DB = DEFAULT_LOUDNESS_MAX_GAIN_DB

#: BS.1770 integrated loudness needs at least one ~0.4 s gating block; below this the real
#: ``pyloudnorm`` measurement is skipped and the RMS fallback is used.
_MIN_BLOCK_S = 0.4
#: Peak at/below this is treated as digital silence: normalization leaves it untouched
#: (avoids ``-inf`` LUFS and divide-by-zero).
_SILENCE_PEAK_EPS = 1e-4


@dataclass(frozen=True)
class LoudnessSettings:
    """Resolved per-segment loudness-normalization settings.

    Constructed from :class:`~casttrophizer.config.AppConfig` at project-creation time
    (:meth:`from_config`), serialized into ``project.tts_params["loudness"]``
    (:meth:`to_params`), and read back out in the orchestration (:meth:`from_params`).
    """

    enabled: bool = DEFAULT_LOUDNESS_ENABLED
    target_lufs: float = DEFAULT_TARGET_LUFS
    peak_ceiling_dbfs: float = DEFAULT_PEAK_CEILING_DBFS
    max_gain_db: float = DEFAULT_MAX_GAIN_DB

    @classmethod
    def from_config(cls, config: AppConfig) -> LoudnessSettings:
        """Build settings from an :class:`AppConfig` (the CLI seeds a new project with this)."""
        return cls(
            enabled=config.loudness_enabled,
            target_lufs=config.loudness_target_lufs,
            peak_ceiling_dbfs=config.loudness_peak_dbfs,
            max_gain_db=config.loudness_max_gain_db,
        )

    @classmethod
    def from_params(cls, tts_params: Mapping[str, Any]) -> LoudnessSettings | None:
        """Read the ``"loudness"`` block out of ``tts_params``, or ``None`` if absent.

        ``None`` means the project carries no loudness block (an older project created before
        this feature) — the orchestration then skips normalization entirely. A present block
        with missing keys falls back to the defaults for those keys.
        """
        block = tts_params.get("loudness")
        if not isinstance(block, Mapping):
            return None
        return cls(
            enabled=bool(block.get("enabled", DEFAULT_LOUDNESS_ENABLED)),
            target_lufs=float(block.get("target_lufs", DEFAULT_TARGET_LUFS)),
            peak_ceiling_dbfs=float(block.get("peak_ceiling_dbfs", DEFAULT_PEAK_CEILING_DBFS)),
            max_gain_db=float(block.get("max_gain_db", DEFAULT_MAX_GAIN_DB)),
        )

    def to_params(self) -> dict[str, Any]:
        """The nested JSON-serializable dict stored under ``project.tts_params["loudness"]``."""
        return {
            "enabled": self.enabled,
            "target_lufs": self.target_lufs,
            "peak_ceiling_dbfs": self.peak_ceiling_dbfs,
            "max_gain_db": self.max_gain_db,
        }


def _measure_loudness(samples: Sequence[float], sample_rate: int) -> float | None:
    """Measure integrated loudness (LUFS) via ``pyloudnorm``, or ``None`` to force the fallback.

    Returns ``None`` — signalling the caller to use the RMS fallback — when ``numpy`` /
    ``pyloudnorm`` are not importable, the segment is shorter than the BS.1770 gating block,
    ``pyloudnorm`` raises, or the result is non-finite (``-inf``/``nan``, e.g. for a DC signal
    that K-weighting filters to silence). ``numpy``/``pyloudnorm`` are imported lazily here so
    this module stays import-cheap.
    """
    if sample_rate <= 0 or len(samples) / sample_rate < _MIN_BLOCK_S:
        return None
    try:
        import numpy as np
        import pyloudnorm as pyln
    except ImportError:
        return None
    try:
        data = np.asarray(samples, dtype="float64")
        meter = pyln.Meter(sample_rate)
        # VERIFY: real BS.1770 measurement. ``pyln.Meter(sr).integrated_loudness(x)`` on a mono
        # float array — confirmed against the pyloudnorm API but only exercised on a real render
        # (CI has no numpy/pyln and takes the RMS fallback). Confirm on-machine via smoke_test.
        measured = float(meter.integrated_loudness(data))
    except Exception:  # noqa: BLE001 - any measurement failure => fall back to RMS
        return None
    if not math.isfinite(measured):
        return None
    return measured


def _rms_gain_db(samples: Sequence[float], target_lufs: float) -> float:
    """Pure-Python RMS-to-target gain: ``target_lufs - rms_dbfs`` (approximate, deterministic).

    Used for short clips and whenever the real ``pyloudnorm`` measurement is unavailable. Treats
    the LUFS target as an approximate RMS-dBFS target — not a true BS.1770 match, but a stable,
    dependency-free level-match good enough for edge/CI clips. The caller guarantees a non-silent
    signal (silence is short-circuited upstream), so ``rms > 0``.
    """
    if not samples:
        return 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    if rms <= 0.0:
        return 0.0
    rms_dbfs = 20.0 * math.log10(rms)
    return target_lufs - rms_dbfs


def normalize_samples(
    samples: Sequence[float], sample_rate: int, settings: LoudnessSettings
) -> list[float]:
    """Normalize a mono float signal in ``[-1, 1]`` to ``settings`` (see module docstring §algo).

    Ordered operations: empty -> unchanged; near-silent (peak <= ``_SILENCE_PEAK_EPS``) ->
    unchanged; measure LUFS (pyloudnorm) or fall back to RMS; ``gain = target - measured``,
    capped at ``max_gain_db`` (no lower bound — attenuation is unbounded); apply gain; finally a
    sample-peak ceiling scalar clamp so nothing clips (prioritizing not-clipping over hitting the
    target for high-crest clips).
    """
    if not samples:
        return list(samples)

    peak = max(abs(s) for s in samples)
    if peak <= _SILENCE_PEAK_EPS:
        return list(samples)

    measured = _measure_loudness(samples, sample_rate)
    if measured is not None:
        gain_db = settings.target_lufs - measured
    else:
        gain_db = _rms_gain_db(samples, settings.target_lufs)

    gain_db = min(gain_db, settings.max_gain_db)
    factor = 10.0 ** (gain_db / 20.0)
    out = [s * factor for s in samples]

    ceiling = 10.0 ** (settings.peak_ceiling_dbfs / 20.0)
    new_peak = max(abs(s) for s in out)
    if new_peak > ceiling:
        scale = ceiling / new_peak
        out = [s * scale for s in out]

    return out


def normalize_wav_file(path: Path, settings: LoudnessSettings) -> None:
    """Read a mono16 WAV at ``path``, normalize its samples, and rewrite it in place.

    A WAV that is not mono 16-bit PCM is left untouched (we only ever produce mono16, so a
    :class:`ValueError` from the reader means "not ours" -> skip). Real I/O errors propagate;
    the synthesize orchestration wraps this call defensively so a normalization failure never
    fails an otherwise-good render.
    """
    try:
        samples, sample_rate = read_wav_mono_float(path)
    except ValueError:
        return  # not a mono16 WAV we produced; leave untouched
    normalized = normalize_samples(samples, sample_rate, settings)
    write_wav_mono16(path, normalized, sample_rate)

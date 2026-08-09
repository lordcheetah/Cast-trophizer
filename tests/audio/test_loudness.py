"""Unit tests for per-segment loudness normalization (``casttrophizer.audio.loudness``).

All offline and deterministic. Signals are constant-amplitude (DC) or short so they take the
pure-Python RMS fallback in ``normalize_samples`` — the real ``pyloudnorm`` (BS.1770) path is
marked ``# VERIFY:`` in the source and is never asserted here (a DC signal K-weights to
silence -> non-finite -> fallback, so these pass identically whether or not pyloudnorm is
installed). Assertions are directional (loud attenuated toward target, quiet amplified but
capped, peaks under the ceiling) rather than exact-gain, matching the plan's verification
strategy.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from casttrophizer.audio.loudness import (
    DEFAULT_MAX_GAIN_DB,
    DEFAULT_PEAK_CEILING_DBFS,
    DEFAULT_TARGET_LUFS,
    LoudnessSettings,
    normalize_samples,
    normalize_wav_file,
)
from casttrophizer.audio.wavfile import read_wav_mono_float, write_wav_mono16
from casttrophizer.config import AppConfig
from casttrophizer.workspace.audio_cache import AudioCache

_SR = 8000
#: Sub-block length (< the 0.4s BS.1770 gating block) so ``normalize_samples`` deterministically
#: takes the pure-Python RMS fallback — never the real pyloudnorm path — regardless of whether
#: pyloudnorm is installed. The real BS.1770 path is ``# VERIFY:`` and not asserted here.
_SHORT_N = int(_SR * 0.1)
_DEFAULTS = LoudnessSettings()  # enabled, -18 LUFS, -1 dBFS, 30 dB cap


def _rms(samples: list[float]) -> float:
    return math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0


def _peak(samples: list[float]) -> float:
    return max((abs(s) for s in samples), default=0.0)


# --------------------------------------------------------------------------- #
# normalize_samples — the ordered algorithm
# --------------------------------------------------------------------------- #
def test_empty_is_unchanged() -> None:
    assert normalize_samples([], _SR, _DEFAULTS) == []


def test_silence_is_unchanged() -> None:
    silent = [0.0] * _SR  # 1s of digital silence
    assert normalize_samples(silent, _SR, _DEFAULTS) == silent


def test_near_silence_below_eps_is_unchanged() -> None:
    # Peak just under _SILENCE_PEAK_EPS (1e-4): short-circuits, no divide-by-zero.
    near_silent = [5e-5, -5e-5] * (_SR // 2)
    assert normalize_samples(near_silent, _SR, _DEFAULTS) == near_silent


def test_loud_constant_is_attenuated_toward_target() -> None:
    loud = [0.9] * _SHORT_N  # RMS ~0.9 (-0.9 dBFS), well above the -18 target
    out = normalize_samples(loud, _SR, _DEFAULTS)
    assert _rms(out) < _rms(loud)  # attenuated
    ceiling = 10.0 ** (DEFAULT_PEAK_CEILING_DBFS / 20.0)
    assert _peak(out) <= ceiling + 1e-9  # under the -1 dBFS ceiling
    # RMS lands near the -18 dBFS target (fallback treats LUFS as approx RMS-dBFS).
    assert abs(20.0 * math.log10(_rms(out)) - DEFAULT_TARGET_LUFS) < 1.0


def test_quiet_constant_is_amplified_but_gain_capped() -> None:
    # RMS ~ -80 dBFS => uncapped gain ~62 dB; the 30 dB cap must bound it.
    quiet = [1e-5 + 1e-4] * _SHORT_N  # peak above the silence eps so it is not skipped
    out = normalize_samples(quiet, _SR, _DEFAULTS)
    assert _rms(out) > _rms(quiet)  # amplified
    gain_applied_db = 20.0 * math.log10(_rms(out) / _rms(quiet))
    assert gain_applied_db <= DEFAULT_MAX_GAIN_DB + 1e-6  # capped, not blown up
    ceiling = 10.0 ** (DEFAULT_PEAK_CEILING_DBFS / 20.0)
    assert _peak(out) <= ceiling + 1e-9


def test_peak_ceiling_clamps_high_crest_signal() -> None:
    # Low RMS but a few full-scale spikes => positive gain would push the spikes past the
    # ceiling; the scalar clamp must pull the peak back to exactly the ceiling. Kept short
    # (< 0.4s) so the deterministic RMS fallback runs.
    samples = [0.02] * 800 + [0.95] * 8
    out = normalize_samples(samples, _SR, _DEFAULTS)
    ceiling = 10.0 ** (DEFAULT_PEAK_CEILING_DBFS / 20.0)
    assert _peak(out) <= ceiling + 1e-9
    assert math.isclose(_peak(out), ceiling, rel_tol=1e-6)  # clamp actually engaged


def test_short_clip_uses_rms_fallback_without_crashing() -> None:
    # 0.1s at 8kHz (< the 0.4s BS.1770 block) -> RMS fallback, no raise, gain within the cap.
    short = [0.5] * int(_SR * 0.1)
    out = normalize_samples(short, _SR, _DEFAULTS)
    assert len(out) == len(short)
    ceiling = 10.0 ** (DEFAULT_PEAK_CEILING_DBFS / 20.0)
    assert _peak(out) <= ceiling + 1e-9


# --------------------------------------------------------------------------- #
# normalize_samples — EXACT numeric guarantees on the RMS-fallback path
# (parametrized across target / peak / cap combos; every signal is short so the
# deterministic pure-Python RMS fallback runs regardless of pyloudnorm install state)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target", [-23.0, -18.0, -14.0])
def test_constant_signal_rms_lands_exactly_at_target(target: float) -> None:
    # A DC constant safely below the ceiling: peak == RMS, gain is attenuation, no clamp, so
    # the RMS fallback lands the final level EXACTLY on the target (target - rms_dbfs applied).
    settings = LoudnessSettings(target_lufs=target, peak_ceiling_dbfs=-1.0, max_gain_db=60.0)
    out = normalize_samples([0.5] * _SHORT_N, _SR, settings)
    level_dbfs = 20.0 * math.log10(_peak(out))
    assert math.isclose(level_dbfs, target, abs_tol=1e-6)  # exact math, not just "moved down"
    ceiling = 10.0 ** (settings.peak_ceiling_dbfs / 20.0)
    assert _peak(out) <= ceiling + 1e-12
    assert all(abs(s) <= ceiling + 1e-12 for s in out)  # no sample over the ceiling


@pytest.mark.parametrize("max_gain", [6.0, 12.0, 30.0])
def test_quiet_signal_gain_capped_exactly_at_max_gain(max_gain: float) -> None:
    # Very quiet DC (above the silence eps): the uncapped RMS gain would be ~56 dB, so the
    # applied gain must equal max_gain_db EXACTLY (cap engaged), and stay under the ceiling.
    settings = LoudnessSettings(target_lufs=-18.0, peak_ceiling_dbfs=-1.0, max_gain_db=max_gain)
    src = [2e-4] * _SHORT_N
    out = normalize_samples(src, _SR, settings)
    applied_db = 20.0 * math.log10(_peak(out) / _peak(src))
    assert math.isclose(applied_db, max_gain, abs_tol=1e-6)  # capped to exactly max_gain_db
    ceiling = 10.0 ** (settings.peak_ceiling_dbfs / 20.0)
    assert _peak(out) <= ceiling + 1e-12


@pytest.mark.parametrize("peak_dbfs", [-3.0, -1.0, -0.5])
def test_peak_ceiling_enforced_after_gain(peak_dbfs: float) -> None:
    # High-crest, low-RMS signal: the positive gain pushes the spikes past the ceiling; the
    # scalar clamp (which runs AFTER the gain) must pull the peak back to exactly the ceiling
    # and leave EVERY sample at/under it (proves clamp-after-gain ordering, no clipping).
    settings = LoudnessSettings(target_lufs=-6.0, peak_ceiling_dbfs=peak_dbfs, max_gain_db=60.0)
    samples = [0.01] * 800 + [0.6] * 8
    out = normalize_samples(samples, _SR, settings)
    ceiling = 10.0 ** (peak_dbfs / 20.0)
    assert math.isclose(_peak(out), ceiling, rel_tol=1e-9)  # clamp engaged, peak == ceiling
    assert all(abs(s) <= ceiling + 1e-12 for s in out)


def test_no_output_sample_clips_for_defaults() -> None:
    # The default ceiling (-1 dBFS) is < full scale, so no output sample may reach |1.0|.
    out = normalize_samples([0.99] * _SHORT_N, _SR, _DEFAULTS)
    assert all(abs(s) < 1.0 for s in out)


def test_single_nonzero_sample_above_eps_no_divide_by_zero() -> None:
    # A near-silent-but-nonzero one-sample clip must not divide by zero (rms > 0 guaranteed).
    out = normalize_samples([2e-4], _SR, _DEFAULTS)
    assert len(out) == 1
    ceiling = 10.0 ** (DEFAULT_PEAK_CEILING_DBFS / 20.0)
    assert abs(out[0]) <= ceiling + 1e-12


def test_disabled_settings_do_not_gate_normalize_samples() -> None:
    # normalize_samples itself ignores `enabled` (the orchestration gates on it); it still
    # normalizes when called directly. This documents that the enabled flag is an orchestration
    # concern, so a disabled-but-called signal is still processed.
    loud = [0.9] * _SHORT_N
    disabled = LoudnessSettings(enabled=False)
    assert _rms(normalize_samples(loud, _SR, disabled)) < _rms(loud)


# --------------------------------------------------------------------------- #
# normalize_wav_file — round-trip through the wavfile module
# --------------------------------------------------------------------------- #
def test_normalize_wav_file_round_trip_attenuates_loud(tmp_path: Path) -> None:
    path = tmp_path / "loud.wav"
    write_wav_mono16(path, [0.9] * _SHORT_N, _SR)  # short -> deterministic RMS fallback
    before, sr_before = read_wav_mono_float(path)

    normalize_wav_file(path, _DEFAULTS)

    after, sr_after = read_wav_mono_float(path)
    assert sr_after == sr_before == _SR
    assert len(after) == len(before)
    assert _peak(after) < _peak(before)  # loud file was attenuated
    ceiling = 10.0 ** (DEFAULT_PEAK_CEILING_DBFS / 20.0)
    assert _peak(after) <= ceiling + 1e-3  # within one 16-bit quantum of the ceiling


def test_normalize_wav_file_leaves_silence_valid(tmp_path: Path) -> None:
    path = tmp_path / "silent.wav"
    write_wav_mono16(path, [0.0] * _SR, _SR)
    normalize_wav_file(path, _DEFAULTS)  # must not crash on silence
    after, sr = read_wav_mono_float(path)
    assert sr == _SR
    assert _peak(after) == 0.0  # silence untouched, still a valid mono16 WAV


def test_normalize_wav_file_silence_is_byte_identical(tmp_path: Path) -> None:
    # Silence short-circuits (step 2): the rewritten WAV must be byte-for-byte the original.
    path = tmp_path / "silent.wav"
    write_wav_mono16(path, [0.0] * _SR, _SR)
    before_bytes = path.read_bytes()
    normalize_wav_file(path, _DEFAULTS)
    assert path.read_bytes() == before_bytes  # untouched on disk


def test_normalize_wav_file_leaves_non_mono16_untouched(tmp_path: Path) -> None:
    # A WAV that is not mono16 (a format we never produce) is left byte-identical, not crashed.
    import wave

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(_SR)
        wav.writeframes(b"\x11\x22\x33\x44" * 10)
    before_bytes = path.read_bytes()
    normalize_wav_file(path, _DEFAULTS)  # ValueError from the reader -> skip, no raise
    assert path.read_bytes() == before_bytes


# --------------------------------------------------------------------------- #
# LoudnessSettings — from_config / from_params / to_params
# --------------------------------------------------------------------------- #
def test_from_config_reads_all_four_fields() -> None:
    config = AppConfig(
        loudness_enabled=False,
        loudness_target_lufs=-20.0,
        loudness_peak_dbfs=-2.0,
        loudness_max_gain_db=25.0,
    )
    settings = LoudnessSettings.from_config(config)
    assert settings == LoudnessSettings(
        enabled=False, target_lufs=-20.0, peak_ceiling_dbfs=-2.0, max_gain_db=25.0
    )


def test_to_params_round_trips_through_from_params() -> None:
    settings = LoudnessSettings(
        enabled=True, target_lufs=-16.0, peak_ceiling_dbfs=-1.5, max_gain_db=20.0
    )
    params = {"seed": 7, "loudness": settings.to_params()}
    assert LoudnessSettings.from_params(params) == settings


def test_from_params_none_when_no_loudness_block() -> None:
    assert LoudnessSettings.from_params({"seed": 7}) is None
    assert LoudnessSettings.from_params({}) is None


def test_from_params_supplies_defaults_for_missing_keys() -> None:
    settings = LoudnessSettings.from_params({"loudness": {"enabled": True}})
    assert settings == LoudnessSettings()  # all other keys default


# --------------------------------------------------------------------------- #
# AppConfig.from_env — CASTTROPHIZER_LOUDNESS_* parsing
# --------------------------------------------------------------------------- #
def test_from_env_defaults_when_unset() -> None:
    config = AppConfig.from_env(env={})
    assert config.loudness_enabled is True
    assert config.loudness_target_lufs == DEFAULT_TARGET_LUFS
    assert config.loudness_peak_dbfs == DEFAULT_PEAK_CEILING_DBFS
    assert config.loudness_max_gain_db == DEFAULT_MAX_GAIN_DB


def test_from_env_parses_loudness_vars() -> None:
    config = AppConfig.from_env(
        env={
            "CASTTROPHIZER_LOUDNESS_ENABLED": "0",
            "CASTTROPHIZER_LOUDNESS_TARGET_LUFS": "-20.5",
            "CASTTROPHIZER_LOUDNESS_PEAK_DBFS": "-2.0",
            "CASTTROPHIZER_LOUDNESS_MAX_GAIN_DB": "18",
        }
    )
    assert config.loudness_enabled is False
    assert config.loudness_target_lufs == -20.5
    assert config.loudness_peak_dbfs == -2.0
    assert config.loudness_max_gain_db == 18.0


def test_from_env_enabled_truthy_values() -> None:
    for value in ("false", "NO", "off", "0"):
        assert AppConfig.from_env(
            env={"CASTTROPHIZER_LOUDNESS_ENABLED": value}
        ).loudness_enabled is (False)
    for value in ("1", "true", "yes", "on"):
        assert (
            AppConfig.from_env(env={"CASTTROPHIZER_LOUDNESS_ENABLED": value}).loudness_enabled
            is True
        )


def test_from_env_bad_float_falls_back_to_default() -> None:
    config = AppConfig.from_env(env={"CASTTROPHIZER_LOUDNESS_TARGET_LUFS": "not-a-number"})
    assert config.loudness_target_lufs == DEFAULT_TARGET_LUFS


def test_from_env_empty_or_whitespace_values_do_not_crash() -> None:
    config = AppConfig.from_env(
        env={
            "CASTTROPHIZER_LOUDNESS_TARGET_LUFS": "",
            "CASTTROPHIZER_LOUDNESS_PEAK_DBFS": "   ",
            "CASTTROPHIZER_LOUDNESS_MAX_GAIN_DB": "",
            "CASTTROPHIZER_LOUDNESS_ENABLED": "",  # empty -> not an off-token -> stays on
        }
    )
    assert config.loudness_target_lufs == DEFAULT_TARGET_LUFS
    assert config.loudness_peak_dbfs == DEFAULT_PEAK_CEILING_DBFS
    assert config.loudness_max_gain_db == DEFAULT_MAX_GAIN_DB
    assert config.loudness_enabled is True


# --------------------------------------------------------------------------- #
# Cache-key interaction — the loudness block must feed AudioCache.compute_key so a
# settings change re-renders the affected segments, and be stable across equal settings.
# --------------------------------------------------------------------------- #
def _key(loudness: dict[str, object] | None) -> str:
    params: dict[str, object] = {"seed": 1}
    if loudness is not None:
        params["loudness"] = loudness
    return AudioCache.compute_key("hello world", "voice-a", params)


def test_cache_key_stable_across_repeated_calls_same_settings() -> None:
    block = LoudnessSettings().to_params()
    assert _key(dict(block)) == _key(dict(block))  # deterministic for equal settings


def test_cache_key_no_block_is_stable() -> None:
    assert _key(None) == _key(None)


def test_cache_key_changes_when_enabling_loudness() -> None:
    assert _key(None) != _key(LoudnessSettings().to_params())


def test_cache_key_changes_on_target_lufs() -> None:
    a = LoudnessSettings(target_lufs=-18.0).to_params()
    b = LoudnessSettings(target_lufs=-16.0).to_params()
    assert _key(a) != _key(b)


def test_cache_key_changes_on_peak_ceiling_dbfs() -> None:
    a = LoudnessSettings(peak_ceiling_dbfs=-1.0).to_params()
    b = LoudnessSettings(peak_ceiling_dbfs=-3.0).to_params()
    assert _key(a) != _key(b)


def test_cache_key_changes_on_enabled_toggle() -> None:
    a = LoudnessSettings(enabled=True).to_params()
    b = LoudnessSettings(enabled=False).to_params()
    assert _key(a) != _key(b)


def test_from_params_empty_mapping_is_none_so_normalization_skipped() -> None:
    # The from_params-returns-None contract: no block -> orchestration skips normalization.
    assert LoudnessSettings.from_params({}) is None

# Plan: Per-segment loudness normalization

## Goal

Normalize every rendered per-segment WAV to a consistent perceived loudness so full-cast
speakers (loud narrator clip vs. quiet character clips) no longer jump in volume in the
assembled M4B.

## Pipeline placement

Stage affected: **synthesize** (`parse → correct → segment & attribute → review →
synthesize → assemble`). Normalization runs in the **synthesize orchestration**
(`audio/synthesize.py`) immediately after each segment's WAV is written by the TTS provider,
before the segment is stamped COMPLETED. Assembly is unchanged (a final ffmpeg `loudnorm`
pass is explicitly out of v1 — see §8). Resumability is preserved because the loudness
settings feed the existing per-segment cache key (§5): a normalized WAV lives at the same
cache path as the segment it belongs to, and changing the target re-renders exactly the
affected segments.

## Where normalization runs — DECISION: orchestration, not the provider

Recommend **option (b): a normalization step in the synthesize orchestration** that
post-processes each just-rendered WAV (stdlib `wave` read → normalize float samples →
rewrite 16-bit PCM). Rejecting (a) provider-internal and (c) assembly-final:

- **(a) inside `ChatterboxProvider.synthesize`** (normalize the float array before
  `_write_wav_mono16`) is the only path that normalizes strictly *before* 16-bit
  quantization, but it (i) couples normalization into one provider, (ii) is **never
  exercised by tests** because `FakeTTSProvider` bypasses the provider body and writes its
  own silent WAV — so no stage/orchestration test could cover it offline. The prompt's own
  requirement ("normalizing a silent/near-silent clip via `FakeTTSProvider` must not crash")
  only makes sense if normalization runs *after* the fake writes its WAV, i.e. in the
  orchestration.
- **(c) final ffmpeg `loudnorm` at assembly** normalizes the whole book to one integrated
  target and does **not** fix per-speaker imbalance (the exact bug). At best a complement;
  out of v1.
- **(b) orchestration** fixes per-speaker imbalance (each segment hits the same target),
  keeps heavy deps lazy (numpy/pyloudnorm imported inside the normalize function), is
  provider-agnostic (works for any future TTS provider *and* for `FakeTTSProvider`), and is
  fully offline-unit-testable.

The one cost of (b) is a re-read + re-quantize of a 16-bit WAV. That is negligible: 16-bit
PCM carries ~96 dB SNR, a few-dB gain plus one requantization is far below audibility, and
the audio is re-encoded to lossy AAC at assembly anyway. The float-domain math still happens
before *our* write's quantization; only the provider's initial quantization precedes it.

The reusable math lives in a new pure module `audio/loudness.py` operating on a float
array, so the loudness logic is unit-testable on synthetic signals independent of any WAV or
provider.

## Method & library — DECISION: pyloudnorm (lazy) with a pure-Python RMS fallback

- **Real measurement:** ITU-R BS.1770 integrated loudness via **`pyloudnorm`**
  (`pyln.Meter(sr).integrated_loudness(x)`), gain = `target_lufs - measured`, then a
  **sample-peak ceiling** clamp. `pyloudnorm` + `numpy` are **lazy-imported inside the
  measurement function** (mirroring how `torch`/`chatterbox` are lazy in the provider) so
  importing `audio/loudness.py` stays cheap and CI (no `tts` extra, no numpy) never needs
  them.
- **Fallback:** a pure-Python **RMS-to-target** gain, used whenever (a) `numpy`/`pyloudnorm`
  are not importable (CI/dev), (b) the segment is shorter than the BS.1770 gating block
  (~0.4 s, §4), or (c) the measurement returns a non-finite value (`-inf`/`nan`). This is
  what CI exercises deterministically; the real `pyln` path runs only on a real render and
  is marked `# VERIFY:`.
- **Explicit dep:** do **not** add `pyloudnorm` or `numpy` to core `dependencies` (keeps CI
  light). Add **`pyloudnorm`** explicitly to the **`tts` optional-extra** in `pyproject.toml`
  (it is already a transitive dep of `chatterbox-tts`; listing it makes the requirement
  intentional and pinnable). `numpy` continues to arrive via torch.

### Exact operations on the float array (mono)

Input is a mono float list in `[-1, 1]` (our WAVs are always mono 16-bit PCM). In order:

1. If empty → return unchanged.
2. `peak = max(abs(s) for s in samples)`. If `peak <= _SILENCE_PEAK_EPS` (digital silence /
   near-silent) → **return unchanged** (avoids `-inf` LUFS and divide-by-zero; §7).
3. `measured = _measure_loudness(samples, sample_rate)` → returns LUFS `float` or `None`
   (None when numpy/pyln absent, segment < `_MIN_BLOCK_S`, or non-finite result).
4. `gain_db = (target_lufs - measured)` if measured is not None, else
   `_rms_gain_db(samples, target_lufs)` (pure fallback: `target_lufs - rms_dbfs`, treating
   the target as an approximate RMS-dBFS target for edge/CI clips).
5. **Cap amplification:** `gain_db = min(gain_db, max_gain_db)` so a short, quiet clip cannot
   be blown up (noise amplification, §4). No lower cap — attenuation is unbounded.
6. Apply gain: `out = [s * 10 ** (gain_db / 20) for s in samples]`.
7. **Sample-peak ceiling:** `ceiling = 10 ** (peak_ceiling_dbfs / 20)`;
   `new_peak = max(abs(s) for s in out)`; if `new_peak > ceiling`, scale the whole segment by
   `ceiling / new_peak`. Scalar clamp (not a limiter): guarantees no clip, preserves relative
   dynamics, and — for a high-crest segment where target-LUFS and the ceiling can't both be
   met — prioritizes not clipping over hitting the target. (True-peak limiting / a brick-wall
   limiter is future work; sample-peak is v1.)

When numpy is importable, steps 2/6/7 use vectorized ndarray ops (real renders always have
numpy via torch); the pure-Python path is the CI/fallback path over the tiny synthetic
signals only.

## Target loudness + granularity — DECISION: -18 LUFS, -1 dBFS ceiling, per-segment

- **Integrated-loudness target default: `-18.0 LUFS`** (a common audiobook level; ACX is
  roughly -18 to -23 LUFS with ~-3 dBFS peak — noted, not over-fit). Configurable.
- **Sample-peak ceiling default: `-1.0 dBFS`.** Configurable.
- **Max amplification cap: `30 dB`** (`_DEFAULT_MAX_GAIN_DB`), guards short/quiet clips.
- **Granularity: per-segment for v1.** Each rendered segment is normalized to the target
  independently. This directly fixes per-speaker jumps, needs no cross-segment state, and
  maps 1:1 onto the existing per-segment cache. **Per-speaker** normalization (one gain per
  speaker computed from all their audio, preserving intra-speaker dynamics) is a future
  refinement — it needs a cross-segment measurement pass and a different cache-key story;
  FLAGGED, not in v1.

## Short-segment robustness — DECISION: RMS fallback + gain cap, never skip silently

`pyloudnorm`'s BS.1770 gating needs ≥ ~0.4 s (`_MIN_BLOCK_S`). For a sub-threshold segment
(a one-word quote), `_measure_loudness` returns `None` (it checks `len(samples) /
sample_rate < _MIN_BLOCK_S` before calling `pyln`, and also catches any `pyln` raise / a
non-finite result). The segment is then normalized via the **RMS fallback with the `30 dB`
amplification cap**, so a one-word clip is level-matched approximately without crashing or
being wildly amplified. Digital-silence segments short-circuit at step 2 (untouched).

## Cache-key interaction — DECISION: fold into `tts_params`, no schema bump

The loudness settings **must** feed `AudioCache.key_for` so changing the target re-renders
affected segments (and turning it on re-renders everything). Fold them **into
`project.tts_params`** under a nested `"loudness"` key:

```python
project.tts_params["loudness"] = {
    "enabled": True,
    "target_lufs": -18.0,
    "peak_ceiling_dbfs": -1.0,
    "max_gain_db": 30.0,
}
```

- `AudioCache.compute_key` already hashes `_canonical_params(tts_params)` (JSON, sorted
  keys). A nested JSON-serializable dict is included automatically → **no change to
  `audio_cache.py`**. Changing `target_lufs`/`peak_ceiling_dbfs`/`enabled` changes the hash →
  the skip gate misses → the segment re-renders (and re-normalizes). Turning loudness on
  (adding the block) changes every key → whole book re-renders.
- **No `schema_version` bump.** `tts_params` is a free-form `dict[str, object]` already
  serialized via `dict(project.tts_params)` in `serialization.py`; a nested JSON dict
  round-trips as-is. Existing projects without the block keep their current keys.
- `synthesize_chapter` already passes `params=dict(project.tts_params)` into
  `SynthesisRequest`; the `"loudness"` block rides along and `ChatterboxProvider.synthesize`
  ignores unknown keys (it only reads `seed`/`exaggeration`/`cfg_weight`). No provider change
  needed; do **not** strip it (simpler, harmless).

Rejected alternative: a dedicated `Project.loudness` field — requires a new field +
serialization + a `schema_version` bump + threading it through `compute_key`/`key_for`. More
churn, more risk, no benefit over folding into the existing free-form dict.

## Config / toggle — DECISION: on by default, via `AppConfig` + env, folded into `tts_params`

`AppConfig` gains three primitive fields (config stays audio-import-free):

```python
loudness_enabled: bool = True
loudness_target_lufs: float = -18.0      # DEFAULT_LOUDNESS_TARGET_LUFS
loudness_peak_dbfs: float = -1.0         # DEFAULT_LOUDNESS_PEAK_DBFS
```

`from_env` recognizes `CASTTROPHIZER_LOUDNESS_ENABLED` (`0/false/no` → off, else on),
`CASTTROPHIZER_LOUDNESS_TARGET_LUFS`, `CASTTROPHIZER_LOUDNESS_PEAK_DBFS`.

Flow into a project: `LoudnessSettings.from_config(config)` (in `audio/loudness.py`) → its
`.to_params()` dict is stored at `project.tts_params["loudness"]` when the project is
created. The CLI wires this at `_new_project` time (see tasks). The orchestration then reads
the block back out of `project.tts_params` via `LoudnessSettings.from_params(...)` — a single
source of truth (`tts_params`), matching how the cache key and the provider already read
`tts_params`. The orchestration does **not** take `AppConfig`; everything it needs is in the
persisted `tts_params`.

## Determinism & testing

- Real `pyln` measurement runs only on a real render → mark it `# VERIFY:` (like the
  Chatterbox `generate` call) for the user to confirm on their machine.
- All CI tests are offline. `numpy`/`pyloudnorm` are absent in CI, so `_measure_loudness`
  returns `None` and the deterministic **pure-Python RMS fallback** runs — this is what the
  unit tests assert against.
- Silence safety: `FakeTTSProvider` writes a silent WAV; the orchestration normalize call
  must leave it untouched (step 2) and never divide by zero. Existing synthesize-stage tests
  keep passing unchanged.

## Assembly note — DECISION: no final ffmpeg `loudnorm` in v1 (FLAG)

Per-segment normalization is the fix for per-speaker imbalance; a final single-pass
`loudnorm` at assembly would target overall-book level / ACX compliance but does **not** fix
inter-speaker jumps (already fixed per segment) and would add an ffmpeg re-encode +
`# VERIFY:` surface. Leave it out of v1; FLAG a final-pass / ACX-target as future work
(would slot into `M4BAssembler._run_ffmpeg` as an `-af loudnorm` filter or a second pass).

---

## Affected / new files

| Path | Purpose |
| --- | --- |
| `src/casttrophizer/audio/loudness.py` | **NEW.** `LoudnessSettings` dataclass + `normalize_samples` (pure float→float, lazy numpy/pyln + RMS fallback) + `normalize_wav_file` (WAV read→normalize→rewrite). Heavy deps lazy-imported inside `_measure_loudness`. |
| `src/casttrophizer/audio/wavfile.py` | **NEW (small refactor).** Stdlib-only `read_wav_mono_float(path) -> (list[float], int)` and `write_wav_mono16(path, samples, sample_rate)`. One home for 16-bit PCM mono I/O, shared by the provider and the normalizer. |
| `src/casttrophizer/providers/tts/chatterbox.py` | Replace the local `_to_pcm16` / `_write_wav_mono16` with `write_wav_mono16` from `audio.wavfile` (behavior identical). Optional but removes duplication; if declined, leave the provider untouched and only add a reader to `wavfile.py`. |
| `src/casttrophizer/audio/synthesize.py` | After a successful `tts.synthesize`, call `normalize_wav_file(cache.path_for_key(key), settings)` when loudness is enabled; defensive (log-and-continue on failure, segment stays COMPLETED). |
| `src/casttrophizer/config.py` | Add `loudness_enabled` / `loudness_target_lufs` / `loudness_peak_dbfs` fields + `DEFAULT_LOUDNESS_*` constants + `from_env` parsing. No audio import. |
| `src/casttrophizer/cli.py` | Fold `LoudnessSettings.from_config(config).to_params()` into the `tts_params` passed to `_new_project` (add a `tts_params` param to `_new_project`). |
| `scripts/smoke_test.py` | Fold a default `"loudness"` block into its `tts_params` (optional CLI flags `--target-lufs` / `--peak-dbfs`) so a real smoke render exercises normalization. |
| `pyproject.toml` | Add `pyloudnorm` to the `tts` optional-extra (already transitive; make it explicit). Not a core dep. |
| `tests/audio/test_loudness.py` | **NEW.** Pure-function unit tests + `normalize_wav_file` round-trip on a synthetic loud/quiet/short/silent WAV. |
| `tests/pipeline/test_synthesize_stage.py` | Add: loudness-enabled run with `FakeTTSProvider` doesn't crash on silence; changing `tts_params["loudness"]["target_lufs"]` re-renders (mirrors the existing `test_tts_params_change_regenerates_everything`). |
| `tests/conftest.py` | `synthesize_ready_project.tts_params` may carry a `"loudness"` block so the stage tests run the normalize path; keep a variant without it for the "no block = no normalization" case. |

## Interfaces & data shapes

`audio/loudness.py`:

```python
DEFAULT_TARGET_LUFS = -18.0
DEFAULT_PEAK_CEILING_DBFS = -1.0
DEFAULT_MAX_GAIN_DB = 30.0
_MIN_BLOCK_S = 0.4          # BS.1770 gating block; below this -> RMS fallback
_SILENCE_PEAK_EPS = 1e-4    # peak <= this => treat as silence, leave untouched

@dataclass(frozen=True)
class LoudnessSettings:
    enabled: bool = True
    target_lufs: float = DEFAULT_TARGET_LUFS
    peak_ceiling_dbfs: float = DEFAULT_PEAK_CEILING_DBFS
    max_gain_db: float = DEFAULT_MAX_GAIN_DB

    @classmethod
    def from_config(cls, config: "AppConfig") -> "LoudnessSettings": ...
    @classmethod
    def from_params(cls, tts_params: Mapping[str, Any]) -> "LoudnessSettings | None":
        # None when there is no "loudness" block (=> orchestration skips normalization)
        ...
    def to_params(self) -> dict[str, Any]:  # the nested dict stored under tts_params["loudness"]
        ...

def normalize_samples(
    samples: Sequence[float], sample_rate: int, settings: LoudnessSettings
) -> list[float]: ...

def normalize_wav_file(path: Path, settings: LoudnessSettings) -> None:
    # read mono16 -> normalize_samples -> rewrite mono16. Defensive: only real I/O errors
    # propagate; a non-mono/non-16-bit WAV is left untouched (we only ever produce mono16).
```

`audio/wavfile.py`:

```python
def read_wav_mono_float(path: Path) -> tuple[list[float], int]: ...   # (samples in [-1,1], sr)
def write_wav_mono16(path: Path, samples: Sequence[float], sample_rate: int) -> None: ...
```

Orchestration seam in `synthesize_chapter` (inside the success `else` branch, after stamping
COMPLETED / setting `rendered_any`):

```python
settings = LoudnessSettings.from_params(project.tts_params)
if settings is not None and settings.enabled:
    try:
        normalize_wav_file(cache.path_for_key(key), settings)
    except Exception:  # normalization must never fail an otherwise-good render
        logger.warning(...)  # segment stays COMPLETED with un-normalized audio
```

Persisted shape (no schema bump): `project.tts_params["loudness"] = {"enabled": bool,
"target_lufs": float, "peak_ceiling_dbfs": float, "max_gain_db": float}`.

## Ordered tasks (for the coder)

1. **`audio/wavfile.py`** — add `read_wav_mono_float` + `write_wav_mono16` (move the body of
   `chatterbox._write_wav_mono16` / `_to_pcm16` here verbatim; stdlib `wave` only).
2. **Refactor `chatterbox.py`** to import `write_wav_mono16` from `audio.wavfile` and drop
   the local copies (behavior identical). (Skip if opting for minimal change; then `wavfile`
   holds the reader only and keeps its own writer.)
3. **`audio/loudness.py`** — `LoudnessSettings` (+ `from_config`/`from_params`/`to_params`),
   `_measure_loudness` (lazy numpy/pyln, `# VERIFY:` on the real `pyln` call, returns `None`
   on absent-deps / short / non-finite), `_rms_gain_db` (pure fallback), `normalize_samples`
   (the 7-step pipeline above), `normalize_wav_file` (via `wavfile`).
4. **`config.py`** — add the three fields + `DEFAULT_LOUDNESS_*` + `from_env` parsing.
5. **`cli.py`** — give `_new_project` a `tts_params: dict` param; at its call site build
   `tts_params={**config.tts_params, "loudness": LoudnessSettings.from_config(config).to_params()}`.
6. **`audio/synthesize.py`** — call `normalize_wav_file` after a successful synth (defensive,
   as above); add a module logger.
7. **`pyproject.toml`** — add `pyloudnorm` to the `tts` extra.
8. **`scripts/smoke_test.py`** — fold a default loudness block into its `tts_params` (optional
   flags).
9. **Tests** — `tests/audio/test_loudness.py` (pure function + `normalize_wav_file`
   round-trip); extend `tests/pipeline/test_synthesize_stage.py` (silence-safe run,
   target-change re-render); adjust `conftest` fixtures.

## Risks & open questions

1. **RMS fallback ≠ LUFS.** On a real render (numpy present) the pyln path runs; in CI the
   RMS fallback runs. They compute different gains, so CI never asserts real-model output
   (intentional, like Chatterbox). The cache key encodes *settings*, not *which method ran*,
   so a project rendered without numpy and later with numpy would **not** re-render. In
   practice real renders always have numpy (it ships with torch/the `tts` extra), so the
   fallback never runs on a real render — accepted. FLAG for the user.
2. **Scalar peak clamp vs. limiter.** A high-crest segment can't hit both -18 LUFS and the
   -1 dBFS ceiling; v1 prioritizes not clipping and accepts slightly-under-target loudness.
   True-peak / brick-wall limiting is future work. FLAG.
3. **Normalization failure policy.** Recommend log-and-continue (segment COMPLETED,
   un-normalized). Downside: a transient failure gets "stuck" (re-run is a cache hit and
   won't retry). Alternative: mark FAILED to force retry. Normalization is pure math and
   rarely fails; recommend log-and-continue. CONFIRM.
4. **`_new_project` currently ignores `config.tts_params`.** Task 5 is the first code to fold
   config-derived params into a new project; confirm no existing caller depends on
   `tts_params` starting empty (the fixtures set it explicitly, so this is additive).
5. **Provider receives the `"loudness"` block** in `SynthesisRequest.params`. Harmless
   (Chatterbox ignores unknown keys); not stripped for simplicity. FLAG only if a future
   provider echoes/validates params.
6. **ACX / final-level compliance** is not addressed by per-segment normalization; deferred
   (§8).

## Verification strategy (for the tester — all offline, no real model)

- **Pure function (`normalize_samples`, RMS-fallback path forced by numpy/pyln absence):**
  - A too-loud constant-amplitude / full-scale sine (peak≈1.0) → output RMS moved *down*
    toward target and `max(abs) <= 10**(-1/20)` (under the ceiling).
  - A too-quiet signal (tiny amplitude) → output amplified (higher RMS) but gain capped and
    peak still under the ceiling.
  - A **silent** array (all zeros) and a **near-silent** array (peak ≤ `_SILENCE_PEAK_EPS`)
    → returned **unchanged**, no exception, no divide-by-zero.
  - A **short** array (< `_MIN_BLOCK_S` at the given sr) → uses the RMS fallback (no raise),
    gain within the cap.
- **`normalize_wav_file` round-trip:** write a synthetic *loud* mono16 WAV, normalize it,
  read it back, assert peak dropped under the ceiling and the file is still a valid mono16
  WAV at the same sample rate.
- **Orchestration / stage (`FakeTTSProvider`, silent WAV, loudness enabled):**
  `synthesize_chapter` / `SynthesizeStage.run` completes without error, segments COMPLETED,
  WAVs still present (silence untouched).
- **Cache-key invalidation:** with `tts_params["loudness"]` present, snapshot
  `AudioCache.key_for(seg, project)`; mutate `target_lufs` (and separately toggle `enabled`);
  assert the key changes, and that a re-run re-synthesizes the affected segments (parallel to
  the existing `test_tts_params_change_regenerates_everything`). Also assert a project with
  **no** `"loudness"` block skips normalization (`from_params` returns `None`).
- **Lazy-import guard:** importing `audio/loudness.py` must not import `numpy`/`pyloudnorm`
  (extend the existing lazy-import test that forbids torch/chatterbox/mutagen at module top).

---

## DECISIONS FOR USER — CONFIRMED (build to these; all recommended defaults accepted)

1. **Where it runs: CONFIRMED — orchestration** (`audio/synthesize.py` post-synth WAV normalize
   via a new `audio/loudness.py`), NOT the provider — provider-agnostic + offline-testable.
2. **Library + packaging: CONFIRMED — `pyloudnorm`** (BS.1770, LAZY-imported so CI without the
   `tts` extra never needs it) with a pure-Python RMS fallback for short clips; add `pyloudnorm`
   to the **`tts` extra** (not core deps); numpy comes via torch (also lazy).
3. **Target + ceiling: CONFIRMED — integrated -18 LUFS, sample-peak ceiling -1 dBFS, max
   amplification cap 30 dB.** All configurable (see decision 6).
4. **Granularity: CONFIRMED — per-segment** for v1 (per-speaker deferred).
5. **Cache-key placement: CONFIRMED — fold settings into `project.tts_params["loudness"]`** so the
   existing `AudioCache.compute_key` picks them up; NO `schema_version` bump. Enabling normalization
   (or changing the target) therefore changes every affected segment's key → it re-renders. That's
   intended; note it in user-facing docs/output.
6. **Config/toggle: CONFIRMED — ON by default**, target/peak/cap/toggle configurable via `AppConfig`
   + `CASTTROPHIZER_*` env (and merged into `tts_params["loudness"]`). IMPORTANT: fix the flagged gap
   where `cli.py::_new_project` ignores `config.tts_params` — the loudness block must reach the
   project's `tts_params` (so it feeds the cache key + the request).
7. **Final assembly `loudnorm` pass: CONFIRMED — NOT in v1** (per-segment normalization is the fix;
   ACX/whole-book leveling is future work).

Handoff: the coder should build **Task 1–3 first** — `audio/wavfile.py`, the
`chatterbox.py` writer refactor, and `audio/loudness.py` (`LoudnessSettings` +
`normalize_samples` + `normalize_wav_file`) — since every later task and test depends on that
module and its interfaces.

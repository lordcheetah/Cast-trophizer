# Plan: Synthesize Stage (`SynthesizeStage`)

**Goal:** Render every attributed `Segment` to its own cached WAV using that segment's speaker's
voice (full-cast), skipping segments whose audio is already cached and current, so re-runs and
edits only regenerate what changed. Sets `Segment.audio_cache_key` / `Segment.audio_status`,
persists per-chapter, records `stage_status[SYNTHESIZE] = COMPLETED`, and is resumable.

Pipeline stage: **`synthesize`** (fifth stage; after `review`, before `assemble`). It reads
attributed `Line.segments`, resolves each segment's voice clip via its `Speaker`, calls
`ctx.tts.synthesize` only for segments whose cache key is new/changed, writes
`audio/<key>.wav` into the workspace audio cache, stamps `audio_cache_key`/`audio_status`, and
persists.

This stage drives `ctx.tts` (the first stage that does), exactly as `attribute` drives `ctx.llm`.

---

## 0. Decisions locked by existing code (do not relitigate)

- `Segment(... audio_cache_key: str | None = None, audio_status: ReviewStatus = PENDING)` already
  exists and round-trips (`_segment_to_dict`/`_segment_from_dict`). Use as-is — **no schema bump**
  (§8).
- `AudioCache` (`workspace/audio_cache.py`) already implements the key contract:
  `compute_key(segment_text, voice_clip_id, tts_params)`, `key_for(segment, project)` (resolves the
  voice clip id from the segment's speaker), `path_for_key(key)`, `has(key)`. **Reuse it verbatim;
  do not reimplement key logic.** Note `key_for` already folds a missing/None voice clip id into the
  key as `""`.
- `WorkspaceLayout.audio_path(cache_key) -> root/audio/<key>.wav` and `ensure_dirs()` already create
  `audio/`. WAVs land there. Source clips are referenced by absolute path and never copied.
- `TTSProvider.synthesize(request: SynthesisRequest, out_path: Path) -> SynthesisResult` and
  `is_available()` are the only TTS entry points. `SynthesisRequest(text, voice_clip_path: Path,
  params: dict)`; `SynthesisResult(audio_path, sample_rate, duration_s)`. **No change to
  `providers/base.py` is required** (§3).
- `StageContext` carries `tts: TTSProvider | None` and `config: AppConfig | None`. The stage uses
  `ctx.tts`; it **never** calls `build_tts_provider` (the runner/UI wires it). `__init__` takes no
  provider — mirror `SegmentAttributeStage` which gets the LLM purely from `ctx.llm`.
- `FakeTTSProvider` (`tests/fakes/fake_tts.py`) writes a silent WAV and records
  `synthesize_calls: list[SynthesisRequest]`. Already exported from `tests/fakes/__init__.py` and a
  `fake_tts` fixture exists. The stage is fully testable against it.
- Stage contract (`pipeline/stage.py`): poll `ctx.progress.should_stop()` at safe checkpoints,
  return STOPPED with partial state persisted, idempotent/resumable, Qt-free, `is_complete` purely
  `stage_status`-driven. `stage_status` keys are `str(StageName.X)`. Runner halts on
  STOPPED/NEEDS_REVIEW/FAILED.
- `project.tts_params` (a `dict[str, object]`) already exists, round-trips, and is the input the
  cache key reads. `AppConfig.tts_params` exists as the source of global defaults.

---

## 1. Where this sits & what it reads/writes

| | |
|---|---|
| **Reads** | `project.book.chapters[*].lines[*].segments[*]` (attributed); `project.speakers` (→ `voice_clip_id`); `project.voice_clips` (→ `source_path`); `project.tts_params`. |
| **Writes** | `Segment.audio_cache_key`, `Segment.audio_status`; WAV files at `audio/<key>.wav`. **Nothing else on the Project mutates** (text/speakers untouched). |
| **Uses** | `ctx.tts.synthesize(...)` — the only TTS call. Voice resolution + cache decisions are offline. |
| **Persists** | whole `Project` via `ctx.store.save`, per chapter. |
| **Records** | `stage_status[str(StageName.SYNTHESIZE)] = COMPLETED`. |
| **Progress** | per **segment** (`set_total(total_segments)`, one `advance` per segment — skipped or rendered) so a thousands-of-segments render updates the UI and stop is responsive. |

---

## 2. What gets synthesized & voice resolution (THE central gap — FLAG)

Per the per-segment render decision, **every** segment — narration and quote — is synthesized with
its speaker's voice. Resolution chain for a segment:

```
segment.speaker_id -> Speaker (in project.speakers)
                   -> Speaker.voice_clip_id
                   -> VoiceClip (in project.voice_clips)
                   -> VoiceClip.source_path  (read-only reference clip)
```

`AudioCache._voice_clip_id_for` already does the `segment → speaker → voice_clip_id` half. The stage
adds the `voice_clip_id → VoiceClip.source_path` lookup (a `{voice_clip_id: VoiceClip}` index built
once per run).

### 2a. Narrator voice — a REAL GAP (FLAG, decision required)

The attribute stage created a reserved narrator `Speaker(role=NARRATOR)` and character `Speaker`s,
but **assigned no `voice_clip_id` to any of them** (voice assignment is deferred to the user). So at
synthesize time, in the common case, **some or all speakers have `voice_clip_id=None`**. There is no
narrator-specific special case in the data model — the narrator is just a `Speaker` whose
`voice_clip_id` the user must assign, exactly like a character.

**Recommendation:** treat the narrator uniformly — it is a `Speaker` that needs a `voice_clip_id`
like any other. Do **not** invent a "default narrator clip" or a dedicated `Project.narrator_voice`
field (that is a schema change and a hidden special case). The user assigns the narrator's clip in
the (future) voice-assignment UI the same way they assign Alice's. **FLAG:** confirm there is no
separate narrator-voice concept; if the user wants a project-level default narrator clip, that is an
additive schema field (`schema_version` bump) and is out of scope for v1 — call it out.

### 2b. Missing-voice precondition — FLAG (decision required, recommend FAIL-fast)

A segment whose resolved `voice_clip_id is None`, or whose `VoiceClip.source_path` file is missing,
**cannot be synthesized**. Two policies:

- **(A) FAIL the stage up front** with a clear, actionable message listing which speakers lack a
  voice ("assign voices for: narrator, Bob"). Status left unset (re-runnable once voices are
  assigned). Mirrors `attribute`'s `ctx.llm is None → FAILED, status unset`.
- **(B) Skip-and-flag**: synthesize the segments that *can* be, leave the rest with
  `audio_status = NEEDS_REVIEW` (or a dedicated "no voice" marker), and still return COMPLETED.

**Recommended: (A) FAIL-fast with a precheck**, because:
- voice assignment is a *precondition* the user controls, not a per-segment data defect — a whole
  book rendered with half the cast silent is worse than a clear "assign voices first" error;
- it matches the stage philosophy that an unrunnable provider/precondition is FAILED, not silently
  partial;
- it is cheap (one pass over speakers before any TTS call) and keeps the per-segment loop simple.

The precheck: collect the distinct speakers referenced by *any* segment; if any referenced speaker
has `voice_clip_id=None` **or** its `VoiceClip` is absent **or** the `source_path` file does not
exist, return `FAILED` naming them, before doing TTS work. **FLAG:** confirm (A) vs (B). (If (B) is
chosen, the per-segment loop flags those segments and continues — covered as an alternative in §5.)

> Note: the precheck must run **before** `set_total`/the render loop so a misconfigured project
> fails instantly rather than after rendering 3000 segments. A missing source file discovered
> mid-loop (race: user deleted a clip) falls to the §5 per-segment failure policy.

---

## 3. AudioCache integration & idempotency (the core value)

The "full render is expensive — cache and only regenerate on change" principle. The cache key is the
single source of truth for "is this segment current?".

### 3a. Per-segment decision (airtight skip logic)

For each segment, compute `key = AudioCache.key_for(segment, project)` (which folds in
`segment.text`, the resolved `voice_clip_id`, and `project.tts_params`). A segment is
**already-synthesized-and-current** — and the TTS call is skipped — iff **all** hold:

1. `segment.audio_cache_key == key` (the stored key matches the freshly computed key), **and**
2. `audio_cache.has(key)` is True (the `<key>.wav` file actually exists on disk), **and**
3. `segment.audio_status` is an acceptable rendered status (see §3c — at minimum not `FAILED`).

If any condition fails, the stage **renders**: build `SynthesisRequest`, call `ctx.tts.synthesize`,
write to `audio_cache.path_for_key(key)`, then set `segment.audio_cache_key = key` and
`segment.audio_status = <rendered status>`.

> Defensive detail: condition (1) catches "text/voice/params changed → new key, old key stamped on
> the segment"; condition (2) catches "key stamped but the WAV was deleted / never written (crash
> between save and file write)". Checking both is what makes resume robust against a half-written
> workspace. **Order matters:** recompute the key from current segment/project state every run — never
> trust the stored `audio_cache_key` as proof of currency on its own.

### 3b. How an edit forces regeneration

An edit to a segment's `text`, a reassignment of its speaker's `voice_clip_id`, or a change to
`project.tts_params` changes the computed `key` → condition (1) fails → the segment re-renders under
the new key. The **old** `<oldkey>.wav` becomes orphaned (see §6 — out of scope to delete). No
explicit invalidation call is needed; the key *is* the invalidation mechanism. This is why
`key_for` must always be recomputed from live state, not read back.

### 3c. `Segment.audio_status` lifecycle

`audio_status` is the per-segment audio review state (distinct from `review_status`, which is the
*attribution* review state). Reuse the existing `ReviewStatus` enum:

- `PENDING` — initial (set by attribute/parse); not yet rendered.
- After a successful render → **`COMPLETED`** (recommended) — "rendered, awaiting per-line user
  review." (Goal #3 is a later REVIEW step where the user listens and APPROVEs/regenerates; that
  later stage promotes `COMPLETED → APPROVED` or forces a re-render. The synthesize stage does
  **not** set `APPROVED` — approval is a human act.)
- On a per-segment TTS failure (if §5 soft-flag policy) → **`FAILED`** so the next run retries it
  (condition 3) and the review UI can surface it.

**Skip rule interaction (§3a cond. 3):** treat `COMPLETED` and `APPROVED` as "rendered & current →
skip if key+file match"; treat `PENDING` and `FAILED` as "must render". This lets a future
user-driven "regenerate this clip" simply reset the segment's `audio_status` to `PENDING` (or clear
`audio_cache_key`), and a re-run re-renders just that segment — symmetric with how attribute's
"clear `segments`" re-triggers. **FLAG (minor):** confirm rendered status is `COMPLETED` (not a new
enum value); recommend reusing `COMPLETED` to avoid a schema/enum change.

### 3d. Stale cache files

A key change orphans the old WAV. The stage does **not** delete orphans (§6). The cache is
content-addressed, so a stale file is harmless (never referenced by a current key) — it only costs
disk. Orphan GC is a separate, explicit maintenance action (out of scope, flagged).

---

## 4. TTSProvider / ChatterboxProvider

### 4a. `SynthesisRequest` construction (the stage's job)

Per renderable segment:
```python
SynthesisRequest(
    text=segment.text,
    voice_clip_path=Path(voice_clip.source_path),   # resolved in §2
    params=dict(project.tts_params),                 # the SAME dict folded into the cache key
)
```
The `params` passed to the provider **must be the same params hashed into the key** (§3b), or a
param change would not invalidate. Keep one source: `project.tts_params`.

### 4b. ChatterboxProvider (`providers/tts/chatterbox.py`) — FLAG: implement now vs fake-only

The stub currently `raise NotImplementedError` in `_load_model` / `synthesize`. The stage is fully
functional and testable against `FakeTTSProvider`, so it can land with the provider still stubbed.

**Recommended (mirror attribute's Claude decision): implement the REAL `ChatterboxProvider` in the
same PR** — without it no real user can render audio, and it is the headline feature's payload. The
implementation rules (the coder MUST verify the actual Chatterbox API against the installed package /
its docs — do **not** hardcode the API from memory):

- **Lazy imports inside methods only.** `from chatterbox.tts import ChatterboxTTS` and any `torch`
  import live **inside** `_load_model` / `synthesize` / `is_available`, never at module top. The
  `tests/test_lazy_imports.py` cases for `("...chatterbox", "torch")` and `(..., "chatterbox")` must
  stay green. `is_available()` already uses `importlib.util.find_spec` (no load) — keep that.
- **Load the model lazily and once.** `_load_model` populates `self._model` on first call and reuses
  it (the field already exists). **FLAG:** the model is large (GPU/CPU memory). Caching one instance
  per provider is correct for a single-process render; document the memory cost. Device selection
  (`self._device`): default to CUDA if available else CPU (verify Chatterbox's device API; do not
  assume).
- **`synthesize`:** load model, map `request.text` + `request.voice_clip_path` + `request.params`
  (e.g. `exaggeration`, `cfg_weight`, `seed`) to Chatterbox's generate API, obtain the waveform +
  sample rate, **write a WAV to `out_path`** (the stage passes `audio_cache.path_for_key(key)`),
  ensure `out_path.parent` exists, and return `SynthesisResult(audio_path=out_path, sample_rate=...,
  duration_s=len(samples)/sample_rate)`. On any failure raise `TTSProviderError` (already in
  `errors.py`).
- **Determinism:** if `params` carries a `seed`, set Chatterbox/torch seed so the same request
  yields the same audio (the `synthesize` docstring already promises "deterministic given the same
  seed"). This matters because the cache assumes a key uniquely identifies audio.

**Alternative (smaller PR):** stage + `FakeTTSProvider` only; leave Chatterbox stubbed with a TODO.
A real render is then blocked until the provider lands. Recommend implementing now (parity with the
attribute-stage decision), but this is a FLAG for the user.

---

## 3b/4c. `tts_params` shape — FLAG (recommend global-only for v1)

`project.tts_params` is `dict[str, object]`, sourced from `AppConfig.tts_params`. The fixture already
uses `{"exaggeration": 0.5, "seed": 7}`. Recommended v1 shape (Chatterbox-aligned; **verify names
against the package**):

```python
tts_params = {
    "exaggeration": float,   # emotional intensity
    "cfg_weight": float,     # classifier-free guidance weight (a.k.a. cfg)
    "seed": int,             # determinism
}
```

**Global vs per-speaker:** the cache key reads `project.tts_params` globally (`key_for` passes
`project.tts_params` for every segment). Per-speaker overrides would require the key to incorporate
the *speaker's* params, i.e. a change to `AudioCache.key_for`/`compute_key` and a new
`Speaker.tts_params` field (**schema bump**).

**Recommended: GLOBAL params for v1** (one knob set for the whole book). Per-speaker overrides are a
real future want (a shouty villain vs. a calm narrator) but they are scope creep here and force a
schema change + key-derivation change. **FLAG:** confirm global-only; if per-speaker is wanted now,
it expands to (a) `Speaker.tts_params` field + serialization, (b) `schema_version` bump, (c)
`AudioCache.key_for` folding per-speaker params, (d) the stage merging global+speaker params into the
request. Keep that out of v1 unless the user insists.

> Whichever is chosen, the **params that feed the cache key must equal the params sent to the
> provider** — one merge point, no drift.

---

## 5. Failure handling (per-segment TTS failure) — FLAG (recommend flag-and-continue)

A single segment's `ctx.tts.synthesize` raising (e.g. `TTSProviderError`, a transient CUDA OOM, a
clip the model rejects) in a multi-thousand-segment render.

- **(A) FAIL the whole stage** on the first segment error — loud, but one bad segment kills a
  6-hour render.
- **(B) Flag-and-continue** (mirror attribute's resilience): catch the per-segment error, set that
  `segment.audio_status = FAILED` (leave/clear `audio_cache_key`), log it, advance progress, and keep
  rendering the rest. The stage returns **COMPLETED**; the failed segments are surfaced for review
  (and re-render on the next run because §3a condition 3 makes `FAILED` non-skippable).

**Recommended: (B) flag-and-continue**, with a guard: if *every attempted* segment fails (e.g. the
model is fundamentally broken), or `synthesize` raises on the first N consecutively, prefer to
**FAIL** so a systemically broken provider is loud rather than producing an all-FAILED book labeled
COMPLETED. Simplest defensible rule: flag-and-continue per segment, but if **zero** segments rendered
successfully and at least one failed, return FAILED. **FLAG:** confirm flag-and-continue + the
"all-failed ⇒ FAILED" guard.

**Distinct from precondition failures:**
- `ctx.tts is None` → `StageResult(SYNTHESIZE, FAILED, "no TTS provider configured")`, status unset
  (re-runnable). Mirrors attribute.
- `not ctx.tts.is_available()` → `FAILED, "TTS provider <name> unavailable"`, status unset.
- Missing-voice precondition (§2b) → FAILED before the loop (if policy A).
- Per-segment synth error mid-loop → §5 (flag-and-continue).

---

## 6. Workspace / audio layout

- WAVs land at `WorkspaceLayout.audio_path(key)` = `<workspace>/audio/<key>.wav` (the existing cache
  dir). `ensure_dirs()` creates it; the stage also `mkdir(parents=True, exist_ok=True)` on the
  parent defensively (the fake already does this).
- Source voice clips (`VoiceClip.source_path`) are **read-only** — opened by the provider, never
  written/copied. Nothing is written outside the workspace.
- **Orphaned cache files** (from edits/re-attribution producing new keys) are **not** cleaned up by
  this stage — out of scope (flagged). A content-addressed orphan is harmless correctness-wise.

---

## 7. Edge cases (stage must handle; tester must cover)

| Case | Required behavior |
|---|---|
| Empty / whitespace-only segment text | Attribute produces zero segments for empty *lines*, so usually none reach here. If a whitespace-only segment exists, **skip it** (no TTS call): treat as nothing-to-render, leave `audio_status=PENDING`/mark `COMPLETED` with no file? **Recommend skip + leave PENDING + no file**, and exclude it from "must render" — it has no audible content. FLAG (minor): confirm whitespace segments are skipped, not errored. |
| Heading-only line | Same as above — only real text segments render. |
| Speaker with `voice_clip_id` whose `VoiceClip` is missing from `project.voice_clips` | Precheck (§2b) → FAILED (policy A) naming the speaker. |
| `VoiceClip.source_path` file missing on disk | Precheck (§2b) catches it up front → FAILED. A file deleted mid-run → §5 per-segment FAILED. |
| Very large book (thousands of segments) | Per-segment progress + per-chapter persist; stop is responsive (polled per chapter, and optionally per-N-segments — see §9). |
| Re-run after user edits text / reassigns a voice | New key → re-render just affected segments (§3b); unaffected segments skipped (zero TTS calls). |
| Mixed already-rendered + new segments | Each decided independently by §3a; only new/changed ones call TTS. |
| All segments already cached & current (full re-run) | **Zero** TTS calls; stage still COMPLETED (tester asserts `synthesize_calls == []`). |
| Two segments with identical text+voice+params | Same key → same file; the second is a cache hit (no second TTS call). Correct dedup for free. |
| `ctx.tts is None` / unavailable | FAILED, status unset, re-runnable. |

---

## 8. Schema impact — NONE required (if recommendations are taken)

`Segment.audio_cache_key`/`audio_status` already exist and round-trip. `project.tts_params` exists.
**No `schema_version` bump, no migration** for the recommended v1 (global params, narrator-as-Speaker,
reuse `COMPLETED` for rendered, no audio-duration field on Segment).

A bump **would** be forced only by an out-of-scope choice:
- **per-speaker `tts_params`** (§3b/4c) → new `Speaker.tts_params` field + key-derivation change.
- a dedicated **`Project.narrator_voice_clip_id`** default (§2a).
- storing **`duration_s`/`sample_rate` on `Segment`** (useful for assembly to avoid re-probing WAVs).
  **Recommend deferring** — assembly can probe the WAV; if the user wants it cached, it is additive +
  a bump. FLAG only if assembly needs it.

---

## 9. Stage wiring & resumability (`pipeline/stages/synthesize.py`)

Mirror `SegmentAttributeStage.run` structure. The stage stays thin; put the per-chapter render
orchestration in a small helper module so the stage mirrors how `attribute.py` holds the policy.

```python
class SynthesizeStage(Stage):
    name = StageName.SYNTHESIZE

    def is_complete(self, project: Project) -> bool:
        return project.stage_status.get(str(StageName.SYNTHESIZE)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        if ctx.tts is None:
            return StageResult(self.name, ReviewStatus.FAILED, "no TTS provider configured")
        if not ctx.tts.is_available():
            return StageResult(self.name, ReviewStatus.FAILED,
                               f"TTS provider {ctx.tts.name} unavailable")

        cache = AudioCache(ctx.store.layout)                 # layout already on the store
        missing = unresolved_voices(project)                 # §2b precheck
        if missing:                                          # policy A
            return StageResult(self.name, ReviewStatus.FAILED,
                               f"assign voices first: {', '.join(missing)}")

        segments = [s for ch in project.book.chapters for ln in ch.lines for s in ln.segments]
        ctx.progress.set_total(len(segments))                # per-segment progress

        any_ok = False
        any_fail = False
        try:
            for chapter in project.book.chapters:
                if ctx.progress.should_stop():
                    ctx.store.save(project)                  # persist rendered-so-far
                    return StageResult(self.name, ReviewStatus.STOPPED, "stopped during synthesize")
                ok, fail = synthesize_chapter(chapter, project, ctx.tts, cache, ctx.progress)
                any_ok = any_ok or ok
                any_fail = any_fail or fail
                ctx.store.save(project)                      # chapter-granular persist
        except TTSProviderError as exc:                      # provider went unreachable mid-run
            ctx.store.save(project)
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        if any_fail and not any_ok:                          # §5 all-failed guard
            ctx.store.save(project)
            return StageResult(self.name, ReviewStatus.FAILED, "all segments failed to synthesize")

        project.stage_status[str(StageName.SYNTHESIZE)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, "segments synthesized")
```

`synthesize_chapter(chapter, project, tts, cache, progress) -> (rendered_any, failed_any)` (in a new
`audio/synthesize.py` helper, mirroring `attribution/attribute.py`):
- Build a `{voice_clip_id: VoiceClip}` index once (passed in or memoized).
- For each line, for each segment:
  - skip whitespace-only text (§7) — `progress.advance(1)`, continue;
  - `key = AudioCache.key_for(segment, project)`;
  - if `segment.audio_cache_key == key and cache.has(key) and segment.audio_status in {COMPLETED, APPROVED}` → cache hit, `progress.advance(1)`, continue (no TTS);
  - else resolve `voice_clip.source_path`, build `SynthesisRequest`, `try: tts.synthesize(req, cache.path_for_key(key))` → set `audio_cache_key=key`, `audio_status=COMPLETED`, `rendered_any=True`; `except TTSProviderError: audio_status=FAILED`, `failed_any=True` (per §5);
  - `progress.advance(1, message=...)`.

**Per-segment progress (not per-chapter):** there can be thousands of segments, so progress must be
per-segment for a usable UI. **Stop responsiveness FLAG (minor):** `should_stop` is polled per
chapter in the skeleton above. For a single huge chapter this is too coarse. **Recommend** polling
`should_stop()` inside the segment loop every N segments (e.g. 16) with a partial `ctx.store.save`,
so stop is responsive on big chapters. Confirm the poll granularity (per-chapter vs per-N-segment).

**Constructor:** `SynthesizeStage()` takes no provider (TTS comes from `ctx.tts`). If a test seam is
wanted, allow injecting the `AudioCache`/cache-dir, but the provider is always `ctx.tts` (so tests
pass `FakeTTSProvider`). Mirror attribute's "provider always from ctx" rule.

### Module layout

| Path | Purpose |
|---|---|
| `src/casttrophizer/pipeline/stages/synthesize.py` | fill `SynthesizeStage.run`/`is_complete` (§9). |
| `src/casttrophizer/audio/synthesize.py` | `synthesize_chapter(...)`, `unresolved_voices(project) -> list[str]`, voice-index helper (orchestration, keeps the stage thin). |
| `src/casttrophizer/providers/tts/chatterbox.py` | implement `_load_model`/`synthesize` (§4b) if "real now" chosen. |

> `audio/` already exists (`audio/assembler.py`); placing synthesize orchestration there keeps the
> audio concern together. Confirm the lazy-import test gains a case so `audio.synthesize` /
> `pipeline.stages.synthesize` pull in **no** torch/chatterbox.

---

## 10. Test plan (for the tester — fully offline/deterministic, `FakeTTSProvider` only)

Add an `synthesize_ready_project` fixture to `conftest.py`: a saved project with
`stage_status[PARSE/CORRECT/ATTRIBUTE]=COMPLETED` whose lines carry **attributed segments** (narration
+ quote), with `project.speakers` having **assigned `voice_clip_id`s** pointing at the existing
`fake_voice_clips`, and `project.tts_params` set (e.g. `{"exaggeration":0.5,"seed":7}`). Provide a
**variant with an unassigned voice** (one speaker `voice_clip_id=None`) for the precondition test.
Reuse `fake_voice_clips`/`fake_tts` fixtures.

`tests/pipeline/test_synthesize_stage.py` (integration), asserting against the **reloaded** project
(`store.load()` proves persistence), `FakeTTSProvider`, `RecordingProgressReporter`:

1. **Happy path:** returns `StageResult(SYNTHESIZE, COMPLETED)`;
   `stage_status[str(StageName.SYNTHESIZE)] == COMPLETED`; every (non-whitespace) segment has a
   non-None `audio_cache_key`, `audio_status == COMPLETED`, and `audio/<key>.wav` exists.
2. **Correct voice per segment:** assert each `FakeTTSProvider.synthesize_calls[i].voice_clip_path`
   equals the `source_path` of the segment's speaker's voice clip (narration → narrator clip; Alice
   quote → Alice clip). This is the full-cast proof.
3. **Cache hit ⇒ zero TTS on re-run:** run twice; after the 2nd run a **fresh**
   `FakeTTSProvider.synthesize_calls == []` (no calls), `audio_cache_key`s unchanged, files unchanged.
4. **Key changes on edit ⇒ regeneration:** mutate a segment's `text` (or a speaker's `voice_clip_id`,
   or `project.tts_params`) and re-run; assert **only** the affected segment(s) re-synthesize
   (`synthesize_calls` count == number changed), the new key differs, the new file exists.
5. **Missing-voice precondition (policy A):** the unassigned-voice variant ⇒ FAILED, message names the
   speaker, `stage_status[SYNTHESIZE]` unset, **zero** TTS calls. (If policy B chosen: COMPLETED with
   those segments `audio_status` flagged and the rest rendered.)
6. **Per-segment failure flag-and-continue (§5):** a fake whose `synthesize` raises
   `TTSProviderError` for one specific text ⇒ that segment `audio_status == FAILED`, the rest
   `COMPLETED`, stage COMPLETED; re-run retries only the FAILED one. Plus the **all-failed guard**:
   a fake that raises for *every* segment ⇒ FAILED.
7. **Stop/resume:** `RecordingProgressReporter(stop=True)` ⇒ STOPPED, partial persisted (first
   chapter's segments have keys/files, later don't), status not COMPLETED; resume (fresh fake) ⇒
   COMPLETED, all segments rendered, **no** re-render of the already-cached ones.
8. **`ctx.tts is None` ⇒ FAILED**, status unset, re-runnable; **`FakeTTSProvider(available=False)` ⇒
   FAILED**.
9. **Whitespace-only segment** ⇒ skipped (no TTS call, no file), not an error.
10. **Persisted-then-reloaded** state proven by `store.load()` for the above; `Line.text`/`segments`
    attribution fields (`speaker_id`, `confidence`, `review_status`) untouched.
11. **`is_complete`/`next_stage`:** True after a COMPLETED run; in
    `Pipeline([..., Synthesize, Assemble])` `next_stage` advances to `assemble`; re-run does not
    re-synthesize.
12. **Lazy-import:** extend `tests/test_lazy_imports.py` so importing
    `casttrophizer.providers.tts.chatterbox`, `casttrophizer.audio.synthesize`, and
    `casttrophizer.pipeline.stages.synthesize` loads **no** `torch`/`chatterbox`. (Some chatterbox
    cases already exist; add the stage/orchestration modules.)

**ChatterboxProvider unit test (only if "implement real now" chosen)**
`tests/providers/test_chatterbox.py`: monkeypatch the lazily-imported `chatterbox.tts.ChatterboxTTS`
(and a stub torch) with a fake that returns a canned waveform; assert `synthesize` writes a WAV to
`out_path`, returns the right `sample_rate`/`duration_s`, maps params, and raises `TTSProviderError`
on a model error. **Never load a real model or torch in CI.**

**`FakeTTSProvider` extension (if needed):** add an optional `fail_for: set[str]` (texts to raise on)
and/or `fail_all: bool` so §6 can force per-segment and all-failed failures deterministically. Keep
`synthesize_calls` for the call-count/voice-path assertions. Do not construct a real provider.

---

## 11. Ordered task list (coder)

Dependency order: 0 (decisions) → 1 → 2 → 3 (stage uses 1-2) → 4 (provider, optional-now) → 5/6.

0. **Get user sign-off** on the DECISIONS list below (narrator/missing-voice policy; real Chatterbox
   now vs fake; global vs per-speaker params; per-segment failure policy; rendered-status =
   COMPLETED; stop poll granularity).
1. **Orchestration** — `src/casttrophizer/audio/synthesize.py`: `unresolved_voices(project)`,
   voice-index helper, `synthesize_chapter(...)` with the §3a skip logic + §5 flag-and-continue.
   Pure-ish on the in-memory project (stage owns persistence). Uses the existing `AudioCache`.
2. **SynthesizeStage** — fill `is_complete` + `run` (§9): None/availability guard, precheck,
   per-segment progress, per-chapter (or per-N-segment) `should_stop` + persist, FAILED paths,
   all-failed guard, COMPLETED. No provider in `__init__`.
3. **Wire into the runner/pipeline** as the stage after `review` (confirm `review` stage state; the
   pipeline list lives wherever the runner is assembled — UI/runner helper). Out-of-this-stage:
   confirm the review stage exists/short-circuits.
4. **ChatterboxProvider** (if chosen, §4b) — implement `_load_model`/`synthesize` with lazy
   torch/chatterbox imports, model caching, device handling, WAV write, seed determinism,
   `TTSProviderError` on failure. **Verify the Chatterbox API against the installed package — do not
   hardcode.** Consult package docs.
5. **Fixtures + tests** — `synthesize_ready_project` (+ unassigned-voice variant) in `conftest.py`;
   extend `FakeTTSProvider` (`fail_for`/`fail_all`); `tests/pipeline/test_synthesize_stage.py`; and
   (if §4) `tests/providers/test_chatterbox.py` (§10).
6. **Tooling** — `ruff check`, `black --check`, `mypy src`, `pytest` green; extend
   `tests/test_lazy_imports.py` so the new modules stay torch/chatterbox-free at import.

---

## 12. Risks & open questions

- **Narrator/character voice assignment is unbuilt** (§2a/2b). The attribute stage made speakers
  with **no** voice clips, so a fresh project will FAIL the precondition until a voice-assignment UI
  (or test fixture) sets `voice_clip_id`s. This stage **assumes** voices are assigned; the assignment
  UI is a separate, prerequisite piece of work. Biggest cross-stage gap — flag to user.
- **Real Chatterbox API not verified here.** The coder must read the installed package / docs for the
  exact generate signature, param names (`exaggeration`/`cfg_weight`/...), device handling, and WAV
  output — do not lift from memory.
- **Model memory/GPU cost** (§4b) — one cached model instance per provider; a real render is slow and
  heavy. Not a CI concern (fake only), but a real-run concern to document.
- **Per-speaker params deferred** (§3b/4c) — a real expressiveness want, but a schema change; v1 is
  global.
- **Orphaned cache files** never GC'd (§6) — disk-only cost, acceptable v1.
- **Scope creep guard:** do NOT build the voice-assignment UI, the per-line audio REVIEW stage,
  per-speaker params, orphan GC, M4B assembly, or a narrator-voice schema field here. This stage stops
  at: every renderable segment has a current cached WAV + `audio_cache_key`/`audio_status` set, and
  `stage_status[SYNTHESIZE]=COMPLETED`.

---

## 13. Verification strategy (what the tester must prove)

1. Full-cast voice routing: each segment rendered with its speaker's resolved `voice_clip_path`
   (narration→narrator clip, quote→character clip) — asserted via `synthesize_calls`.
2. Idempotency/cache: cache hit ⇒ **zero** TTS calls on re-run; key+file+status are the skip gate.
3. Change-detection: text/voice/param edit changes the key and re-renders **only** the affected
   segments.
4. Precondition: missing/unassigned voice ⇒ FAILED (policy A), status unset, zero TTS calls.
5. Resilience: per-segment synth error ⇒ that segment `FAILED`, stage COMPLETED, retried next run;
   all-failed ⇒ FAILED; `ctx.tts None`/unavailable ⇒ FAILED.
6. Stop/resume: STOPPED persists partial, status not COMPLETED; resume completes without
   re-rendering cached segments.
7. Persistence proven by reload; attribution fields untouched; WAVs only under `<workspace>/audio/`;
   source clips read-only; no schema bump.
8. Fully offline — `FakeTTSProvider` only, no real model; new modules import no torch/chatterbox.
9. `ruff` / `black` / `mypy src` / `pytest` green.

---

## Handoff

**Coder, once decisions land, build Task 1 first:** the offline orchestration in
`src/casttrophizer/audio/synthesize.py` — specifically `unresolved_voices(project)` (the §2b
precheck) and `synthesize_chapter(...)` with the §3a airtight skip logic (recompute
`AudioCache.key_for`, require key-match AND file-exists AND rendered-status before skipping) and §5
flag-and-continue. It is the seam the stage and all idempotency/voice-routing tests depend on, and it
is fully testable against `FakeTTSProvider` with **no** real TTS. The provider is only ever reached
via `ctx.tts.synthesize` with the **existing** `SynthesisRequest`/`SynthesisResult` interface (no
provider-interface change). Wire the stage (Task 2) next, then the real `ChatterboxProvider` (Task 4)
if the user approves implementing it now.

---

## DECISIONS FOR USER — CONFIRMED (build to these)

1. **Narrator voice & missing-voice precondition (§2a/§2b): CONFIRMED — FAIL-fast precheck.** Narrator
   is an ordinary `Speaker` whose `voice_clip_id` the user assigns like any character (no special
   narrator field, no schema change). Before rendering, scan for any segment whose speaker has no
   assigned/existing voice clip; if any exist, return **FAILED** with a clear message naming the
   unresolved speakers ("assign voices to: Narrator, Alice, …"), stage status left unset so it
   re-runs after voices are assigned. Nothing is partially rendered.
2. **Real `ChatterboxProvider` (§4b): CONFIRMED — implement now.** Build the real provider in this PR
   (lazy `from chatterbox.tts import ChatterboxTTS` + torch INSIDE methods, model loaded/cached once,
   seed determinism, WAV to `out_path`, `SynthesisResult` populated). The user verifies it hands-on
   on a torch/GPU machine. IMPORTANT: the coder CANNOT web-verify and the `tts` extra is NOT installed
   here, so write the Chatterbox call from the known import + standard usage and **clearly mark the
   real call site with a `# VERIFY:` comment** noting the exact Chatterbox API (model load + generate
   signature, audio_prompt path param, sample rate) must be confirmed by the user at runtime. Do NOT
   install the heavy `tts` extra in CI. Stage + ALL tests run on `FakeTTSProvider`; importing the
   provider/stage must not load torch/chatterbox (lazy-import test).
3. **`tts_params` (§3b/§4c): CONFIRMED — global only for v1** (`project.tts_params`, fed identically
   to the cache key and the synthesis request). Per-speaker overrides deferred (no schema bump).
4. **Per-segment failure policy (§5): CONFIRMED — flag-and-continue.** A failing segment gets
   `audio_status=FAILED`, the render continues, the stage returns COMPLETED (failed segments re-render
   next run / surface for review); an **all-failed** run returns FAILED. `ctx.tts is None` /
   model unavailable → FAILED.
5. **(Minor) CONFIRMED — rendered segment status = `ReviewStatus.COMPLETED`** (review stage promotes to
   APPROVED later). No new enum/schema.
6. **(Minor) CONFIRMED — poll `should_stop()` every N segments (N=16) with partial save**, not just
   per chapter, so stop stays responsive on a single huge chapter.

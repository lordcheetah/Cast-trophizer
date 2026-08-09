# UI Slice 5 (FINAL): Per-line (per-segment) audio review — regenerate-now

**Goal:** After synthesize has rendered segments, let the user browse each chapter's rendered
segments, play the take, approve a good one, and **regenerate a bad one in place — rendering that
one segment immediately on a worker thread and auto-playing the fresh take** — completing pipeline
goal #3 ("per-line review") and making the PySide6 review UI feature-complete.

Mirrors the slice 2–4 MVP shape: a pure Qt-free derivation (`review/audio_view.py`), a Qt-free
presenter against a view protocol (`ui/audio_presenter.py`), and a dumb Qt panel
(`ui/audio_panel.py`). The single-segment render runs on a worker thread behind an injected
executor protocol — exactly how `ProjectPresenter` injects `RunExecutor` (slice 1). All state
mutations go through the shared `ReviewService`.

---

## Decisions (final — this is the plan of record)

- **DECISION 1 = (b) REGENERATE-NOW.** Render the one segment immediately on a worker thread,
  then auto-play the new take. Fully specified below (no longer an appendix).
- **DECISION 2 = NO assemble gate.** Approval stays a curation marker. Assemble keeps stitching
  every `RENDERED_STATUSES` segment (COMPLETED **or** APPROVED); it is not required for an M4B.
- **DECISION 3 = (3b) `Segment.audio_seed`.** Schema bump 2→3; a fresh seed is re-rolled on every
  regenerate so the take actually differs even when the project pins a global seed.

**Run-flow: full-run-then-review** (no "run until synthesize" mode). Assemble is cheap; the
in-session regenerate-now covers the audition loop, so there is nothing to defer. After curating,
the user Runs again: SynthesizeStage cache-hits the unchanged (and already-regenerated) segments
and assemble rebuilds the M4B.

---

## Confirmed against the code

- `providers/tts/chatterbox.py` seeds torch from `request.params["seed"]`
  (`torch.manual_seed(int(seed))`, `torch.cuda.manual_seed_all`). So the render must set
  `params["seed"] = segment.audio_seed` to override any global `project.tts_params["seed"]`.
- `TTSProvider` (providers/base.py) has **no** `is_available()` (only `LLMProvider` does). The
  `tts` extra imports lazily inside `chatterbox._load_model` (`from chatterbox.tts import
  ChatterboxTTS`) — the model is **not** loaded at construction. Availability is therefore probed
  cheaply with `importlib.util.find_spec("chatterbox")`; a real load failure surfaces as
  `TTSProviderError` on the first `synthesize`.
- `AudioCache.compute_key` hashes `text \x00 voice_clip_id \x00 params`. Folding `audio_seed`
  **only when non-None** (append `b"\x00seed\x00" + str(seed)`) keeps every existing
  (`audio_seed=None`) segment's key **byte-identical** to today's — so upgrading does **not**
  invalidate the whole cache; only a re-rolled segment gets a new key.
- `audio/synthesize.py` builds `params=dict(project.tts_params)` in the success branch and stamps
  `audio_cache_key`/`audio_status=COMPLETED`; loudness via `audio/loudness.normalize_wav_file`
  (log-and-continue). The regenerate-now core render reuses this exact path.
- `domain/serialization.py`: `CURRENT_SCHEMA_VERSION=2`; migrations are a version-step chain
  (`_migrate_v1_to_v2` defaults a new field). The v2→v3 step mirrors it.
- `store.layout` exposes the `WorkspaceLayout`, so an `AudioCache` is buildable from the store.

---

## Affected / new files

**New — pure-UI MVP trio (no PySide6 except the panel):**
- `src/casttrophizer/review/audio_view.py` — pure derivation: `AudioSegmentRow` +
  `audio_segment_rows(project, cache)` + counts/filters. Imports the workspace `AudioCache`
  (Qt-free). **No PySide6.**
- `src/casttrophizer/ui/audio_presenter.py` — Qt-free `AudioView` + `SegmentRenderExecutor`
  protocols + `AudioPresenter` (owns approve/reject/regenerate orchestration + the play decision,
  builds the TTS provider lazily, drives the injected render executor). **No PySide6 / no
  QtMultimedia.**
- `src/casttrophizer/ui/audio_panel.py` — dumb `AudioPanel(QWidget)` implementing `AudioView`;
  the only new module importing `PySide6.QtMultimedia`. Single panel-owned
  `QMediaPlayer`/`QAudioOutput` (mirror `ui/voice_panel.py`).
- `src/casttrophizer/ui/segment_render_executor.py` — Qt `QtSegmentRenderExecutor(QObject)` +
  `SegmentRenderWorker(QObject)` implementing `SegmentRenderExecutor`: one single-shot QThread per
  regenerate, callbacks forwarded on the main thread. Mirror `ui/run_executor.py` +
  `ui/workers.py`.

**Modified — core / Qt-free:**
- `src/casttrophizer/audio/synthesize.py` — extract the success-branch body into a shared helper
  and add a public, Qt-free `render_segment(project, segment, cache, tts, *, loudness)`; thread
  `segment.audio_seed` into the `SynthesisRequest.params` for **both** paths.
- `src/casttrophizer/review/actions.py` — `approve_audio(segment)`,
  `reroll_audio(segment, *, seed)`.
- `src/casttrophizer/review/service.py` — `approve_audio` (save only), `reroll_audio` (new seed +
  reset + `_invalidate_render` + save), `commit_rendered_audio` (persist after the worker render).
- `src/casttrophizer/domain/models.py` — `Segment.audio_seed: int | None = None`.
- `src/casttrophizer/domain/serialization.py` — (de)serialize `audio_seed`; bump
  `CURRENT_SCHEMA_VERSION` 2→3; add `_migrate_v2_to_v3`.
- `src/casttrophizer/workspace/audio_cache.py` — fold `audio_seed` into `compute_key`/`key_for`
  (only when non-None).
- `src/casttrophizer/app_service/…` — small `tts_extra_available() -> bool`
  (`importlib.util.find_spec("chatterbox") is not None`); export it.
- `src/casttrophizer/ui/presenter.py` — `_has_rendered_audio` predicate, `audio_cache` property,
  `set_audio_available` on `ProjectView`, call it in `_refresh_status`.

**Modified — Qt shell:**
- `src/casttrophizer/ui/main_window.py` — page 4 (`AudioPanel`), `show_audio_page`,
  `review_audio_requested`, "Review audio" button + `set_audio_available` + run-lockout.
- `src/casttrophizer/ui/app.py` — construct `AudioPresenter` + `QtSegmentRenderExecutor`, attach
  in `_on_project_loaded` (pass the cache), wire intents + back + nav.

---

## Interfaces & data shapes

### `review/audio_view.py`
```python
@dataclass(frozen=True)
class AudioSegmentRow:
    segment_id: SegmentId
    chapter_index: int
    chapter_title: str
    line_order: int
    text: str
    speaker_display: str          # reuse the attribution_view narrator-fallback labelling
    audio_status: ReviewStatus    # PENDING / COMPLETED / APPROVED / FAILED
    is_approved: bool             # == APPROVED
    is_failed: bool               # == FAILED
    is_rendered: bool             # in RENDERED_STATUSES (COMPLETED or APPROVED)
    is_playable: bool             # audio_cache_key is not None and cache.has(audio_cache_key)
    wav_path: str | None          # cache.path_for_key(audio_cache_key) when playable else None

def audio_segment_rows(project: Project, cache: AudioCache) -> list[AudioSegmentRow]: ...
def approved_count(rows) -> int: ...
def rendered_count(rows) -> int: ...
```
Whitespace-only segments are non-playable/not rendered. Reuse `find_narrator` + the
`attribution_view._speaker_display` rule so the narrator-fallback name matches the other panels.

### `audio/synthesize.py` — Qt-free single-segment render (point 1)
```python
def render_segment(
    project: Project, segment: Segment, cache: AudioCache, tts: TTSProvider, *,
    loudness: LoudnessSettings | None,
) -> None:
    """Render ONE segment now and stamp it (mirrors synthesize_chapter's success branch).

    Resolves the segment's effective speaker -> voice clip path (reuse _resolved_clip_path +
    resolve_segment_speaker_id/find_narrator); a missing clip raises TTSProviderError. Computes
    the key via cache.key_for (which folds audio_seed), builds a SynthesisRequest whose params
    override seed with segment.audio_seed, calls tts.synthesize to cache.path_for_key(key),
    applies loudness normalization (log-and-continue), then stamps audio_cache_key /
    audio_status=COMPLETED. No Qt, no persistence — the caller persists.
    """
```
Refactor: extract the success-branch body of `synthesize_chapter`
(build request → synthesize → normalize → stamp) into a shared private helper that both
`synthesize_chapter` and `render_segment` call, so the render path is not duplicated. Build the
params via a shared rule: `params = {**project.tts_params}` then `if segment.audio_seed is not
None: params["seed"] = segment.audio_seed`. This makes `synthesize_chapter` also honour a
re-rolled seed on a full run.

### `workspace/audio_cache.py` — fold the seed (point 2)
```python
@staticmethod
def compute_key(segment_text, voice_clip_id, tts_params, audio_seed: int | None = None) -> str:
    ...  # existing text/voice/params hashing, then ONLY when audio_seed is not None:
    #   h.update(b"\x00seed\x00"); h.update(str(audio_seed).encode("utf-8"))

@classmethod
def key_for(cls, segment, project) -> str:
    return cls.compute_key(segment.text, cls._voice_clip_id_for(segment, project),
                           project.tts_params, segment.audio_seed)
```
Folding **only when non-None** keeps existing (`None`) segments' keys unchanged → no wholesale
cache invalidation on upgrade.

### `review/actions.py` (pure)
```python
def approve_audio(segment: Segment) -> None:
    segment.audio_status = ReviewStatus.APPROVED           # COMPLETED -> APPROVED (both RENDERED)

def reroll_audio(segment: Segment, *, seed: int) -> None:
    segment.audio_seed = seed                              # new seed -> key changes -> fresh render
    segment.audio_cache_key = None                         # mirror the synth FAILED clear-key path
    segment.audio_status = ReviewStatus.PENDING
```

### `review/service.py`
```python
def approve_audio(self, segment) -> None:
    actions.approve_audio(segment); self._save()           # no _invalidate_render, no _invalidate_review

def reroll_audio(self, segment) -> None:
    seed = _new_seed(segment.audio_seed)                    # see "seed source" below
    actions.reroll_audio(segment, seed=seed)
    self._invalidate_render()                              # re-open SYNTHESIZE + ASSEMBLE (slice-4 reuse)
    self._save()

def commit_rendered_audio(self, segment) -> None:
    """Persist a segment the worker already stamped COMPLETED in memory; keep ASSEMBLE re-opened."""
    self._save()                                          # ASSEMBLE stays popped from reroll_audio -> M4B rebuilds
```
Audio review is **post-synthesize**: none of these touch `_invalidate_review` (the pre-synth
criterion-1/2/3 gate). `reroll_audio` re-opens the render stages; after the worker render the
segment is COMPLETED+current so the next full run cache-hits SYNTHESIZE and assemble (still
re-opened) rebuilds.

**Seed source (point 2):** plain Python app code — `random.randrange(1, 2**31 - 1)` (int32 range
so torch accepts it), re-drawing if it equals the current `audio_seed` so a re-roll always
differs. `secrets.randbelow`/`random.SystemRandom` are fine too; determinism is not wanted here
(we want a *different* take), so any well-distributed source works.

### `ui/audio_presenter.py` — presenter + injected executor protocol (point 5)
```python
@runtime_checkable
class AudioView(Protocol):
    def show_segments(self, rows: list[AudioSegmentRow]) -> None: ...
    def show_progress(self, approved: int, rendered: int) -> None: ...      # "N of M approved"
    def select_segment(self, index: int) -> None: ...
    def play_clip(self, path: str) -> None: ...
    def stop_playback(self) -> None: ...                                    # releases the file handle
    def set_regenerate_available(self, available: bool) -> None: ...        # false when tts extra absent
    def set_busy(self, busy: bool) -> None: ...                             # disable approve/regen/back mid-render
    def show_error(self, title: str, message: str) -> None: ...

@runtime_checkable
class SegmentRenderExecutor(Protocol):
    def start(self, tts, project, segment, cache, *, loudness,
              on_finished: Callable[[], None], on_failed: Callable[[str], None]) -> None: ...
    # runs audio.render_segment on a worker thread; both callbacks fire on the MAIN thread.

class AudioPresenter:
    def __init__(self, view, executor, deps, *, on_reviewed): ...  # deps -> tts_factory + resolved_config
    def attach(self, service, cache): ...     # adopt shared service + cache; stop playback;
                                              # view.set_regenerate_available(tts_extra_available())
    def open(self): ...                       # derive + push rows + progress
    # intents
    def approve(self, segment_id): ...        # service.approve_audio -> refresh -> on_reviewed
    def reject(self, segment_id): ...         # service.reroll_audio only (DEFER: re-render on next full run)
    def regenerate(self, segment_id): ...     # render-now, below
    def play(self, segment_id): ...           # wav_path None -> show_error; else stop_playback + play_clip
    def set_filter(self, unapproved_only): ...
    def next_segment(self); def prev_segment(self); def play_next(self); def set_selected(self, i): ...
```

**`regenerate` orchestration (point 5), all decisions in one flow:**
1. Guard: if `self._rendering` is already set, ignore (single-flight).
2. `service.reroll_audio(segment)` — sets a new `audio_seed`, resets key/status, `_invalidate_render`,
   persists. (This is the same call `reject` makes; regenerate additionally renders now.)
3. Build the TTS provider lazily (`self._tts = self._tts or self._deps.tts_factory(config)`); if
   `tts_extra_available()` is False, skip the worker: the segment is already re-rolled/deferred —
   `view.set_regenerate_available(False)`, an info message ("Chatterbox unavailable — this segment
   will re-render on the next full Run"), refresh, `on_reviewed`, done.
4. `self._rendering = True`; `view.set_busy(True)`; `view.stop_playback()` (release the WAV handle
   before it is overwritten).
5. `executor.start(self._tts, project, segment, cache, loudness=..., on_finished=..., on_failed=...)`.
6. **on_finished** (main thread): the worker already stamped the segment COMPLETED in memory →
   `service.commit_rendered_audio(segment)` (persist); `self._rendering=False`; `view.set_busy(False)`;
   refresh rows/progress; `on_reviewed`; then **auto-play** the new take (`self.play(segment_id)`).
7. **on_failed(msg)** (main thread): `self._rendering=False`; `view.set_busy(False)`; if the
   failure is a `TTSProviderError` (model load failed) degrade — `view.set_regenerate_available(False)`
   + `show_error` — the segment stays re-rolled/PENDING so a later full Run re-renders it; refresh.

`loudness = LoudnessSettings.from_params(project.tts_params)` (may be `None`). Provider
construction is cheap (no model load); the heavy load happens inside the worker on first
`synthesize`.

### `ui/segment_render_executor.py` — Qt worker (point 3, mirror run_executor/workers)
- `SegmentRenderWorker(QObject)`: holds `(tts, project, segment, cache, loudness)`; `Signal
  finished()`, `Signal failed(str)`. `@Slot() def run()`: `try: audio.render_segment(...) ; emit
  finished()` / `except TTSProviderError as e: emit failed(str(e))` / `except Exception as e: emit
  failed(...)`.
- `QtSegmentRenderExecutor(QObject)`: implements `SegmentRenderExecutor.start`. Raises/ignores if a
  render is already in flight (belt-and-braces with the presenter guard). Per render: new
  `QThread` + `SegmentRenderWorker`, `moveToThread`, `thread.started→worker.run`,
  `worker.finished→_forward_finished`, `worker.failed→_forward_failed` (queued → main thread),
  and — per slice-1's teardown fix — `thread.finished→worker.deleteLater` +
  `thread.finished→thread.deleteLater` wired **before** start. `_forward_*` capture the callback,
  `_teardown()` (quit+wait, null state), then invoke the callback. Single-shot: a fresh
  thread/worker per regenerate (QThreads are not cleanly restartable).

### `ui/presenter.py` additions
```python
def _has_rendered_audio(project) -> bool:
    return any(seg.audio_status in RENDERED_STATUSES
               for ch in project.book.chapters for ln in ch.lines for seg in ln.segments)

@property
def audio_cache(self) -> AudioCache | None:
    return AudioCache(self._store.layout) if self._store is not None else None
```
`_refresh_status` also calls `self._view.set_audio_available(_has_rendered_audio(project))`;
`ProjectView` gains `set_audio_available`; `MainWindow.set_running` disables the Review-audio
button while a pipeline run is in flight.

---

## Ordered tasks

1. **`review/audio_view.py`** + unit tests — `AudioSegmentRow`, `audio_segment_rows(project,
   cache)`, `approved_count`, `rendered_count`, filters. Pure/offline; identical regardless of the
   render mechanism. **Build this first.**
2. **Seed plumbing + schema 2→3** (point 2):
   - `domain/models.py`: `Segment.audio_seed: int | None = None`.
   - `domain/serialization.py`: `_segment_to_dict`/`_segment_from_dict` handle `audio_seed`
     (from_dict uses `.get("audio_seed")` so a pre-migration dict is safe); bump
     `CURRENT_SCHEMA_VERSION=3`; add `_migrate_v2_to_v3` (walk chapters→lines→segments,
     `seg.setdefault("audio_seed", None)`) and the `version == 2` step in `_migrate`.
   - `workspace/audio_cache.py`: fold `audio_seed` into `compute_key`/`key_for` (only when
     non-None).
3. **`audio/synthesize.py`** — extract the shared render+stamp helper; add public
   `render_segment(...)`; thread `segment.audio_seed` into the request params for both paths.
4. **`app_service`** — `tts_extra_available()`; export it.
5. **`review/actions.py`** + **`review/service.py`** — `approve_audio`, `reroll_audio`,
   `commit_rendered_audio`; the `_new_seed` helper. Docstrings state *why no `_invalidate_review`*.
6. **`ui/audio_presenter.py`** — `AudioView` + `SegmentRenderExecutor` protocols + `AudioPresenter`
   (the full regenerate flow, availability probe, single-flight guard, auto-play, graceful
   degrade). No PySide6.
7. **`ui/segment_render_executor.py`** — `QtSegmentRenderExecutor` + `SegmentRenderWorker` (Qt
   worker, slice-1 teardown, main-thread callbacks).
8. **`ui/audio_panel.py`** — dumb panel: row list, filter(s), progress, ▶/Approve/Regenerate +
   Play-all/Next, single `QMediaPlayer` (copy voice_panel lifecycle), `set_busy`/
   `set_regenerate_available` button enablement, `stop_playback` releasing the handle, `closeEvent`.
9. **`ui/presenter.py`** — `_has_rendered_audio`, `audio_cache`, `set_audio_available`.
10. **`ui/main_window.py`** — page 4, `show_audio_page`, `review_audio_requested`, button,
    `set_audio_available`, run-lockout.
11. **`ui/app.py`** — construct `AudioPresenter` + `QtSegmentRenderExecutor`; attach in
    `_on_project_loaded` with `presenter.audio_cache`; wire panel intents, back, nav.
12. **Tests** — land with each unit (see Verification).

---

## Threading + lifecycle (point 3)

- The single-segment render runs on a **fresh single-shot `QThread`** per regenerate (recommended
  over reusing one long-lived thread — QThreads are not cleanly restartable, and this mirrors
  `QtRunExecutor`'s proven per-run thread). The Qt-free presenter never touches a thread; it drives
  the injected `SegmentRenderExecutor`, and its callbacks land on the **main thread** (queued
  connections via the `QObject` executor).
- **Provider construction is in the review session, lazy and cheap** (`deps.tts_factory(config)` —
  no model load); the model loads inside the worker on the first `synthesize`.
- **Single-flight:** one regenerate at a time — the presenter's `_rendering` guard **and** the
  executor's in-flight guard. While busy the panel disables Approve, Regenerate, **and Back**
  (`set_busy(True)`), preventing a second render or navigating away mid-render; re-enabled on
  finish/fail.
- **Windows file handle:** the player must `setSource(QUrl())` (via `stop_playback`) **before** the
  render overwrites the WAV at the cache path, **and** before the auto-play replay — otherwise the
  open handle blocks the overwrite on Windows. (Note: a re-roll changes the key → a *new* path, so
  the overwrite risk is mainly on repeated re-rolls landing on a prior path; releasing the handle
  unconditionally before each render is the safe rule.)
- **Teardown:** `thread.finished → worker.deleteLater` + `thread.finished → thread.deleteLater`
  wired before `start`; `_teardown()` quits+joins; state nulled for the next render (slice-1's fix
  — an imperative `deleteLater` after `quit()`/`wait()` would post to a dead loop and leak the
  worker, which pins the TTS provider).
- **Panel-wide run-lockout still applies:** the shell disables the Review-audio entry while a
  pipeline run is in flight (`MainWindow.set_running`), so a pipeline run and a regenerate can't
  overlap through the UI.

---

## Graceful degradation (point 4)

- On `attach`, the presenter probes `tts_extra_available()` and calls
  `view.set_regenerate_available(...)`. When the `tts` extra (Chatterbox/torch) is absent — CI, or
  a user without torch — **Regenerate is disabled** with a clear tooltip/message; **Play and
  Approve still work** (they need no provider).
- If availability is True but the model load fails at render time, the worker raises
  `TTSProviderError` → `on_failed` degrades: disable Regenerate + a clear `show_error`; the segment
  is already re-rolled/PENDING so the next full Run re-renders it. Never crashes.
- `reject` (defer) never needs the provider — it only re-rolls + marks for the next Run — so it
  remains available even when regenerate-now is disabled.

---

## Risks & open questions

- **Single-segment worker teardown** — follow slice-1's `thread.finished→deleteLater` exactly; a
  leaked worker pins the (heavy) TTS provider/model. Verify no orphaned thread across repeated
  re-rolls.
- **Windows file-handle release** — `setSource(QUrl())` before overwrite *and* before replay;
  test that stop→render→auto-play doesn't hit a locked file.
- **Provider-unavailable** — probe with `find_spec`, degrade cleanly; the real-model-load failure
  path (TTSProviderError on the worker) must also degrade, not crash.
- **Seed source** — must differ from the current `audio_seed` (re-draw on collision) so a re-roll
  always produces a new key/take; int32 range for torch.
- **Cache-key back-compat** — fold `audio_seed` only when non-None so upgrading a v2 project
  doesn't invalidate every cached WAV (assert an unchanged key for `audio_seed=None`).
- **Cross-thread stamp** — the worker stamps the shared `Segment` on the worker thread; the queued
  `finished` signal provides the happens-before before the main thread persists + reads. Safe under
  single-flight (the row shows busy; nothing else mutates that segment concurrently). Alternative if
  preferred: have the worker return the key and do the stamp on the main thread in
  `commit_rendered_audio(segment, key)` — note this as an acceptable variant.
- **Approval vs later edits** — a slice-4 text/voice edit on an approved segment resets it to
  PENDING (audio changed) — correct; the audio panel re-derives on `open()`/after a run.
- **Stale-WAV interplay with slice-4 invalidation** — `audio_view` derives from the live shared
  project + `cache.has`, so a dirtied segment shows PENDING/non-playable automatically; confirm each
  page entry re-runs `presenter.open()` (it does, via `app.py`'s `_open_*` closures).

---

## Verification strategy (MOCK the worker/provider — no GPU/TTS in CI)

- **`review/audio_view.py` unit tests:** `audio_status` mapping; `is_approved`/`is_failed`/
  `is_rendered`/`is_playable`/`wav_path` against a fake `AudioCache` (stub `has`/`path_for_key`)
  or tmp WAVs; whitespace-only non-playable; narrator-fallback `speaker_display` matches
  attribution; `approved_count`/`rendered_count`; filters.
- **Seed / schema / cache tests:** `Segment` round-trips `audio_seed`; a **v2 fixture migrates**
  (segments gain `audio_seed=None`); `AudioCache.compute_key` is **unchanged** for
  `audio_seed=None` and **changes** for a non-None seed; `render_segment`/`synthesize_chapter`
  pass `params["seed"] == segment.audio_seed` to a fake `TTSProvider`.
- **`audio.render_segment` unit test** (fake `TTSProvider` + tmp cache): renders to
  `cache.path_for_key(key)`, applies loudness (fake/settings), stamps
  `audio_cache_key`/`audio_status=COMPLETED`; a missing voice clip raises `TTSProviderError`; a
  `TTSProviderError` from the fake propagates (no partial stamp).
- **`AudioPresenter` tests** (fake `AudioView` + fake `SegmentRenderExecutor` + real shared
  `ReviewService` over an in-memory `Project` + fake/tmp cache), loop-free:
  - `approve` flips COMPLETED→APPROVED and persists.
  - `regenerate` calls `service.reroll_audio` → asserts a **new** `audio_seed`, reset key/PENDING,
    and popped `stage_status[SYNTHESIZE]`+`[ASSEMBLE]`; asserts the executor is invoked with the
    right `(project, segment, cache, seed-in-params)`; on the fake executor's `on_finished`, asserts
    `commit_rendered_audio` persisted and the view **auto-played** the new WAV path.
  - **Single-flight guard:** a second `regenerate` while `_rendering` is a no-op.
  - **Graceful-unavailable:** with `tts_extra_available()` False (patched), `regenerate` re-rolls +
    defers (no executor call) and `set_regenerate_available(False)`; `on_failed(TTSProviderError)`
    degrades likewise; **`play`/`approve` unaffected**.
  - `play` fires `play_clip(cache.path_for_key(key))`; missing WAV → `show_error`, not `play_clip`.
  - filter, `next/prev`, `play_next` selection logic.
- **`ProjectPresenter` gating test:** `_has_rendered_audio`; `set_audio_available` toggles true once
  a segment is COMPLETED/APPROVED; disabled while running.
- **Offscreen Qt smoke** (`QT_QPA_PLATFORM=offscreen`): `AudioPanel` renders rows; ▶/Approve/
  Regenerate wire to intents; **mock `QMediaPlayer`** asserts `setSource`/`play`; `stop_playback`
  clears the source; `set_busy`/`set_regenerate_available` toggle button enablement. For
  `QtSegmentRenderExecutor`, **mock the worker/provider** (or inject a fake `render_segment`) — CI
  needs no GPU/audio device — and assert callbacks fire on the main thread + clean teardown.

---

## Handoff

**Build first: `src/casttrophizer/review/audio_view.py`** — the pure `AudioSegmentRow` +
`audio_segment_rows(project, cache)` derivation with unit tests (the Qt-free foundation every later
task consumes). Then the seed/schema plumbing (task 2) and `audio.render_segment` (task 3), since
the presenter and worker both depend on them. This slice completes the review UI.

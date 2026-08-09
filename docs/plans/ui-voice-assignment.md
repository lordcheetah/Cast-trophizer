# Plan: PySide6 review UI — Slice 3 "Voice assignment"

## Goal
Let the user assign reference voice clips to the cast in the GUI — assign a `.wav` to a
speaker, unassign, change a speaker's `VoiceCategory`, **audition** a candidate/assigned clip
by ear, and **bulk-assign by category** to every still-unvoiced referenced speaker — so
review-gate **criterion 3 (unassigned voices)** can be resolved, with the shell's "voices
needed for: X" summary updating live.

---

## DECISIONS (resolved by the coordinator)

### Fork 1 — Reference-clip audition: **INCLUDED in this slice** (first-class, not deferred).
A **▶ play** button auditions a candidate/assigned reference clip — you pick the right clip by
ear before committing. This applies both to the **per-speaker** action row (play the selected
speaker's assigned clip) and to the **bulk-category picker** rows (play the clip chosen for
man/woman/boy/girl/default before applying). Qt multimedia is scoped tightly (see "Audition"
below): `QMediaPlayer` + `QAudioOutput` live **only** in the Qt panel; the presenter and the
pure derivation stay multimedia-free and PySide6-free.

### Fork 2 — Category-editing granularity: **per-speaker only** (as recommended).
Ship a per-speaker category `QComboBox`. The post-attribution classification pass already
stamps categories; per-speaker fixes cover corrections, and bulk-assign-by-category is the real
time-saver. A "bulk-set all UNKNOWN" helper is a trivial follow-up — not in this slice.

---

## Where this sits on the pipeline
No new stage. Pure **review-stage editing UI** over already-attributed state: it reads
`project.speakers` / `project.voice_clips` and mutates them through the existing
`ReviewService`, which persists via `WorkspaceStore` and invalidates the review-gate flag on
`unassign`. Resumability is unchanged — each edit is a synchronous in-memory dataclass
mutation plus one atomic `project.json` write, exactly as `ReviewService` already does for the
CLI. Audition only **reads** a clip file (playback); it mutates nothing. **No provider/LLM/TTS
call in this slice.**

Out of scope (later slices — list-only, do NOT build): text-suggestion review (criterion 2),
per-line playback/approve/regenerate of **rendered** segments (this slice auditions the user's
own **reference** clips, NOT TTS output), and synth/assemble (already driven by slice 1).

---

## The anti-drift rule (the load-bearing constraint of this slice)
"Which speakers still need a voice" and "which speakers bulk-assign targets" must both come
from the **same predicate the gate and the render precheck use** —
`casttrophizer.audio.synthesize.unresolved_speakers(project)` (and its sibling
`unresolved_voices`). The UI derives `needs_voice` from `unresolved_speakers`; bulk-assign
targets exactly `unresolved_speakers` filtered by category. The UI must **never** re-implement
"has a usable voice" (the `voice_clip_id set + clip present + file on disk` check) — that lives
in `_resolved_clip_path` and is reached only via `unresolved_speakers`.

One supporting addition: to render an `is_referenced` column without re-walking segments (which
would risk drift), **expose `referenced_speaker_ids(project)` publicly** from
`audio/synthesize.py` — a thin public alias over the existing private `_referenced_speaker_ids`
(add it to `__all__`). `needs_voice` still comes from `unresolved_speakers`; `is_referenced`
comes from `referenced_speaker_ids`; neither is re-derived in the UI.

---

## New / modified files

### New — pure, Qt-free
- `src/casttrophizer/review/voice_view.py` — the view-model **derivation** (mirrors
  `review/attribution_view.py`): `SpeakerVoiceRow`, `CategoryOption`, `speaker_voice_rows`,
  `category_options`, `needs_voice_count`. Reads the project only. **No PySide6, no multimedia.**
- `src/casttrophizer/app_service/voices.py` — the **shared category-clip resolution** extracted
  from `cli.py` so the CLI `--rest` path and the UI bulk-assign call one implementation:
  `resolve_category_clip`, `BulkVoicePlan`, `plan_bulk_voice`, `apply_bulk_voice`.

### New — Qt (confined to `ui/`)
- `src/casttrophizer/ui/voice_presenter.py` — `VoiceView` Protocol + `VoicePresenter`
  (Qt-free; imports **no** PySide6 and **no** multimedia), mirroring
  `ui/attribution_presenter.py`. The Protocol exposes a `play_clip(path)` hook so "play the
  selected speaker's clip" logic is decided in the presenter and testable without audio.
- `src/casttrophizer/ui/voice_panel.py` — the dumb `VoiceView` Qt widget, mirroring
  `ui/attribution_panel.py`. **The only place `PySide6.QtMultimedia` (`QMediaPlayer` /
  `QAudioOutput`) is imported.**

### New — behaviour on `ReviewService` / actions (category setter — none exists today)
- `review/actions.py` — add `set_speaker_category(speaker, category)` (pure:
  `speaker.category = category`).
- `review/service.py` — add `ReviewService.set_speaker_category(speaker, category)`
  (apply + save; **no** review-flag invalidation — category affects neither the gate nor the
  `AudioCache` key, only which default clip `--rest`/bulk picks).

### Modified
- `src/casttrophizer/audio/synthesize.py` — publish `referenced_speaker_ids` (alias +
  `__all__`).
- `src/casttrophizer/cli.py` — refactor `_cmd_assign_voice_rest` / `_resolve_category_clip`
  to call the new `app_service/voices.py` (behaviour identical; existing
  `tests/cli/test_cli_assign_voice_rest.py` must stay green).
- `src/casttrophizer/ui/main_window.py` — add page 2 (the voice panel) to the stack, an
  **"Assign voices"** shell button + `assign_voices_requested` callback,
  `show_voice_page()`, a `set_voice_available(bool)` `ProjectView` method, and run-lockout
  of the new button in `set_running`.
- `src/casttrophizer/ui/presenter.py` — push `set_voice_available` in `_refresh_status`; add
  the `set_voice_available` method to the `ProjectView` Protocol. (No change to
  `refresh_after_review` — voice edits reuse it verbatim.)
- `src/casttrophizer/ui/app.py` — construct the voice panel + presenter, wire intents/nav
  (including the audition hook), and combine `on_project_loaded` so **both** review presenters
  re-attach on load and the voice panel's player is reset on project switch.

### Dependency note
`PySide6.QtMultimedia` ships **inside the existing `PySide6>=6.6` wheel** (verified: it imports
without any extra package, as `PySide6/QtMultimedia.pyd`). **No new pip dependency and no
change to the `dev` extra is required.** `QtMultimedia` is Qt-only, so the isolation guard
(`tests/test_qt_isolation.py`) is unaffected as long as the import stays inside
`ui/voice_panel.py`. (Runtime caveat, not a build one: on headless Linux CI the multimedia
plugin may have no audio backend — the panel must construct the player lazily/defensively and
tests must not require real playback; see Verification.)

---

## Data shapes (new, Qt-free — `review/voice_view.py`)

```
@dataclass(frozen=True)
class SpeakerVoiceRow:
    speaker_id: SpeakerId
    name: str
    role: SpeakerRole
    category: VoiceCategory
    voice_clip_label: str | None      # assigned clip's label, else None
    voice_clip_path: str | None       # assigned clip's source_path, else None (feeds ▶ play)
    is_referenced: bool               # in referenced_speaker_ids(project)
    needs_voice: bool                 # in unresolved_speakers(project) — the criterion-3 flag
    clip_file_missing: bool           # voice_clip_id set but file absent (a needs_voice reason)

@dataclass(frozen=True)
class CategoryOption:
    value: VoiceCategory
    display: str                      # e.g. "man" / "woman" / … / "unknown"
```

- `speaker_voice_rows(project) -> list[SpeakerVoiceRow]` — one row per `project.speakers`.
  `needs_voice` = membership in `unresolved_speakers(project)` (build the id-set once);
  `is_referenced` = membership in `referenced_speaker_ids(project)`; the clip fields resolve
  `voice_clip_id → project.voice_clips`; `clip_file_missing` = id set but `Path(source_path)`
  absent. **Both membership sets are computed by calling the synthesize helpers — never
  re-derived.** `voice_clip_path` is what the panel hands to `play_clip` for audition.
- `category_options() -> list[CategoryOption]` — every `VoiceCategory` member.
- `needs_voice_count(rows) -> int` — `sum(r.needs_voice for r in rows)` (equivalently
  `len(unresolved_speakers(project))`; assert they agree in a test).

The panel should make `needs_voice` rows prominent (bold + accent, sorted/filterable to top);
an unreferenced, unvoiced speaker is shown but **not** flagged (it isn't a blocker).

---

## Shared category resolution (`app_service/voices.py`) — the CLI/UI drift seam

Extract from `cli.py` (verbatim precedence: per-category flag/override > matching env default
> shared `--default`/`CASTTROPHIZER_VOICE_DEFAULT`), taking a plain `overrides` mapping instead
of an `argparse.Namespace` so it is front-end-agnostic:

```
REST_CATEGORY_KEYS = ("man", "woman", "boy", "girl")   # 'unknown' has no key → uses default

def resolve_category_clip(
    category: str, overrides: Mapping[str, str | None], config: AppConfig
) -> str | None: ...
    # overrides has per-category keys + "default"; returns None when nothing resolves.

@dataclass(frozen=True)
class BulkVoicePlan:
    assignments: list[tuple[Speaker, str]]      # (speaker, resolved clip path)
    uncovered: dict[str, list[str]]             # category value -> speaker names with no clip

def plan_bulk_voice(project, overrides, config) -> BulkVoicePlan: ...
    # targets = unresolved_speakers(project); resolve each; split resolved/uncovered.
    # PURE — no mutation, no I/O, no path-existence check (that's apply's job).

def apply_bulk_voice(service: ReviewService, plan: BulkVoicePlan) -> dict[str, int]: ...
    # register one shared VoiceClip per distinct path (validates each exists → ValueError),
    # then service.assign_voices(pairs). Returns per-category counts. Caller must reject a
    # non-empty plan.uncovered FIRST (never leave a referenced speaker unvoiced).
```

- **CLI** (`_cmd_assign_voice_rest`): build `overrides` from `args`
  (`{k: getattr(args, k) for k in REST_CATEGORY_KEYS} | {"default": args.default}`), call
  `plan_bulk_voice`, raise the same `CliError` on `uncovered`, else `apply_bulk_voice`. Output
  string unchanged. `tests/cli/test_cli_assign_voice_rest.py` is the regression guard.
- **UI** (`VoicePresenter.bulk_assign_by_category`): build `overrides` from the panel's
  per-category clip pickers, call the same `plan_bulk_voice`; on `uncovered` →
  `view.show_error(...)` (do NOT assign anything); else `apply_bulk_voice(self._service, plan)`
  then `_after_edit()`.

Layering note: `app_service` sits above `review`, so importing `ReviewService`/`actions` and
`audio.synthesize.unresolved_speakers` here is fine; it imports **no** Qt, so
`tests/test_qt_isolation.py` still covers the module.

---

## Audition (Fork 1, in-scope) — where the multimedia boundary sits

**Presenter side (Qt-free, testable without audio).** `VoiceView` exposes a single play hook:

```
def play_clip(self, path: str) -> None: ...   # the panel plays it; the presenter only decides WHAT
def stop_playback(self) -> None: ...          # the panel stops + resets its player
```

The presenter owns the *decision* ("play the selected speaker's clip", "play the man-category
picker's clip") and validates the target before asking the view to play:

- `audition_speaker(speaker_id)` — resolve the speaker's `voice_clip_path`; if it is `None`
  (unvoiced) or the file is missing → `view.show_error(...)`; else `view.play_clip(path)`.
- `audition_path(path)` — for a bulk-category picker clip the user already chose; same
  missing-file guard, then `view.play_clip(path)`.

So the "what to play / is it playable" logic is unit-tested by asserting `play_clip` is invoked
with the expected path (and `show_error` on a missing file) — **no audio device required**.

**Panel side (the ONLY multimedia code).** The panel owns exactly one `QMediaPlayer` +
`QAudioOutput`, constructed lazily/defensively, and implements `play_clip(path)`:
`player.stop()` → `setSource(QUrl.fromLocalFile(path))` → `play()`. ▶ buttons live on:
- the per-speaker action row (plays the selected row's assigned clip — disabled when the
  selected row has no `voice_clip_path`), and
- each bulk-category picker row (plays that category's chosen clip — disabled until a clip is
  picked).

Both buttons translate a click into a presenter intent (`audition_speaker` /
`audition_path`); the panel never decides playability itself beyond enabling/disabling the
button from the row/picker state.

**Lifecycle / teardown (panel-owned):**
- One player per panel, created once (or lazily on first play).
- **Stop + release on panel teardown** (`closeEvent`/`deleteLater` path) and **on project
  switch** — `VoicePanel.stop_playback()` (called from the presenter's `attach`, which the
  combined `on_project_loaded` fires on every project load) stops the player and clears its
  source, so a clip from the previous project isn't left playing/holding a handle when a new
  project loads.
- **Missing/deleted-file guard:** the presenter's missing-file check is the first gate; the
  panel additionally connects `QMediaPlayer.errorOccurred` to surface a friendly error via
  `show_error` rather than letting a backend error escape (covers a file deleted between the
  presenter check and playback, and a headless CI with no audio backend). Never crash.

---

## Presenter (`ui/voice_presenter.py`, Qt-free)

`VoiceView` Protocol (render-only + the audition hooks; the panel implements it structurally,
tests fake it):
- `show_speakers(rows: list[SpeakerVoiceRow]) -> None`
- `show_category_options(options: list[CategoryOption]) -> None`
- `show_needs_voice(needs_voice: int, referenced_total: int) -> None`  (the progress line)
- `select_speaker(index: int) -> None`
- `play_clip(path: str) -> None`  (audition — the panel plays; the presenter only decides)
- `stop_playback() -> None`  (called on re-`attach` / project switch so the panel resets its player)
- `show_error(title: str, message: str) -> None`

`VoicePresenter(view, *, config: AppConfig, on_reviewed: Callable[[], None])`:
- `attach(store)` — `ReviewService(store, store.load())`; reset selection/filter; call
  `view.stop_playback()` (project switch must not leave a prior clip playing).
- `open()` — push `show_category_options(category_options())`, then `_render()`.
- Mutating intents (each resolves the speaker by id in `service.project`, calls the matching
  `ReviewService` method, then `_after_edit()`; wrap `ValueError` → `show_error`):
  - `assign(speaker_id, wav_path)` → `clip = service.register_voice_clip(wav_path,
    label=speaker.name)` then `service.assign_voice(speaker, clip)`. A missing/renamed file
    raises `ValueError` from `register_voice_clip` (it checks existence) → `show_error`, nothing
    committed. **The Qt panel opens the `QFileDialog` and passes a real path string; the
    presenter never opens dialogs** (mirrors `reassign_new`).
  - `unassign(speaker_id)` → `service.unassign_voice(speaker)` (re-opens the gate).
  - `set_category(speaker_id, category)` → `service.set_speaker_category(speaker, category)`.
  - `bulk_assign_by_category(overrides: dict[str, str | None])` → `plan_bulk_voice` →
    on `uncovered` `show_error` (list the categories + speaker names, no mutation) →
    else `apply_bulk_voice(service, plan)`.
- Audition intents (read-only; no `_after_edit`, no `on_reviewed`):
  - `audition_speaker(speaker_id)` → resolve `voice_clip_path`; `None`/missing → `show_error`;
    else `view.play_clip(path)`.
  - `audition_path(path)` → missing-file guard → `view.play_clip(path)`.
- `set_filter(needs_voice_only: bool)` — view-state only; re-`_render()` (default ON).
- `_render()` — `rows = speaker_voice_rows(project)`; push filtered rows +
  `show_needs_voice(needs_voice_count(all_rows), referenced_total)`.
- `_after_edit()` — `_render()` then `on_reviewed()` (= `ProjectPresenter.refresh_after_review`,
  so the shell's "voices needed for: X" line drops live — **the exact slice-2 wiring**).

Config: the presenter needs `AppConfig` for the env-default fallback in bulk resolution;
`ui/app.py` injects `deps.resolved_config()`. `AppConfig` is Qt-free.

---

## Panel (`ui/voice_panel.py`, Qt — dumb `VoiceView`)
Mirror `attribution_panel.py`; **the only module importing `PySide6.QtMultimedia`**:
- **Cast list** (`QListWidget`/table): name, role, category, voice state ("NO VOICE" or the
  clip label), a referenced indicator. `needs_voice` rows are bold + accented and tagged
  (e.g. "NEEDS VOICE" / "assigned but file missing" when `clip_file_missing`). A **"Needs
  voice only"** `QCheckBox` (default ON) → `set_filter`.
- **Per-selection action row:** **Assign clip…** (`QFileDialog.getOpenFileName`, filter
  `WAV (*.wav)`, then `assign(speaker_id, path)`), **Unassign**, a category `QComboBox`
  (populated from `show_category_options`) → `set_category`, and a **▶** button →
  `audition_speaker(speaker_id)` (disabled when the selected row has no `voice_clip_path`).
- **Bulk-assign panel:** one small row per category (man/woman/boy/girl/default) with a
  "Pick…" button + a label showing the chosen path (or the env default), a **▶** button →
  `audition_path(picked_path)` (disabled until a clip is picked), and an **Apply to unvoiced**
  button → `bulk_assign_by_category(overrides)`. Show a live "still need voices: man ×3,
  woman ×2" summary so the user sees exactly what Apply will touch (nothing silently
  committed).
- **Audition player:** one panel-owned `QMediaPlayer` + `QAudioOutput`; `play_clip(path)`
  stops-then-plays; `stop_playback()` stops + clears the source; `errorOccurred` →
  `show_error`. Teardown on `closeEvent`/`deleteLater`.
- Progress label ("`N of M referenced speakers still need a voice`") ← `show_needs_voice`.
- **Back to project** button → shell page 0.
- Emits intent callbacks wired by `ui/app.py`; holds no voice logic and never re-checks
  "has a usable voice".

---

## Shell integration & navigation
Two review surfaces now coexist (attribution = page 1, voices = page 2). **Recommend two shell
buttons, not a hub:** keep the existing "Review attributions" button and add an **"Assign
voices"** button beside it. A dedicated "review hub" page is over-engineering for two entry
points — note it as a future consolidation once the text-suggestion and audio-review surfaces
land (then a hub earns its keep).

- `main_window.py`: `_stack.addWidget(self.voice_panel)` (page 2); `show_voice_page()`
  (`setCurrentIndex(2)`); "Assign voices" `QPushButton` → `assign_voices_requested`; new
  `ProjectView` method `set_voice_available(bool)` enabling it; in `set_running(True)` **also**
  disable the voice button (run-lockout — see below).
- `presenter.py`: `ProjectView` Protocol gains `set_voice_available`; `_refresh_status` pushes
  `self._view.set_voice_available(_has_segments(project))` (a project with segments always has a
  referenced narrator; `_has_segments` is the simplest correct signal — or use
  `bool(referenced_speaker_ids(project))` for precision). `_on_finished → _refresh_status`
  re-enables it after a run.
- `app.py`: build `VoicePanel` + `VoicePresenter(view=panel, config=deps.resolved_config(),
  on_reviewed=presenter.refresh_after_review)`; wire panel intents (including the two audition
  buttons → `audition_speaker` / `audition_path`); navigation
  `assign_voices_requested = lambda: (voice_presenter.open(), window.show_voice_page())`,
  panel `back_requested = window.show_shell_page`. **Combine the load hook** so both panels
  re-attach and the voice player resets on switch:
  `presenter.on_project_loaded = lambda store: (attribution_presenter.attach(store),
  voice_presenter.attach(store))` (voice `attach` calls `view.stop_playback()`).

Navigation flow: pipeline halts NEEDS_REVIEW → shell shows "…, voices needed for: Bob, Carol"
→ user clicks **Assign voices** → page 2 renders the cast (needs-voice rows prominent) → user
auditions candidates, assigns / bulk-assigns → each edit → `ReviewService` save → `_after_edit`
→ `on_reviewed` → `refresh_after_review` re-reads `project.json` and the shell's blocker line
drops **live** → **Back** → **Run** again (unchanged; this slice never auto-runs).

---

## Thread ownership & run-lockout
All voice editing runs on the **Qt main thread** — no worker. Each action is an in-memory
dataclass mutation + one atomic `WorkspaceStore.save` (bulk-assign is many in-memory
`assign_voice` calls + a **single** save via `assign_voices`), which `ReviewService`'s docstring
sanctions as too cheap for a worker. Audition playback is likewise main-thread and event-driven
(`QMediaPlayer` runs its own backend thread internally — no worker needed here). **Run-lockout:**
the "Assign voices" button is disabled while a pipeline run is in flight (same gate slice 2 put
on "Review attributions"), so a voice edit's `ReviewService` save can't race the worker's
`project.json` write; `_on_finished → _refresh_status → set_voice_available` re-enables it.

---

## Ordered tasks (dependency-ordered, each independently verifiable)
1. **`audio/synthesize.py`** — publish `referenced_speaker_ids` (alias over
   `_referenced_speaker_ids` + `__all__`). *Verify: it returns the same ids the private one
   does; a unit test that a referenced narrator/character appears.*
2. **`review/actions.py` + `review/service.py`** — `set_speaker_category` (pure action) +
   `ReviewService.set_speaker_category` (apply + save, no invalidation). *Verify: service test
   — category persists on reload; gate blockers unchanged; no cache-key change.*
3. **`review/voice_view.py`** — `SpeakerVoiceRow`, `CategoryOption`, `speaker_voice_rows`,
   `category_options`, `needs_voice_count`. Pure/offline. *Verify: unit tests (below).*
4. **`app_service/voices.py`** — `resolve_category_clip`, `BulkVoicePlan`, `plan_bulk_voice`,
   `apply_bulk_voice`. Qt-free. *Verify: precedence + targeting + apply tests (below).*
5. **`cli.py`** — refactor `_cmd_assign_voice_rest`/`_resolve_category_clip` onto task 4.
   *Verify: `tests/cli/test_cli_assign_voice_rest.py` stays green unchanged.*
6. **`ui/voice_presenter.py`** — `VoiceView` Protocol (incl. `play_clip` / `stop_playback`) +
   `VoicePresenter` (assign/unassign/set_category/bulk + `audition_speaker`/`audition_path`).
   No Qt, no multimedia. *Verify: loop-free presenter tests (below), including the audition
   hook + missing-file guard, and that `attach` calls `stop_playback`.*
7. **`ui/voice_panel.py`** — the dumb Qt `VoiceView` (cast list, filter, action row, category
   combo, bulk panel with per-category pickers, ▶ audition buttons, Back, intent callbacks) **and
   the single `QMediaPlayer`/`QAudioOutput`** implementing `play_clip`/`stop_playback` with
   lazy construction, `errorOccurred` → `show_error`, and stop/release on teardown + project
   switch.
8. **`ui/main_window.py`** — page 2, "Assign voices" button + callback, `show_voice_page`,
   `set_voice_available`, run-lockout in `set_running`.
9. **`ui/presenter.py`** — push `set_voice_available`; extend the `ProjectView` Protocol.
10. **`ui/app.py`** — construct + wire the voice panel/presenter, nav, the audition-button
    callbacks, and the combined `on_project_loaded` (both attach + player reset on switch).
11. **Tests** — the derivation, shared-resolution, presenter (incl. audition), and
    offscreen-smoke suites below.

---

## Interfaces & boundaries
- **Mutation boundary (unchanged seam):** all edits go through `ReviewService`
  (`register_voice_clip`, `assign_voice`, `assign_voices`, `unassign_voice`, new
  `set_speaker_category`). The UI adds **no** voice logic and never calls `review/actions.py`
  directly except via `apply_bulk_voice` (which lives in Qt-free `app_service`).
- **Predicate boundary (the anti-drift rule):** `unresolved_speakers` / `unresolved_voices`
  (needs-voice + bulk target set) and `referenced_speaker_ids` (is-referenced) are the only
  sources — never re-implemented in `ui/` or `review/voice_view.py`.
- **Multimedia boundary:** `PySide6.QtMultimedia` is imported **only** in `ui/voice_panel.py`;
  the presenter/view-model touch it exclusively through the `play_clip`/`stop_playback` hooks
  on `VoiceView`.
- **Gate boundary (unchanged):** `unassign_voice` pops `stage_status[REVIEW]`; `review_blockers`
  / `describe_blockers` drive the shell summary via `refresh_after_review`.
- **Persisted schema:** untouched — no `schema_version` bump (only existing
  `Speaker.voice_clip_id`/`category` + `project.voice_clips` fields change).
- **File-path contract:** the picker yields an existing absolute `.wav`;
  `register_voice_clip` re-checks existence (raises `ValueError` → `show_error`) and stores
  `Path.resolve()` (a prior bug fix). Assigning or auditioning a missing/renamed file errors
  gracefully.
- **No provider/LLM/TTS boundary crossed.**

---

## Risks & open questions
- **Bulk-assign target set MUST equal the gate predicate.** If `plan_bulk_voice` targeted
  anything other than `unresolved_speakers(project)`, the UI could "finish" while the gate still
  blocks (or voice an unreferenced speaker pointlessly). Mitigation: `plan_bulk_voice` calls
  `unresolved_speakers` directly, and a presenter test asserts the applied set equals
  `{s.id for s in unresolved_speakers(project)}` of the chosen category.
- **Missing-file handling (assign + audition).** `register_voice_clip` raises `ValueError` on a
  non-existent path; the presenter catches it and `show_error`. Audition guards the same way
  (presenter check + panel `errorOccurred`), so a clip deleted after assignment neither crashes
  playback nor silently "plays" nothing. Also surface the `clip_file_missing` row state (already
  reflected by `unresolved_speakers`). Test all three.
- **Qt multimedia lifecycle/teardown.** A leaked `QMediaPlayer` can keep a file handle open or
  keep audio running after the panel/project changes. Mitigation (built here): one panel-owned
  player, `stop_playback()` on re-`attach`/project switch (via the combined
  `on_project_loaded`), stop+release on `closeEvent`/`deleteLater`, and `errorOccurred` wired to
  a friendly error. **Headless-CI caveat:** an offscreen box may have no audio backend — the
  panel must construct the player defensively and tests must never require real playback (see
  Verification).
- **`register_voice_clip` never de-dupes by path.** Each assign appends a new `VoiceClip`, and
  bulk-assign registers one per **distinct** path (via `apply_bulk_voice`); over many manual
  edits `project.voice_clips` can accumulate. Acceptable for this slice; note a later
  "clip library / re-use existing clip" enhancement (a picker of already-registered clips) as a
  follow-up — do not build it now.
- **CLI refactor regression.** Moving `_resolve_category_clip` out of `cli.py` must preserve the
  flag > env-default > shared-default precedence and the exact `uncovered` error text.
  `tests/cli/test_cli_assign_voice_rest.py` is the guard; keep it unchanged and green.
- **Two review presenters, two snapshots.** Attribution and voice presenters each `load()` their
  own `Project`. Correctness relies on funneling every mutation through a `ReviewService` +
  reloading after save, and re-`attach`-ing both on every project (re)load (the combined
  `on_project_loaded`). Covered by a persistence test.
- **Category has no gate/cache effect.** Confirmed: the `AudioCache` key uses resolved
  `voice_clip_id` (not category), and the gate keys on voiced-state; so `set_speaker_category`
  needs neither review-flag invalidation nor a re-render. Documented on the new service method.

---

## Verification strategy
Static: `ruff` + `black --check` + `mypy --strict` on all new/changed modules.
`review/voice_view.py`, `app_service/voices.py`, and `ui/voice_presenter.py` import **no
PySide6** (and no `QtMultimedia`) — `tests/test_qt_isolation.py` stays green; Qt **and all
multimedia** stay confined to `ui/voice_panel.py` + the `main_window.py` edits.

Tests (all offline; **no** model/provider/ffmpeg call, **and no audio device**):

- **Pure derivation** (`tests/review/test_voice_view.py`): over a project with a mix of
  referenced-unvoiced, referenced-voiced, and unreferenced speakers (plus a
  voice_clip_id-set-but-file-missing case), assert each `SpeakerVoiceRow`'s `needs_voice`
  **equals** membership in `unresolved_speakers(project)`, `is_referenced` equals
  `referenced_speaker_ids(project)`, the clip label/path resolution (`voice_clip_path` feeds
  audition), and `clip_file_missing`. Assert `needs_voice_count(rows) ==
  len(unresolved_speakers(project))` (the drift check). `category_options()` lists every
  `VoiceCategory`.

- **Shared resolution** (`tests/app_service/test_voices.py`): `resolve_category_clip`
  precedence (per-category override > env default from `AppConfig` > shared `--default` >
  `CASTTROPHIZER_VOICE_DEFAULT`); `plan_bulk_voice` targets exactly `unresolved_speakers`,
  buckets uncovered categories with their speaker names, and mutates nothing; `apply_bulk_voice`
  registers one clip per distinct path (dedupe), assigns via a real `ReviewService` over an
  in-memory project, returns per-category counts, and raises `ValueError` on a non-existent
  path. Re-assert the CLI `--rest` behaviour is unchanged by keeping
  `tests/cli/test_cli_assign_voice_rest.py` green.

- **Loop-free `VoicePresenter`** (`tests/ui/test_voice_presenter.py`): a `FakeVoiceView`
  (records `show_speakers`/`show_needs_voice`/`show_category_options`/`select_speaker`/
  **`play_clip`**/**`stop_playback`**/`show_error`) + a **real** `ReviewService` over an
  in-memory `Project` (from `tmp_workspace`) + a recording `on_reviewed`. Assert:
  - `open()` renders rows + a correct needs-voice count/referenced-total.
  - `assign(id, wav)` (real temp `.wav`) → speaker `voice_clip_id` set, that speaker leaves
    `unresolved_speakers`, needs-voice count decremented, `on_reviewed` fired, rows re-pushed.
  - `assign(id, "does-not-exist.wav")` → `view.show_error`, no mutation, no `on_reviewed`.
  - `unassign(id)` → `voice_clip_id is None`, needs-voice count incremented,
    `stage_status[REVIEW]` popped (gate re-opened).
  - `set_category(id, WOMAN)` → `speaker.category == WOMAN`, `on_reviewed` fired.
  - `bulk_assign_by_category(overrides)` with a full-coverage overrides map → assigns to
    **exactly** `{s.id for s in unresolved_speakers(project) if s.category == …}`, one clip per
    distinct path; with an uncovered category → `show_error`, nothing assigned.
  - **Audition (no audio device):** `audition_speaker(voiced_id)` calls `view.play_clip(path)`
    with that speaker's `voice_clip_path` and does **not** fire `on_reviewed` (read-only);
    `audition_speaker(unvoiced_id)` and `audition_speaker` on a missing-file speaker →
    `view.show_error`, `play_clip` NOT called; `audition_path(temp_wav)` → `play_clip(temp_wav)`;
    `audition_path(missing)` → `show_error`. `attach` calls `view.stop_playback()`.
  - Persistence: after each edit a fresh `tmp_workspace.load()` reflects it (ReviewService save
    path exercised).
  - `on_reviewed` fires on every **mutating** action and **not** on the audition intents.

- **Offscreen Qt smoke** (`tests/ui/test_voice_panel_smoke.py`, `QT_QPA_PLATFORM=offscreen`,
  reuse the session `qapp` fixture): `VoicePanel` satisfies `VoiceView` structurally; wiring a
  real `VoicePresenter` renders rows into the list; the filter toggle and an Unassign/Assign
  (with a monkeypatched `QFileDialog.getOpenFileName` returning a temp `.wav`) drive the
  presenter and update the widget/progress label; `MainWindow` `QStackedWidget` navigation
  (shell ↔ voice page) switches pages and the run-lockout disables the button.
  **Audition under offscreen (no real playback):** either (a) monkeypatch/mock `QMediaPlayer`
  so `play_clip` asserts `setSource`/`play` were called with the expected `QUrl` **without**
  invoking a backend, **or** (b) simply construct the real player under offscreen and call
  `play_clip` once, asserting it does not raise — but do **not** assert audible output or a
  playing state, and do **not** require an audio backend. Prefer (a) for determinism. Also
  assert `stop_playback()` on project switch stops the player without error.

- **Isolation guard:** `tests/test_qt_isolation.py` stays green (no PySide6/QtMultimedia import
  outside `ui/`); the existing `main_window`/attribution smoke tests still pass after the page-2
  stack addition.

---

## Scope discipline
Minimal-but-usable: list the cast with voice state (needs-voice prominent), assign/unassign a
clip, change a speaker's category, **audition candidate/assigned reference clips (▶)**,
bulk-assign by category (drift-free target set), live blocker refresh. Do **not** pull forward
text-suggestion review or per-line **rendered**-audio review/regenerate — each is a separate
slice that reuses the same `ReviewService` + gate seams (and the audio-review slice reuses this
slice's `QMediaPlayer` pattern for rendered segments).

---

## Handoff
Coder: build **task 1 first — publish `referenced_speaker_ids` in `audio/synthesize.py`**, then
**task 3 (`review/voice_view.py`) with its unit test**, since both the presenter and panel
depend on those Qt-free shapes and on the drift-free predicate. Do **not** touch any Qt/
multimedia before tasks 3–4 (the derivation + shared resolution) and their tests are green.
Build the presenter (task 6) with its audition-hook tests **before** wiring `QMediaPlayer` in
the panel (task 7), so the "what to play / missing-file" logic is proven without an audio
device. Both DECISION forks are resolved (audition IN, per-speaker category) — no user gate
remains.

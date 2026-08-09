# UI slice 4 — text-suggestion review (+ shared ReviewService refactor + text→audio propagation)

Pipeline stage touched: **review** (gate criterion 2 — pending `TextSuggestion`s), with a
**core** extension so accepted suggestions and free-text line edits reach the rendered audio
(and, for whole-line edits, re-open attribution). Source ebooks and voice clips remain
read-only; all writes go through `ReviewService` → `project.json`.

Task order: **Part A** (shared `ReviewService`) → **Part A2** (core text→segment propagation) →
**Part B** (suggestion UI panel + shell wiring).

---

## DECISIONS for the user (resolve before/while coding)

1. **Shared-service ownership → `ProjectPresenter` (recommended).** It already owns `_store`,
   the load/run lifecycle (`_adopt`, `_on_finished`, `_notify_project_loaded`), and is the only
   place that knows when disk changed. Alternative (rejected): a free-standing `ReviewSession`
   holder — more moving parts, no benefit at three panels.

2. **Rebuild the shared service on run *failure* too (minor behavior change).** Today `_on_failed`
   does not re-attach the panels, so after a failed run that partially mutated disk the panels
   hold a stale snapshot. Recommend rebuilding on **both** `_on_finished` and `_on_failed`.

3. **Text-review entry availability → gate on "project has suggestions", not `has_segments`.**
   Suggestions exist after `correct`, before `attribute` produces segments. New
   `_has_suggestions(project)` predicate lets the user triage text as soon as corrections land.

4. **Shell: keep three flat buttons this slice; defer a "review hub".** Order by pipeline /
   criterion: **Review text** → **Review attributions** → **Assign voices**.

5. **PROPAGATE TO AUDIO NOW (user decision).** Accepting a suggestion updates the matching
   **segment** text (not just `line.text`), so it re-renders; a free-text whole-line edit
   **re-segments** the line, which re-opens attribution for its quotes and re-renders. This is
   core behavior (CLI + all UI), designed in **Part A2** and living in `review/actions.py` +
   `ReviewService`. See the honest remaining limitations at the end (span-preserving remap and
   auto re-attribution stay out of scope).

---

## Goal

Let the user browse every line carrying a **pending** `TextSuggestion`, accept (apply the
replacement to line **and** the matching segment) or reject (keep original) each, and
free-text-edit a line (re-segmenting it) — with the shell's criterion-2 (and now criterion-1)
blocker counts updating live. As task 1, collapse the per-panel `ReviewService` snapshots into
one shared service; as task 2, make text edits reach the rendered audio.

---

## Part A — shared `ReviewService` refactor (DO FIRST)

*(unchanged from the prior revision — reproduced so the coder has one document)*

### Problem being removed
Each panel presenter builds its own `ReviewService(store, store.load())` in `attach`, holding an
independent full-project snapshot; `store.save` writes the **whole** project, so panel B's save
clobbers panel A's edit. Slice 3 patched this by re-loading the entering panel from disk on
navigation (`ui/app.py::_open_review_page`/`_open_voice_page` + `ProjectPresenter.store` +
`tests/ui/test_cross_panel_persistence.py`). That "reload on entry" dance does not scale to a
third editing panel.

### Target design
One `ReviewService` over one in-memory `Project`, created by `ProjectPresenter` on project
load / run finish and shared by reference to all three panel presenters. One save path, one
`Project`, no snapshots to diverge → clobber impossible by construction; the re-attach dance and
the re-attach role of `ProjectPresenter.store` are deleted.

### Lifecycle (load-bearing)
- Rebuild the shared service from disk at both authoritative points: `_adopt(store)` (fresh
  open/create) and `_on_finished(result)` (the worker just mutated `project.json`), then hand it
  to the panels via the existing `on_project_loaded` hook. The pre-run in-memory `Project` is
  stale; rebuilding from disk is mandatory. Per DECISION 2, rebuild on `_on_failed` too.
- Run-lockout stays the guarantee that nothing mutates the shared `Project` mid-run:
  `MainWindow.set_running(True)` disables the review/voice (and now text) entry buttons;
  `_refresh_status` re-enables them only after a terminal callback. Confirm the new text button
  is disabled in `set_running` exactly like the other two.

### File changes (Part A)
- **`ui/presenter.py`** (`ProjectPresenter`, `ProjectView`): add `self._service`; add
  `_rebuild_service()` (`self._service = ReviewService(self._store, self._store.load())`) called
  in `_adopt`, `_on_finished`, `_on_failed` before `_notify_project_loaded`; hook type
  `Callable[[ReviewService], None]`; `_notify_project_loaded` passes `self._service`; expose a
  `service` property (drop the `store` re-attach rationale); `refresh_after_review` reads blockers
  from `self._service.project`; add `set_text_available` to `ProjectView` and push
  `_has_suggestions(project)` from `_refresh_status`.
- **`ui/attribution_presenter.py`** / **`ui/voice_presenter.py`**: `attach(self, service:
  ReviewService)` stores the service by reference (drop `store.load()` + `WorkspaceStore` import);
  voice keeps its `stop_playback()` call.
- **`ui/app.py`**: `_on_project_loaded(service)` attaches **all three** panels to the same
  service; delete the re-attach in `_open_review_page`/`_open_voice_page` (now just
  `open(); show_*_page()`); add `_open_text_page`.

### Test changes (Part A)
- **`tests/ui/test_cross_panel_persistence.py` → REPLACE** with
  `tests/ui/test_shared_review_service.py`:
  - `test_single_shared_service_makes_cross_panel_clobber_impossible` (one service, all panels
    attached, both edits coexist on disk, no re-attach);
  - `test_all_panels_share_the_same_service_object` (`attribution._service is voice._service is
    suggestion._service`);
  - delete `test_stale_snapshot_without_reattach_clobbers_the_other_edit`.
- **`tests/ui/test_attribution_presenter.py`**: `_presenter` helper (~91) and the standalone
  `tmp_path` builders (~304, ~319, ~440) build `ReviewService(store, store.load())` and
  `attach(service)`; add the `ReviewService` import.
- **`tests/ui/test_voice_presenter.py`**: `_presenter`/attach helper (~186) builds and attaches a
  service; `test_attach_calls_stop_playback` still valid.
- **`tests/ui/test_presenter.py`**: `FakeProjectView` gains `set_text_available`; assert
  `presenter.service` is refreshed after `_adopt`/`_on_finished`/`_on_failed` and the hook fired
  with a `ReviewService`.

Part A is **behavior-preserving for the user** (attribution + voice review identical; run-lockout,
live blocker refresh, and shell-summary ticking unchanged).

---

## Part A2 — text→segment propagation (CORE, before the UI)

Lives in **`review/actions.py`** + **`ReviewService`** (CLI + all UI benefit), NOT the UI.

### Key mechanic (verified against the code — lean on it)
- `AudioCache.key_for(segment, project)` = `compute_key(segment.text, voice_clip_id, tts_params)`
  (`workspace/audio_cache.py:64-75`) — it hashes **`segment.text`**.
- The synth skip-gate (`audio/synthesize.py:263-270`) skips a segment **only** when
  `segment.audio_cache_key == key AND audio_status in RENDERED_STATUSES AND cache.has(key)`.
- **Therefore mutating `segment.text` changes the recomputed key → the stored key no longer
  matches → the segment re-renders on the next synth pass.** Updating `segment.text` alone is
  sufficient to force a re-render. We will **also** defensively reset the touched segment's
  `audio_cache_key=None` and `audio_status=ReviewStatus.PENDING` — clearer intent, and symmetric
  with the FAILED branch (`synthesize.py:280-281,297-298`) which nulls stale keys so the assemble
  stage never stitches an out-of-date WAV.

### Shared segment-builder extraction (reuse, no LLM in the review layer)
Segment construction currently lives inline in `attribution/attribute.py::_attribute_window`
(lines ~78-102: narration span → APPROVED narrator segment; quote span → NEEDS_REVIEW,
`speaker_id=None`, `confidence=0.0`). Extract it into a new **pure, offline** module:

- **`src/casttrophizer/attribution/segmentation.py`**
  - `build_line_segments(line_text: str, narrator: Speaker, segmenter: Segmenter) -> list[Segment]`
    — for each `segmenter.split(line_text)` span: quote → `Segment(id=new_id("seg"),
    text=span.text, speaker_id=None, role=NARRATOR, confidence=0.0,
    review_status=NEEDS_REVIEW)`; narration → `Segment(..., speaker_id=narrator.id,
    role=NARRATOR, confidence=1.0, review_status=APPROVED)`. Fresh ids; `audio_cache_key=None` /
    `audio_status=PENDING` by construction (a fresh `Segment`) → new segments re-render naturally.
  - Refactor `_attribute_window` step 1 to call it, then re-derive its `quote_segments` /
    `quote_owner` maps from the built `NEEDS_REVIEW` segments (behavior-preserving; the LLM path is
    untouched). No LLM import enters `segmentation.py`.

### 1. `accept_suggestion` propagation (the common, tractable case)
`review/actions.py::accept_suggestion(line, suggestion_id)` — after
`line.text = line.text.replace(original, suggested, 1)` and `status = APPROVED`, **also**:
- Walk `line.segments` in order; take the **first** segment whose `.text` contains `original`
  (the "first-occurrence" rule that mirrors the `line.text` replacement). Apply
  `seg.text = seg.text.replace(original, suggested, 1)` and reset
  `seg.audio_cache_key = None`, `seg.audio_status = ReviewStatus.PENDING`.
- **`original` in exactly one segment** (normal): that segment is fixed and re-renders.
- **`original` in no segment** (already-diverged text, or a token that straddles a
  narration/quote boundary, or a pre-attribution line with no segments): leave `line.segments`
  untouched — the `line.text` fix still stands; nothing to re-render (the token isn't spoken by
  any current segment). No crash, no gate change.
- **`original` in multiple segments / twice in one segment**: only the **first containing
  segment** is edited, and `replace(..., 1)` fixes only its first occurrence — deterministic,
  consistent with the line-level rule.
- **Attribution is untouched** (a spelling fix does not change who speaks) → no criterion-1
  effect. Accepting resolves a criterion-2 blocker and never adds one → **no `_invalidate_review`**.

### 2. `edit_line_text` propagation (the hard case — chosen design: full re-segment)
`review/actions.py::edit_line_text(line, new_text, project, *, segmenter: Segmenter | None = None)
-> bool` (returns True iff segments were rebuilt):
- `line.text = new_text`.
- No-op guard: if the new spans' texts equal the current segments' texts in order (e.g. a
  whitespace-only edit), leave segments untouched and return `False` — an inert edit neither
  re-opens attribution nor forces a re-render.
- Otherwise `line.segments = build_line_segments(new_text, ensure_narrator(project), segmenter or
  default_segmenter())` and return `True`. A whole-line rewrite cannot be token-mapped onto old
  segments, so we rebuild: narration → APPROVED narrator, quote → **NEEDS_REVIEW**
  (`speaker_id=None`). New segments carry `None` cache keys → they re-render.
- **This re-opens criterion 1** for the line's quotes (the legitimate cross-criterion effect):
  each new quote segment is `NEEDS_REVIEW` → it re-appears in the attribution panel. Re-attribution
  is **manual** — the review layer must not call the LLM (providers stay behind interfaces; this
  module is pure/offline), so new quotes sit at narrator-fallback (`speaker_id=None`) until the
  user re-reviews.

### 3. `ReviewService` wiring
- `accept_suggestion(line, suggestion_id)`: unchanged delegation + `_save()` (no
  `_invalidate_review` — see above).
- `edit_line_text(line, new_text)`: caller-facing signature unchanged (the presenter still passes
  `(line, new_text)`); internally call `resegmented = actions.edit_line_text(line, new_text,
  self._project)`, and **`_invalidate_review()` when `resegmented`** (re-segmentation can introduce
  a criterion-1 blocker, so a previously-COMPLETED gate must re-open), then `_save()`.
  `reject_suggestion` unchanged.

### 4. Cross-criterion + gate correctness
- After an accept: criterion 2 loses the blocker; the segment re-renders on next synth; the gate
  (`review/gate.py` — criteria are NEEDS_REVIEW segments, PENDING suggestions, unresolved voices)
  is unaffected by `audio_status`, so nothing silently commits and the count only drops.
- After a whole-line edit: criterion 1 can **grow** (new NEEDS_REVIEW quotes) and the line
  re-enters attribution review; the gate re-opens; the shell blocker summary reflects it because
  `refresh_after_review` recomputes from the shared `Project` (so "Review text" can push the
  "N attributions" count **up**). A subsequently re-attributed quote pointed at a voiceless new
  character would re-block criterion 3 — the existing voice precheck still catches it. Nothing is
  silently committed at any step.

### Test changes (Part A2) — enumerated
Existing (behavior changes, must update):
- **`tests/review/test_actions.py::test_edit_line_text_leaves_segments_untouched`** (line ~110) —
  **INVERTS**. Rewrite as `test_edit_line_text_resegments_and_reopens_attribution`: pass `project`;
  a quoted `new_text` → new NEEDS_REVIEW quote segment(s) with `speaker_id=None` (assert
  `review_blockers(project).needs_attribution` grew); a narration-only `new_text` → single
  APPROVED narrator segment, no re-open. Update the call site to `edit_line_text(line, text,
  project)`.
- **`tests/review/test_actions.py::test_accept_suggestion_applies_text_and_approves`** (~44) and
  **`test_accept_suggestion_replaces_token_not_whole_line`** (~54) — still pass (their fixtures
  hit the "token in no segment" no-op path); keep, but they no longer cover propagation.
- Any **`actions.edit_line_text(` / `ReviewService.edit_line_text` call site** across
  `tests/review/` gains the `project` arg / asserts `stage_status[REVIEW]` popped on resegment
  (grep `edit_line_text` in `tests/review/test_service*.py`).
- Attribute-stage tests must stay green after the `_attribute_window` extraction (behavior-
  preserving); if none directly assert segment shapes, add
  `tests/attribution/test_segmentation.py` covering `build_line_segments`.
New core tests:
- `tests/attribution/test_segmentation.py`: `build_line_segments` — narration → APPROVED narrator,
  quote → NEEDS_REVIEW `speaker_id=None`; ordering; empty/whitespace → `[]`.
- `tests/review/test_actions.py` (accept propagation): a line whose segment contains the token →
  accept fixes `segment.text`, resets `audio_cache_key=None`/`audio_status=PENDING`, and
  `AudioCache.key_for(seg, project)` **differs** from the pre-edit key (forces re-render);
  double-occurrence (same segment, and two segments) → only the first containing segment / first
  occurrence changes; token-in-no-segment → segments untouched, `line.text` still updated.
- `tests/review/test_service*.py`: `accept_suggestion` persists the segment propagation;
  `edit_line_text` pops `stage_status[REVIEW]` (invalidation) when it resegments, and persists.
- No LLM/TTS is invoked anywhere (segmenter + `build_line_segments` are pure/offline;
  `AudioCache.key_for` is a hash — no synthesis).

---

## Part B — text-suggestion review panel

Mirror slices 2/3: pure derivation seam → Qt-free presenter (no PySide6) → dumb Qt panel.
Mutations go through the **shared** `ReviewService`; fast/in-memory + one save → main thread,
no worker.

### New files
- **`src/casttrophizer/review/suggestion_view.py`** — pure derivation (mirrors
  `attribution_view.py`).
  - `@dataclass(frozen=True) SuggestionRow`: `suggestion_id: SuggestionId`, `line_id: LineId`,
    `chapter_index: int`, `chapter_title: str`, `line_order: int`, `line_text: str`,
    `original: str`, `suggested: str`, `reason: str`, `confidence: float`,
    `status: ReviewStatus`, `is_pending: bool`.
  - `suggestion_rows(project) -> list[SuggestionRow]` (flatten chapters→lines→suggestions in
    order; `line_text` = current `line.text` for full-line context); `pending_count(rows) -> int`.
- **`src/casttrophizer/ui/suggestion_presenter.py`** — Qt-free `SuggestionPresenter` +
  `SuggestionView` protocol (no PySide6).
  - `SuggestionView`: `show_suggestions(rows)`, `show_progress(pending, total)`,
    `select_suggestion(index)`, `show_error(title, message)`.
  - `__init__(view, *, on_reviewed)`; `attach(service: ReviewService)` (store by reference, reset
    filter=pending-only default, selection, rows); `open()`.
  - Intents (through `_apply` → `_after_edit` → `_render` + `on_reviewed`): `accept(sug_id)`,
    `reject(sug_id)`, `edit_line(line_id, new_text)` (guard blank → `show_error`);
    `set_filter(pending_only)`, `next_pending()`/`prev_pending()`, `set_selected(index)`.
  - Helpers `_find_suggestion(sug_id) -> tuple[Line, TextSuggestion] | None`,
    `_find_line(line_id)`, `_render`, `_reselect_after_edit` (land on next pending). Missing id /
    `ValueError` → `show_error`, nothing committed, no notify.
- **`src/casttrophizer/ui/suggestion_panel.py`** — dumb `SuggestionPanel(QWidget)`, structural
  `SuggestionView`.
  - Header: Back, "Pending only" `QCheckBox` (**default checked**), progress `QLabel`.
  - Flat `QListWidget` (blockSignals on repopulate). Row label: chapter · line, truncated
    `line_text`, `original → suggested`, reason, `conf 0.NN`, status tag; pending rows bold.
  - Action row: **Accept**, **Reject**, **Edit line…** (`QInputDialog` prefilled with the selected
    row's `line_text`).
  - Standing hint (now honest, propagation is live): "Accepting a suggestion fixes both the line
    and the spoken segment, which re-renders on the next run. Editing a whole line re-segments it —
    its quotes return to attribution review and it will re-render."

### Modified files (shell integration)
- **`ui/main_window.py`**: `self.suggestion_panel = SuggestionPanel()` as stack page 3;
  `show_text_page()`; "Review text" button (first in `review_row`, disabled until available);
  `review_text_requested` intent; `set_text_available`; disable the text button in
  `set_running(True)`.
- **`ui/app.py`**: build `SuggestionPresenter`; attach in `_on_project_loaded`; `_open_text_page`;
  wire `window.review_text_requested`, `suggestion_panel.back_requested = show_shell_page`, and the
  panel intents to the presenter (`on_reviewed=presenter.refresh_after_review`).

### Tests (Part B)
- `tests/review/test_suggestion_view.py`: `suggestion_rows` shape/order, `is_pending`,
  `pending_count`, reason/confidence carried through, resolved statuses not pending.
- `tests/ui/test_suggestion_presenter.py` (loop-free; fake view + **real shared** `ReviewService`
  over an in-memory `Project`, dedicated build whose `original` actually appears — and appears
  twice): accept applies `replace(original, suggested, 1)` and **propagates to the matching
  segment** (assert segment text + cache reset); reject keeps text/sets REJECTED; `edit_line`
  updates `line.text` **and** re-segments (assert new NEEDS_REVIEW quote →
  `review_blockers(...).needs_attribution` grew, and `stage_status[REVIEW]` invalidation);
  pending-only filter; pending count decrement; `on_reviewed` fired; blank edit / unknown id →
  `show_error`, no commit.
- `tests/ui/test_suggestion_panel_smoke.py`: offscreen (`QT_QPA_PLATFORM=offscreen`, the manual
  `qapp` fixture) — construct, push rows, click Accept/Reject, trigger Edit line, assert intents
  fire with the right ids; structural `SuggestionView` conformance.

---

## Threading / run-lockout note

Everything in Parts A2 + B runs on the **Qt main thread**: each accept/reject/edit is in-memory
dataclass mutation(s) plus one small atomic `project.json` write — `ReviewService`'s docstring
sanctions this as too cheap for a worker (same as slices 2/3). Re-segmentation is a pure,
deterministic string split (no TTS/LLM), so it stays on the main thread too. Actual re-rendering
happens later, on the next pipeline **run** (off-thread via the existing worker) when synth sees
the changed segment text. The "Review text" entry is disabled while a run is in flight, so no
edit's save races the worker's write and the shared `Project` is never mutated mid-run.

Architecture invariants: Qt confined to `ui/`; `ui/` never imports `cli`; `suggestion_view`,
`SuggestionPresenter`, and `attribution/segmentation.py` import **no** PySide6; `review/` and the
service never import Qt; `attribution/segmentation.py` imports no LLM SDK.

---

## Risks & open questions

- **Refactor run-finish reload lifecycle (Part A, highest risk).** A missed `_rebuild_service` on
  any terminal path resurrects stale state; the shared-service test + the ProjectPresenter
  refresh assertion guard it.
- **Segment-mapping ambiguity (Part A2 accept).** A token spanning a narration/quote boundary or
  appearing in multiple segments is handled by the deterministic "first containing segment,
  `replace(..., 1)`" rule and the safe no-op when absent — but a straddling token will not be
  fixed in audio (only in `line.text`); surfaced, not silently wrong.
- **`edit_line_text` re-segmentation loses prior attribution (Part A2 edit).** A whole-line edit
  reverts **all** that line's quotes to NEEDS_REVIEW, discarding any careful per-quote
  attribution on that line. This is the accepted cost of the full-re-segment design; a
  span-preserving remap (keep attribution for unchanged spans) is a future refinement.
- **Re-render cost.** Accept re-renders the affected segment; a whole-line edit re-renders all of
  the line's segments on the next run. TTS is slow — correct but not free.
- **`_attribute_window` extraction must stay behavior-preserving** (attribute-stage tests green).
- **Segmenter v1 limits** (straight/curly *double* quotes only; cross-line/multi-paragraph quotes
  degrade to whole-line narration) carry into review-time edits.

### Honest remaining limitations (out of scope after this slice)
- **No span-preserving remap** — editing a mixed line re-opens its quotes for attribution.
- **No automatic re-attribution** — new quote segments are narrator-fallback/NEEDS_REVIEW until
  the user re-reviews (the review layer never calls the LLM).
- **Rendered-audio review** (per-segment playback / approve / regenerate) — the next slice.
- **Review hub / tabbed navigation** — revisit when the audio-review entry makes a 4th+ item.

---

## Verification strategy

Static: `ruff` + `black` clean; `mypy --strict` clean (new frozen dataclass, protocol, typed
hooks, new `segmentation.py`, changed `edit_line_text` signature).

Tests (all offline — **no** provider/model/pipeline/synthesis calls):
- **Part A**: `test_shared_review_service.py` (clobber-impossible + same-object); mechanical
  `attach(service)` updates across the three UI test files; `presenter.service` refreshed after
  each terminal path; `set_text_available` pushed.
- **Part A2**: `test_segmentation.py`; accept-propagation + double-occurrence + token-absent in
  `test_actions.py`; inverted `edit_line_text` re-segment/re-open test; service invalidation +
  persistence; the `AudioCache.key_for` key-changed assertion proving re-render is forced.
- **Part B**: `test_suggestion_view.py`; loop-free `test_suggestion_presenter.py` (accept
  propagation, reject, edit re-segments + re-opens attribution, filter, count, notify, error
  paths); offscreen `test_suggestion_panel_smoke.py`.

---

## Handoff

Coder starts with **Part A, task 1** (single shared `ReviewService` in `ProjectPresenter`;
`attach(service)`; rewire `app.py`; replace the cross-panel test), then **Part A2** (extract
`attribution/segmentation.py::build_line_segments`, propagate `accept_suggestion` to the matching
segment with cache-key reset, re-segment in `edit_line_text` with `_invalidate_review` on
resegment, and update the enumerated `tests/review/` tests), and only then **Part B** (the
suggestion seam/presenter/panel + shell wiring).

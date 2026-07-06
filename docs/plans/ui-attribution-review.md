# Plan: PySide6 review UI — Slice 2 "Attribution review"

## Goal
Let the user browse the book's attributed segments in the GUI, jump straight to the ones
flagged `NEEDS_REVIEW` (including the narrator-fallback `speaker_id=None` quotes), and
**approve / reassign-to-existing / reassign-to-new / reject** each one through the existing
`ReviewService` — with the needs-review count and slice-1 blocker summary updating live —
so review-gate **criterion 1 (attribution)** can be resolved item by item.

---

## DECISIONS to surface to the user (genuine UX forks)

1. **Segment-browser layout — RECOMMEND a flat, filterable segment list (with chapter
   header rows), not a chapter reading-view.** The whole point of this slice is triaging
   ~120 flagged segments quickly; a flat list with a "needs-review only" filter and
   next/prev-flagged keyboard nav is the fastest surface and the least code. A chapter
   reading-view with inline rows reads nicely but adds layout/scroll-sync work and buries
   the flagged items in prose. I recommend the flat list now and note reading-view as a
   later enhancement. **Confirm or override.**

2. **Navigation surface — RECOMMEND an embedded `QStackedWidget` panel inside `MainWindow`
   (page 0 = the slice-1 shell, page 1 = the attribution panel), not a modal dialog.** A
   non-modal embedded panel lets the shell's live blocker summary and the review panel
   coexist and keeps one window; a modal `QDialog` is simpler to wire but hides the shell
   and makes "live blocker-summary refresh" a close-time event rather than a live one.
   **Confirm or override** (modal dialog is the fallback if you prefer isolation).

Everything below assumes the recommended options.

---

## Where this sits on the pipeline
No new pipeline stage. This is pure **review-stage editing UI** over already-attributed
state: it reads `project.book…segments` and mutates them through `ReviewService`, which
persists via `WorkspaceStore` and invalidates the review-gate flag. Resumability is
unchanged — every edit is a synchronous in-memory mutation plus one atomic `project.json`
write, exactly as `ReviewService` already does for the CLI. This slice adds **no** provider
or model calls.

Out of scope (later slices, list-only — do NOT build): text-suggestion review (criterion 2),
voice-clip assignment UI (criterion 3), per-line audio playback/approve/regenerate, and
synth/assemble (already driven by slice 1).

---

## Presenter-vs-view split (RECOMMEND a dedicated `AttributionPresenter`)

Add a **separate** `AttributionPresenter`, do **not** extend `ProjectPresenter`. Rationale:
`ProjectPresenter` is focused on open/create/run/stop pipeline orchestration against
`ProjectView` + `RunExecutor`; attribution review is an unrelated concern with its own view
protocol, its own view-model, and no threading. Two focused presenters keep each testable in
isolation (mirrors the "one pure seam per concern" pattern already in `review/gate.py` vs
`review/actions.py`). The only coupling is a one-way "I edited something, refresh the shell"
callback.

Both presenters stay **Qt-free** and are driven through Protocols; the Qt widgets are dumb
views.

### Pure derivation seam (new, Qt-free, no I/O) — `review/attribution_view.py`
Mirroring `review/gate.py`, put the view-model **derivation** in the review package so it is
unit-testable without any presenter or Qt, and reusable:

- `@dataclass(frozen=True) SegmentRow`: `segment_id: str`, `chapter_index: int`,
  `chapter_title: str`, `line_order: int`, `text: str`, `speaker_display: str`,
  `role: SpeakerRole`, `confidence: float`, `review_status: ReviewStatus`,
  `needs_review: bool` (`review_status == NEEDS_REVIEW`),
  `is_narrator_fallback: bool` (`segment.speaker_id is None`).
- `@dataclass(frozen=True) SpeakerOption`: `speaker_id: str`, `display: str`,
  `role: SpeakerRole`.
- `def segment_rows(project: Project) -> list[SegmentRow]` — flattens
  chapters→lines→segments in order; resolves `speaker_display` via a small pure helper
  (`segment.speaker_id is None` → narrator's name from `find_narrator`, rendered e.g.
  `"narrator (auto)"`; else look up `project.speakers` by id → `.name`; unknown id →
  defensive `"<unknown>"`).
- `def speaker_options(project: Project) -> list[SpeakerOption]` — the narrator first, then
  characters, for the reassign combo.
- `def needs_review_count(rows) -> int` (or reuse `len(review_blockers(project).needs_attribution)`).

This is the seam the presenter tests assert against directly.

### `ui/attribution_presenter.py` (new, Qt-free)
- `AttributionView` **Protocol** (render-only surface):
  - `show_segments(rows: list[SegmentRow]) -> None`
  - `show_speaker_options(options: list[SpeakerOption]) -> None`
  - `show_progress(needs_review: int, total: int) -> None`
  - `select_segment(index: int) -> None` (for keyboard next/prev-flagged; scroll+highlight)
  - `show_error(title: str, message: str) -> None`
- `AttributionPresenter(view, *, on_reviewed: Callable[[], None])`:
  - `attach(store: WorkspaceStore) -> None` — load the project from `store`, build a
    `ReviewService(store, project)`, reset filter/selection. Called when a project loads
    (see shell integration). Does not render until `open()`.
  - `open() -> None` — (re)derive rows/options and push them + progress to the view.
  - Intents (each looks the segment up by id in `service.project`, calls the matching
    `ReviewService` method, then `_after_edit()`):
    - `approve(segment_id)` → `service.approve_attribution(seg)`
    - `reassign_existing(segment_id, speaker_id)` → `service.set_segment_speaker(seg,
      speaker_id=speaker_id, role=<NARRATOR if that speaker is the narrator else CHARACTER>,
      approve=True)`
    - `reassign_new(segment_id, name)` → `service.reassign_segment_to_new_speaker(seg, name)`
      (the Qt view collects `name` via an input dialog and passes the string — the presenter
      never prompts)
    - `reject(segment_id)` → `service.reject_attribution(seg)`
  - `set_filter(needs_review_only: bool)` → re-derive the displayed rows (filtered) + push.
  - `next_flagged()` / `prev_flagged()` → advance selection to the next/prev `needs_review`
    row in the current (filtered) list; `view.select_segment(index)`.
  - `_after_edit()` (private): re-derive rows (respecting the active filter), push
    `show_segments` + `show_progress`, then invoke `on_reviewed()` so the shell refreshes
    live. Wrap `ReviewService` calls that can raise `ValueError` (bad id / empty name) →
    `view.show_error(...)`.

Note the role mapping for reassign-to-existing: selecting the narrator option must pass the
narrator's real `speaker_id` with `role=NARRATOR` (explicit and unambiguous) rather than
`speaker_id=None`; only the pre-existing fallback segments carry `None`.

### `ui/attribution_panel.py` (new, Qt — the dumb view)
A `QWidget` implementing `AttributionView` structurally:
- a "Needs review only" `QCheckBox` (**default checked** — see perf risk) → `set_filter`
- a progress label ("`N of M attributions need review`") fed by `show_progress`
- a `QListWidget` (or table) of segment rows; each row shows chapter/line, truncated text,
  `speaker_display`, `role`, `confidence`, `review_status`; flagged rows are visually
  prominent (bold + accent color/icon; the narrator-fallback rows tagged e.g. "(auto→narrator)")
- a per-selection action row: **Approve**, a speaker `QComboBox` (populated from
  `show_speaker_options`) + **Reassign**, **New speaker…** (opens `QInputDialog` for the
  name, then calls `reassign_new`), **Reject**
- keyboard nav: bind e.g. `Down`/`Up` or `J`/`K` (and `N`/`P`) to `next_flagged`/`prev_flagged`
  and `Enter` to approve-selected; the panel translates key events into presenter intents
- a **Back to project** button → returns to shell page 0
- emits intent callbacks (like `MainWindow` does) that `ui/app.py` wires to presenter methods;
  holds no logic beyond widget wiring and the two Qt dialogs.

### `ui/main_window.py` (modify)
- Wrap the current central content as **page 0** of a new `QStackedWidget`; add the
  `AttributionPanel` as **page 1**. Add `show_shell_page()` / `show_attribution_page()`.
- Add a **"Review attributions"** `QPushButton` to the shell (near the outcome panel),
  enabled whenever the loaded project has ≥1 segment; also referenced from the NEEDS_REVIEW
  outcome text ("… resolve, then Run again" → point at this button). Its click emits a new
  intent callback `review_attributions_requested`.
- Keep `MainWindow` a dumb `ProjectView`; it gains only stacked-page plumbing and the new
  button/callback — no attribution logic.

### `ui/presenter.py` (`ProjectPresenter`, minimal change)
- Add an optional `on_project_loaded: Callable[[WorkspaceStore], None] | None = None` hook,
  invoked at the end of `_adopt(store)` (and it may be called again after a run completes,
  since a run can add segments). `ui/app.py` wires it to `attribution_presenter.attach`.
- Add `refresh_after_review()` = re-run `_refresh_status()` **and** re-show the last outcome's
  blocker summary if the shell is currently showing NEEDS_REVIEW. Simplest concrete form:
  recompute `describe_blockers(review_blockers(project))` and push it to the shell's outcome/
  blocker label. `ui/app.py` passes this as the `AttributionPresenter.on_reviewed` callback.
- No change to `RunExecutor` / threading.

### `ui/app.py` (modify)
Construct the `AttributionPanel` + `AttributionPresenter`, wire:
- `attribution_presenter = AttributionPresenter(view=panel, on_reviewed=project_presenter.refresh_after_review)`
- `project_presenter.on_project_loaded = attribution_presenter.attach`
- panel intent callbacks → `attribution_presenter` methods (approve/reassign/reject/filter/nav)
- `window.review_attributions_requested = lambda: (attribution_presenter.open(), window.show_attribution_page())`
- panel "Back" → `window.show_shell_page()`

---

## Shell integration & navigation flow (recommended option 2)
1. Pipeline halts `NEEDS_REVIEW`; slice-1 shows the read-only blocker summary ("3
   attributions, 1 text suggestion, voices needed for: Bob").
2. User clicks **Review attributions** → `ProjectPresenter` (already holds the store) has
   handed it to the `AttributionPresenter` via the `on_project_loaded` hook; the panel
   `open()`s (renders rows filtered to needs-review) and the stack switches to page 1.
3. User approves/reassigns/rejects. Each edit → `ReviewService` mutation + save → `_after_edit`
   → `on_reviewed()` → `ProjectPresenter.refresh_after_review()` re-reads `project.json` and
   updates the shell's stage rows + blocker summary **live** (even while page 1 is showing).
4. **Back to project** returns to page 0, where the refreshed blocker summary is visible. To
   pass the gate the user re-clicks **Run** (unchanged slice-1 path) — this slice never
   auto-runs or auto-approves.

Coordination note: `WorkspaceStore.load()` reads fresh from disk every call (confirmed), so
`ProjectPresenter` (its own snapshot) and `AttributionPresenter` (the `ReviewService` snapshot)
never share a mutable object. Correctness relies on: all mutations funnel through the single
`ReviewService`/store, and `refresh_after_review` reloads **after** the save. `attach` must
re-load a fresh `ReviewService` each time a project is (re)loaded so the panel never edits a
stale snapshot.

---

## Thread ownership
All attribution editing runs on the **Qt main thread** — no worker. Each action is an
in-memory dataclass mutation + one atomic `WorkspaceStore.save` (the same cost `ReviewService`
already pays on the CLI). This is confirmed acceptable per `ReviewService`'s own docstring
("cheap, synchronous mutations plus a small atomic JSON write … do not need a worker thread").
Rendering the row list is also main-thread.

Flagged: on a very large book the per-edit `save` serializes the **whole** `project.json`.
For a big cast this is a few-ms to tens-of-ms fsync per click — acceptable for slice 2. If it
proves janky, a later slice can debounce/batch saves or move save to a worker; do **not** build
that now.

---

## Ordered tasks (dependency-ordered, each independently verifiable)
1. **`review/attribution_view.py`** — `SegmentRow`, `SpeakerOption`, `segment_rows`,
   `speaker_options`, display-name helper. Pure/offline. *Verify: unit test the derivation
   incl. the `speaker_id=None` fallback row.*
2. **`ui/attribution_presenter.py`** — `AttributionView` Protocol + `AttributionPresenter`
   (approve/reassign-existing/reassign-new/reject/filter/next-prev/attach/open/`_after_edit`).
   No Qt. *Verify: loop-free presenter tests (below).*
3. **`ui/attribution_panel.py`** — the dumb Qt `AttributionView` widget (list, filter,
   action row, combo, New-speaker `QInputDialog`, keyboard nav, Back button, intent callbacks).
4. **`ui/main_window.py`** — wrap content in `QStackedWidget`; add the AttributionPanel page,
   `show_shell_page`/`show_attribution_page`, the "Review attributions" button + callback, and
   enable/disable it based on segment presence.
5. **`ui/presenter.py`** — add `on_project_loaded` hook (call in `_adopt`/post-run) and
   `refresh_after_review()`.
6. **`ui/app.py`** — construct + wire the panel/presenter/hook/callback graph.
7. **Tests** — pure derivation tests, loop-free `AttributionPresenter` tests, and a few
   offscreen Qt smoke tests (below).

---

## Interfaces & data shapes crossing boundaries
- **Mutation boundary (unchanged):** all edits go through `ReviewService`
  (`approve_attribution`, `set_segment_speaker`, `reassign_segment_to_new_speaker`,
  `reject_attribution`) — the UI adds **no** attribution logic and never touches
  `review/actions.py` directly.
- **Gate boundary (unchanged):** `review_blockers` / `describe_blockers` drive the shell's
  live summary; `set_segment_speaker`/`reassign_*` already pop `stage_status[REVIEW]` so the
  gate re-opens if an edit re-introduces a blocker.
- **Persisted schema:** untouched. No `schema_version` bump.
- **New in-process view-models:** `SegmentRow` / `SpeakerOption` (in `review/`, Qt-free).
- **Presenter↔view Protocol:** `AttributionView` (render-only); the Qt panel implements it
  structurally, exactly like `MainWindow` implements `ProjectView`.
- **No provider/LLM/TTS boundary is crossed** in this slice.

---

## Risks & open questions
- **Large-book list performance.** A book may have thousands of segments; `QListWidget`
  materializing all of them can be sluggish and memory-heavy. Mitigation in this slice:
  default the **"needs review only"** filter ON so the list is ~the flagged count (~120), and
  keep row rendering lightweight. Flagged follow-up: switch to a virtualized `QListView`/
  `QAbstractListModel` (or lazy paging) if the "show all" path is slow. Do not build the model
  layer now.
- **Reassign-to-new-speaker introduces a downstream voice blocker (criterion 3).** Creating a
  voiceless character makes `review_blockers.unassigned_voices` non-empty, so the review gate
  stays blocked even after the needs-review **attribution** count hits 0. This is correct but
  can confuse ("I approved everything, why still NEEDS_REVIEW?"). Because the shell's blocker
  summary already renders "voices needed for: X" and refreshes live via `refresh_after_review`,
  the user sees the reason — call this out in the UI copy. The voice-assignment fix is a later
  slice (do not pull it forward).
- **Two snapshots / staleness.** Two presenters each `load()` their own `Project` object.
  Correctness depends on funneling every mutation through the single `ReviewService` and
  reloading after save. `attach` must rebuild the `ReviewService` on each (re)load. Covered by
  a presenter test that edits, then asserts a freshly-loaded project reflects it.
- **Reject UX.** `reject_attribution` sets REJECTED, which is **not** a gate blocker but leaves
  the segment pointing at its old (possibly wrong) speaker. Recommend the panel treat Reject as
  "reject → then reassign" (surface a hint after reject) rather than a terminal state, so a
  rejected segment doesn't silently render with the wrong voice. Include Reject but make
  reassign the prominent path. **Open question for the user:** keep Reject in slice 2 or defer
  it? (It's cheap to include; recommend keep.)
- **Chapter grouping in a flat list.** Header rows (non-selectable) add minor complexity; if it
  fights `QListWidget` selection, fall back to a "Chapter N · line M" prefix per row.

---

## Verification strategy
Static: `ruff` + `black --check` + `mypy --strict` on all new modules. `review/attribution_view.py`
and both presenters must import **no PySide6** (keeps `tests/test_qt_isolation.py` green); Qt
stays confined to `ui/attribution_panel.py` + the `main_window.py` edits.

Tests (all offline; **no** model/provider/ffmpeg calls):

- **Pure derivation** (`tests/review/test_attribution_view.py`): `segment_rows` over
  `review_ready_project` (and a crafted project with a `speaker_id=None`, confidence 0.0,
  `role=NARRATOR`, `NEEDS_REVIEW` fallback segment) asserts each row's `text`,
  `speaker_display` (narrator name for the fallback; character name otherwise; `<unknown>`
  defensive), `confidence`, `review_status`, `needs_review`, `is_narrator_fallback`, and order.
  `speaker_options` lists narrator first then characters.

- **Loop-free `AttributionPresenter`** (`tests/ui/test_attribution_presenter.py`): a
  `FakeAttributionView` (records `show_segments`/`show_progress`/`select_segment`/`show_error`)
  + a **real** `ReviewService` over an in-memory `Project` loaded from `tmp_workspace`
  (`review_ready_project` fixture), plus a recording `on_reviewed` callback. Assert:
  - `open()` renders rows + a correct needs-review count/total.
  - `approve(id)` → segment `review_status == APPROVED`, needs-review count decremented,
    `on_reviewed` fired, `show_segments` re-pushed.
  - `reassign_existing(id, alice_id)` → segment `speaker_id == alice_id`, `role == CHARACTER`,
    `APPROVED`; selecting the **narrator** option → `role == NARRATOR` with the narrator's id.
  - `reassign_new(id, "Carol")` → a new CHARACTER speaker exists, segment attributed +
    APPROVED, and `review_blockers(project).unassigned_voices` now includes "Carol" (the
    documented downstream voice blocker).
  - `reject(id)` → `review_status == REJECTED`; it is **not** counted as a needs-review blocker.
  - **Narrator-fallback flip:** a `speaker_id=None` NEEDS_REVIEW segment, after
    `reassign_existing`/`reassign_new`, has a non-None `speaker_id` and is APPROVED (flagged →
    attributed).
  - `set_filter(True/False)` changes the pushed row set (flagged-only vs all).
  - `next_flagged`/`prev_flagged` call `select_segment` with the expected indices and wrap/stop
    sensibly.
  - Persistence: after an edit, a fresh `tmp_workspace.load()` reflects it (proves the
    ReviewService save path is exercised, not just in-memory).
  - Bad input (`reassign_existing` with an unknown speaker id, empty new-speaker name) →
    `view.show_error`, no crash.
  - `on_reviewed` is invoked on every mutating action (the live shell-refresh contract).

- **Offscreen Qt smoke** (`tests/ui/test_attribution_panel_smoke.py`, `QT_QPA_PLATFORM=offscreen`,
  reuse the session `qapp` fixture): `AttributionPanel` satisfies `AttributionView`
  structurally; wiring a real `AttributionPresenter` renders rows into the list widget; a
  filter toggle and an Approve button click drive the presenter and update the widget/progress
  label; the `QStackedWidget` navigation (shell ↔ attribution page) via `MainWindow` switches
  pages. Keep these few — behavioral coverage lives in the loop-free presenter tests.

- **Isolation guard:** existing `tests/test_qt_isolation.py` must still pass (no new PySide6
  import outside `ui/`), and the full existing suite stays green (this slice adds files; it
  edits only `main_window.py`/`presenter.py`/`app.py` — check the slice-1 smoke tests for
  `MainWindow` still hold after the `QStackedWidget` refactor).

---

## Scope discipline
Minimal-but-usable: browse + jump-to-flagged + approve/reassign/reject + live count/summary.
Do **not** pull forward text-suggestion review, voice-clip assignment, or audio playback —
those are separate slices that will reuse the same `ReviewService` seam.

---

## Handoff
Coder: build **task 1 first — `review/attribution_view.py` (the pure `SegmentRow`/`segment_rows`
derivation)** and its unit test, since both presenter and panel depend on those shapes and it
is the Qt-free foundation. Then task 2 (`AttributionPresenter`) with its loop-free tests before
touching any Qt. Wait on the two DECISION forks at the top only if the user wants to override;
otherwise proceed with the flat-list + embedded-stacked-panel recommendations.

# Plan: PySide6 review UI — Slice 1 "Shell + pipeline driver"

## Goal
Ship a usable desktop shell that opens/creates a project, shows per-stage status for
all six stages, runs the existing headless pipeline off the main thread with a live
progress bar and Stop/Resume, and renders the right terminal outcome (COMPLETED →
m4b path, NEEDS_REVIEW → read-only blocker counts, STOPPED → resumable, FAILED →
error) — with friendly handling when a provider/ffmpeg is unavailable.

## Where this sits on the pipeline
This slice adds **no pipeline stage**. It is a second front-end (alongside `castrun`)
over the existing `parse → correct → attribute → review → synthesize → assemble`
runner. Resumability is already owned by the stages + `WorkspaceStore`; the UI only
drives `Pipeline.run` and reads back `project.stage_status` / `review_blockers`. The
run MUST halt at NEEDS_REVIEW (no auto-accept in the UI — that stays a CLI-only flag),
so nothing irreversible happens.

---

## Key architectural task: extract a Qt-free, CLI-free application-service layer

Today `cli.py` owns orchestration that the UI needs verbatim. Duplicating it would
drift; importing `cli` from `ui/` is forbidden. Extract the shared pieces into a new
**`src/casttrophizer/app_service/`** package (rationale: it is an application-service
layer sitting *above* pipeline/review/workspace and *below* both presentation
front-ends; a package rather than one fat module keeps create/run/interpret concerns
legible, and the whole package is trivially import-cheap and Qt-free so the existing
`test_qt_isolation.py` guard covers it for free). A single `projects.py` module is a
viable smaller alternative, but four thin modules read better than one 250-line file.

### What moves out of `cli.py` (unchanged behavior)
- `_new_project` and `_seed_tts_params` → `app_service/projects.py`.
- `_build_pipeline` → `app_service/pipeline_service.py` (assembler still injected).
- `_build_llm` / `_build_tts` + the LLM preflight → `app_service/pipeline_service.py`,
  renamed and **re-homed onto Qt/CLI-free exceptions** (see below) instead of `CliError`.
- `_run_once` (load project → decide preflight → build providers → build `StageContext`
  → `pipeline.run`) → `app_service/pipeline_service.py` as `run_pipeline(...)`.
- The **outcome classification** inside `_report_run` (COMPLETED-vs-early-stop,
  NEEDS_REVIEW blockers, STOPPED, FAILED) → a pure function `interpret_result(...)` in
  `app_service/outcome.py` returning a structured `RunOutcome` (no printing, no exit codes).
- The per-stage status rows (the loop in `cmd_status`) → `stage_status_rows(project,
  pipeline)` in `app_service/status.py`.

### What each caller keeps
- **`cli.py`** keeps: `CliDeps` (identical public fields — `llm_factory`, `tts_factory`,
  `assembler`, `progress_factory`, `config` — so every existing CLI test drives through
  `cli.main(..., deps=CliDeps(...))` unchanged), `PrintReporter`, SIGINT wiring, the
  `--auto-accept` re-run (`_auto_accept`), all argparse, and the *presentation* half of
  `_report_run` (printing + exit-code mapping). Each `cmd_*` becomes a thin wrapper that
  adapts `CliDeps` → `AppServiceDeps`, calls the shared function, then prints/maps.
- **`ui/`** keeps: all Qt. It builds an `AppServiceDeps` from `AppConfig.from_env()` +
  default provider factories, and calls the same shared functions.

### New shared exceptions (in `errors.py`)
Add `PreconditionError(CasttrophizerError)` — raised by `build_providers(...)` when a
required provider is unavailable at preflight (e.g. missing `ANTHROPIC_API_KEY` before
attribute). `ConfigError` (already exists) still covers an unknown provider name.
- CLI maps `PreconditionError`/`ConfigError` → `CliError(code=_EXIT_PRECONDITION)` (exit 2) — unchanged UX.
- UI catches them → friendly message, does not start the worker.
The tts-extra/ffmpeg-missing cases are **not** preflighted; they still surface as a
stage `FAILED` `StageResult` (per the existing stage guards) and render as FAILED.

### Proposed shared-layer API (all Qt-free, CLI-free, provider-import-cheap)
```
# app_service/deps.py
@dataclass
class AppServiceDeps:
    llm_factory: Callable[[AppConfig], LLMProvider] = _default_llm_factory
    tts_factory: Callable[[AppConfig], TTSProvider] = _default_tts_factory
    assembler: M4BAssembler | None = None
    config: AppConfig | None = None      # None -> AppConfig.from_env()
# (CliDeps in cli.py gains a helper `.to_app_service_deps()` returning this;
#  CliDeps additionally keeps progress_factory, which is presentation-specific.)

# app_service/projects.py
class ProjectExistsError(CasttrophizerError): ...
def create_project(epub: Path, name: str | None, workspace_dir: Path,
                   config: AppConfig, *, overwrite: bool = False)
                   -> tuple[WorkspaceStore, Project]
    # validates parser_for(epub); raises ProjectExistsError unless overwrite;
    # builds project via _new_project(_seed_tts_params(config)); store.save; returns both.
def open_project(workspace_dir: Path) -> tuple[WorkspaceStore, Project]
    # raises WorkspaceError (clear message) if no project.json present.

# app_service/pipeline_service.py
def build_pipeline(deps: AppServiceDeps) -> Pipeline
def build_providers(deps: AppServiceDeps, config: AppConfig, *,
                    provider: str | None, preflight: bool)
                    -> tuple[LLMProvider, TTSProvider]
    # raises ConfigError / PreconditionError (never CliError/Qt).
def run_pipeline(store: WorkspaceStore, pipeline: Pipeline, deps: AppServiceDeps,
                 config: AppConfig, progress: ProgressReporter, *,
                 provider: str | None = None, until: StageName | None = None)
                 -> StageResult
    # the moved _run_once: preflight decision + build_providers + StageContext + run.

# app_service/outcome.py
class RunOutcomeKind(StrEnum): COMPLETED / NEEDS_REVIEW / STOPPED / FAILED
@dataclass(frozen=True)
class RunOutcome:
    kind: RunOutcomeKind
    output_path: Path | None = None          # COMPLETED
    blockers: ReviewBlockers | None = None    # NEEDS_REVIEW
    summary: str = ""                         # describe_blockers / stopped / failed text
    message: str = ""                         # FAILED: StageResult.message
def interpret_result(result: StageResult, store: WorkspaceStore, *,
                     until: StageName | None, final: StageName) -> RunOutcome

# app_service/status.py
@dataclass(frozen=True)
class StageRow: name: StageName; status: ReviewStatus | None; is_next: bool
def stage_status_rows(project: Project, pipeline: Pipeline) -> list[StageRow]
def next_stage_name(project: Project, pipeline: Pipeline) -> StageName | None
```
`run_pipeline` deliberately does **not** embed `--auto-accept` — that re-run loop stays
in `cli.cmd_run`, so the UI cannot accidentally auto-resolve the review gate.

---

## UI structure: Model-View-Presenter for testability

Split so the orchestration is unit-testable **without a Qt event loop**. The presenter
imports **no PySide6**; the view is a dumb widget; the threading is behind an injected
executor protocol.

- **`ui/presenter.py`** (no Qt) — `ProjectPresenter(view, executor, deps)`:
  - Intent methods: `open(dir)`, `create(epub, name, overwrite)`, `start_run()`,
    `stop_run()`.
  - Holds the loaded `(store, project)` and derives view-models via `app_service`
    (`stage_status_rows`, `interpret_result`, `describe_blockers`).
  - Talks to the view through a `ProjectView` **Protocol** (defined here): e.g.
    `show_stage_rows(rows)`, `show_next_stage(name)`, `set_running(bool)`,
    `set_progress(done, total, message)`, `show_outcome(RunOutcome)`,
    `show_error(title, message)`, `show_project_loaded(name, workspace)`.
  - Talks to the worker through a `RunExecutor` **Protocol**: `start(pipeline, ctx,
    reporter, on_finished, on_failed)` and `request_stop()`. Callbacks are invoked on
    the main thread (the Qt impl connects signals with the default queued/auto
    connection), so presenter code is thread-agnostic.
  - Provider preflight friendliness: `start_run()` calls `build_providers(preflight=…)`
    and catches `PreconditionError`/`ConfigError` → `view.show_error(...)`, and does not
    start the worker.
- **`ui/run_executor.py`** (Qt) — `QtRunExecutor` implementing `RunExecutor`. Owns a
  fresh `QThread` + `PipelineWorker` + `QtProgressReporter` **per run** (QThreads are not
  cleanly restartable; a new one per Run/Resume is simplest and correct). Wires
  `reporter.total_changed/advanced/message_emitted` → presenter's progress callback,
  `worker.finished/failed` → presenter callbacks, and thread teardown
  (`quit`/`wait`/`deleteLater`). Exposes `request_stop()` → `reporter.request_stop()`.
- **`ui/view_models.py`** (no Qt) — reuse `app_service.status.StageRow` and
  `app_service.outcome.RunOutcome`; add only tiny formatting helpers if needed.
- **`ui/main_window.py`** (Qt, rebuilt) — the dumb view implementing `ProjectView`
  structurally: menu/toolbar (Open, New), a project header label, a six-row stage-status
  list + "next" label, a `QProgressBar`, a status/log `QPlainTextEdit` (append `message`
  events), Run/Stop buttons, and an outcome panel (`QLabel`/read-only text). It emits
  intent callbacks into the presenter and only renders what the presenter pushes. No
  business logic, no `app_service` calls beyond what the presenter drives.
- **`ui/app.py`** (Qt) — construct `QApplication`, `AppServiceDeps` (from
  `AppConfig.from_env()` + default factories), `MainWindow`, `QtRunExecutor`, and
  `ProjectPresenter(view=window, executor=..., deps=...)`; wire the view's intent
  callbacks to presenter methods; `show()`; `exec()`.

---

## Threading ownership

- **On the worker thread:** `Pipeline.run(ctx)` and all `ctx.store` load/save. Reuse
  `PipelineWorker` **unchanged** (it already takes a fully-built `pipeline` + `ctx` and
  emits `finished(StageResult)` / `failed(str)`).
- **On the main thread:** all widget updates (via queued signals from
  `QtProgressReporter` and the worker), the presenter, and — for this slice — the
  provider build + preflight. Provider construction is cheap (no heavy SDK import;
  `torch`/`anthropic` load lazily inside provider methods) and the Claude preflight is a
  key-presence check, so doing it on the main thread before starting the thread is
  acceptable and lets a missing key surface as a friendly dialog rather than a `failed`
  crash signal. `StageContext` is built on the main thread and handed to the worker.
- **No concurrent store access:** the worker owns the store during a run; the presenter
  only re-`load()`s the project *after* a terminal `finished`/`failed` signal to compute
  stage rows / blockers. Sequential, not shared-concurrent.
- **Stop/Resume:** `stop_run()` → `QtRunExecutor.request_stop()` → `reporter.request_stop()`
  (thread-safe `Event`); the stage returns STOPPED with partial state saved. Resume is
  just `start_run()` again (the pipeline skips complete stages). Each run uses a fresh
  reporter (or `reporter.reset()`), so a prior stop flag never leaks into the next run.

Injection note: `PipelineWorker` needs `providers` + `StageContext` built for it. The UI
builds them via `app_service.build_providers` + `StageContext(store, progress=QtProgressReporter,
llm, tts, config)`. No change to `PipelineWorker` is required for this slice.

---

## Ordered tasks (dependency-ordered, each independently verifiable)

1. **errors:** add `PreconditionError(CasttrophizerError)` to `errors.py` `__all__`.
2. **app_service/deps.py:** `AppServiceDeps` + the two default factory functions (moved
   from `cli.py`).
3. **app_service/projects.py:** move `_new_project`/`_seed_tts_params`; add
   `create_project` (parser validation + overwrite guard → `ProjectExistsError`) and
   `open_project`.
4. **app_service/pipeline_service.py:** move `build_pipeline`, `build_providers`
   (raising `PreconditionError`/`ConfigError`), and `run_pipeline` (the `_run_once` body).
5. **app_service/status.py:** `StageRow`, `stage_status_rows`, `next_stage_name`.
6. **app_service/outcome.py:** `RunOutcomeKind`, `RunOutcome`, `interpret_result` (the
   classification half of `_report_run`).
7. **app_service/__init__.py:** re-export the public API.
8. **Refactor `cli.py`:** delete the moved bodies; make `cmd_new`/`cmd_run`/`cmd_status`
   thin wrappers over `app_service` (adapt `CliDeps`→`AppServiceDeps`; keep `--auto-accept`,
   SIGINT, printing, exit-code mapping). Keep `CliDeps` fields identical.
   *Gate: full existing CLI suite green with zero test edits.*
9. **ui/presenter.py:** `ProjectView` + `RunExecutor` protocols and `ProjectPresenter`
   (no Qt).
10. **ui/run_executor.py:** `QtRunExecutor` (Qt; owns QThread/PipelineWorker/QtProgressReporter).
11. **ui/main_window.py:** rebuild as the dumb `ProjectView` (menus, stage list, progress
    bar, log, Run/Stop, outcome panel).
12. **ui/app.py:** wire deps + presenter + executor + window.
13. **Tests:** presenter unit tests (fake view + fake executor + fake pipeline/providers)
    and offscreen Qt smoke tests (see below).

---

## Interfaces & data shapes crossing boundaries
- **StageContext** (unchanged): `store`, `progress: ProgressReporter`, `llm`, `tts`,
  `config`. The UI supplies `progress = QtProgressReporter`.
- **Provider boundary** (unchanged, kept swappable): `LLMProvider.is_available()` drives
  the preflight; `TTSProvider`/ffmpeg unavailability stays a stage-level FAILED. The UI
  never imports a concrete provider — only `build_providers` via `AppServiceDeps`.
- **Persisted schema:** untouched. `stage_status: dict[str, ReviewStatus]` is read for the
  status rows; `review_blockers(project)` is read for the NEEDS_REVIEW summary.

---

## Risks & open questions
- **`is_available()` cost off the main thread.** Claude's preflight is a cheap key check,
  but `LMStudioProvider.is_available()` may probe the network and briefly block the main
  thread. Acceptable for this Claude-primary slice; **flagged**. Follow-up if it proves
  slow: move build+preflight into the worker and extend `PipelineWorker` to emit a
  distinct `precondition_failed(str)` signal (keep it out of scope now).
- **QThread lifecycle:** creating a fresh thread per run is the safe default; verify clean
  teardown (`quit`/`wait`) so Resume doesn't leak threads. Covered by a smoke test.
- **Qt test harness dependency:** decide manual `QApplication` fixture vs `pytest-qt`
  (below). Recommendation given; needs the tester's confirmation.
- **`interpret_result` and `--until`:** the UI runs with `until=None` (full pipeline), so
  the "early-stop looks like COMPLETED" branch is not exercised by the UI — but keep the
  guard in the shared function for CLI parity.

---

## Verification strategy
Static: `ruff` + `black --check` + `mypy --strict` (all new modules typed; `app_service`
must stay Qt-free so `test_qt_isolation.py` passes unchanged — add nothing that imports
PySide6 outside `ui/`).

Tests (all offline; never touch a real model/ffmpeg):
- **Regression gate:** the entire existing CLI suite passes **unedited** after the
  refactor — this proves the `cli`→`app_service` extraction is behavior-preserving.
- **app_service unit tests** (new `tests/app_service/`): `create_project` (validation +
  `ProjectExistsError`), `open_project` (missing → `WorkspaceError`), `build_providers`
  preflight raising `PreconditionError` with an unavailable fake LLM, `interpret_result`
  mapping each `StageResult` status → the right `RunOutcome`, and `stage_status_rows`
  marking the correct `is_next`.
- **Presenter tests** (`tests/ui/test_presenter.py`, **no event loop**): drive
  `ProjectPresenter` with a `FakeProjectView` (records calls) + a `FakeRunExecutor` that
  synchronously invokes the supplied `on_finished`/`on_failed` with a chosen
  `StageResult` and pumps a few progress events. Assert the view is told to:
  - render six stage rows + next stage after open/create,
  - set running true on start and false on terminal,
  - forward progress (total/advance/message),
  - show the right `RunOutcome` for COMPLETED (m4b path) / NEEDS_REVIEW (blocker counts,
    read-only) / STOPPED (resumable) / FAILED (message),
  - `show_error` (and not start the worker) when `build_providers` raises
    `PreconditionError`.
  Providers and the pipeline are faked (reuse `tests/fakes/FakeLLMProvider`,
  `FakeTTSProvider`, `FakeM4BAssembler`, `RecordingProgressReporter`); the executor fake
  means no real `Pipeline` or thread is needed for presenter logic.
- **Qt offscreen smoke tests** (`tests/ui/test_main_window_smoke.py`, few and small):
  run under `QT_QPA_PLATFORM=offscreen`. **Recommend a manual session-scoped
  `QApplication` fixture** in `tests/ui/conft.py` (sets `QT_QPA_PLATFORM=offscreen`,
  yields one `QApplication`) rather than adding `pytest-qt` — PySide6 is already a core
  dep, so this keeps the `dev` extra untouched. (`pytest-qt`/`qtbot` is the ergonomic
  alternative if the tester prefers signal-spies; it would need adding to the `dev`
  extra.) Smoke assertions: `MainWindow` constructs and satisfies `ProjectView`
  structurally; wiring a real `ProjectPresenter` + `FakeRunExecutor` to it updates the
  stage-list and progress-bar widgets on signals; Run→Stop path leaves no live QThread.
  Keep these to a handful — the behavioral coverage lives in the loop-free presenter tests.

---

## Explicitly out of scope (later slices — do not build now)
Text-suggestion editing, attribution review/reassignment, voice-assignment UI, and
per-line audio playback/approve/regenerate. This slice shows NEEDS_REVIEW blockers as a
**read-only summary only**; the editing panels come later and will reuse `ReviewService`.

### Slice-1 follow-ups (not built this slice)
- **GUI overwrite affordance for an existing workspace.** `app_service.create_project` and
  `ProjectPresenter.create` already support `overwrite=True`, but the GUI never surfaces it:
  `ui/app.py`'s `new_requested` calls `presenter.create(epub, name)` with the default
  `overwrite=False`, so a duplicate workspace only shows the "already exists" error. A later
  slice should add an "overwrite?" confirmation (New Project dialog) that re-invokes
  `presenter.create(..., overwrite=True)`. The service/presenter seam is ready; only the
  Qt affordance is missing.

---

## Handoff
Coder: start with **task 1 (add `PreconditionError`) then tasks 2–7 — stand up the
`app_service/` package by moving the orchestration out of `cli.py`** — and immediately
prove task 8's regression gate (existing CLI suite green, unedited) before touching any
`ui/` code. The UI (tasks 9–13) builds on that stable, Qt-free service layer.

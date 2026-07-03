# Plan: Command-line pipeline runner (`castrun`)

## Goal
A Qt-free console entry point that drives a real EPUB through all six pipeline stages
(`parse → correct → attribute → review → synthesize → assemble`) from the terminal —
create a project, run until the review gate halts, resolve the gate (assign voices +
auto-accept attributions/suggestions), then re-run to synthesize and assemble the M4B —
before the PySide6 UI exists.

## Where this lives on the pipeline
The CLI is an **orchestrator only**. It constructs the existing `Pipeline`
(`pipeline/runner.py`), builds a `StageContext`, injects providers, and calls
`Pipeline.run(ctx)`. All stage logic, resumability, and the review gate already exist and
are unchanged. The only net-new non-orchestration piece is a small "create initial
`Project` from an EPUB path" helper (see Impact §8 — no such helper exists today).

Key runner behaviour the CLI leans on (confirmed in code):
- `Pipeline.run(ctx)` loads the project via `ctx.store.load()`, **skips** stages whose
  `is_complete()` is True, and **halts** the moment a stage returns
  `STOPPED / NEEDS_REVIEW / FAILED` (`_HALTING` in `runner.py`). It returns the last
  `StageResult`; if nothing ran it returns a synthetic `COMPLETED` for `ASSEMBLE`.
- `ReviewStage.run` returns `NEEDS_REVIEW` (with `describe_blockers(...)` as the message)
  while any blocker remains, and only writes `stage_status[REVIEW]=COMPLETED` once the gate
  is clear. So the first `run` naturally stops at review.
- Providers reach stages **only** via `StageContext(store, progress, llm, tts, config)`.
  `AttributeStage` reads `ctx.llm`; `SynthesizeStage` reads `ctx.tts`. `AssembleStage`
  takes its assembler via `__init__(assembler=None)` (its own test seam — NOT via `ctx`).
  `ParseStage` uses `parser_for()` unless a parser is injected via `__init__`.

## The crux: review is interactive, speakers are discovered during `attribute`
`project.speakers` is empty until `AttributeStage` runs `ensure_narrator` + registers
discovered characters. So voices cannot be assigned up front. The command flow mirrors the
gate's three criteria in `review/gate.py::review_blockers`:

1. **No `NEEDS_REVIEW` segments** — cleared by `approve_attribution(segment)` (keeps the
   LLM-proposed speaker) or `set_segment_speaker(...)`.
2. **No `PENDING` `TextSuggestion`s** — cleared by `accept_suggestion` / `reject_suggestion`.
3. **`unresolved_voices(project) == []`** — cleared by `register_voice_clip` + `assign_voice`
   (narrator included). **Cannot be auto-defaulted.**

Flow: `run` (parse→correct→attribute, halts at review) → user inspects (`status`,
`speakers`) → `assign-voice` for every speaker + `--auto-accept` clears criteria 1 & 2 →
`run` again continues review→synthesize→assemble.

---

## Files

New (all under `src/casttrophizer/`, Qt-free, import-cheap):

| Path | Purpose |
|------|---------|
| `cli.py` | The whole CLI: `main`, argparse wiring, subcommand handlers, `CliDeps`, `PrintReporter`, the project factory. Single module (see FLAG on package split). |

Changed:

| Path | Change |
|------|--------|
| `pyproject.toml` | Add `[project.scripts]` entry `castrun = "casttrophizer.cli:main"` (does **not** touch the existing `casttrophizer = ...ui.app:main`). |

New tests:

| Path | Purpose |
|------|---------|
| `tests/cli/test_cli_flow.py` | End-to-end `new → run → assign-voice → run` with injected fakes. |
| `tests/cli/test_cli_errors.py` | Error/edge paths (missing epub, no project, no LLM, etc.). |
| `tests/cli/test_cli_import.py` | Lazy-import / Qt-isolation for the CLI module. |

(The existing `tests/test_qt_isolation.py` already scans **all** of `src` for Qt imports,
so `cli.py` is covered by it automatically; the new import test adds torch/anthropic/
chatterbox isolation, which the existing scan does not assert.)

---

## Command surface (argparse, subcommands)

```
castrun [--workdir DIR] <command> ...

new           --epub PATH [--name NAME] [--workdir DIR] [--force]
run           [--until STAGE] [--auto-accept] [--provider claude|lmstudio]
status
speakers
assign-voice  SPEAKER CLIP            # SPEAKER = list index (int) or name (case-insensitive)
```

`--workdir` is a **global** option (default: current directory) naming the workspace the
command operates on. `new` creates it there; every other command loads an existing store
from there. No project registry (see FLAG).

### `new` — create a project from an EPUB
- Validates `--epub` exists and `parser_for(Path(epub))` supports it (fail fast with
  `EbookParseError` message).
- Resolves workspace dir = `--workdir` (or `config.workspaces_root / slug(name)` if no
  `--workdir`; FLAG the default). Refuses if `store.exists()` unless `--force`.
- Builds the initial `Project` (factory, §8) with `book.source_ebook_path` = **absolute**
  epub path, placeholder `title`/`author` (ParseStage overwrites from parsed metadata),
  empty chapters. Persists via `store.save(project)`.
- Does **not** run parse (kept single-pathed in `run`; FLAG).
- Calls: `parser_for`, `WorkspaceStore.for_dir`, `store.exists/save`, the project factory.

### `run` — advance the pipeline from wherever it is
Core logic:
1. Require `store.exists()`; `project = store.load()`.
2. Compute per-stage `is_complete(project)` to decide which providers are actually needed
   (so the review-gate flow never imports torch/anthropic unnecessarily):
   - build **LLM** iff `AttributeStage.is_complete` is False (parse/correct are cheap and
     will complete in the same run, so attribute is reachable).
   - build **TTS** iff `ReviewStage.is_complete` is True **and** `SynthesizeStage.is_complete`
     is False (synthesize is only *reached* once the gate has passed).
   - preflight **ffmpeg** (`assembler.is_available()`) iff `ReviewStage.is_complete` is True
     and `AssembleStage.is_complete` is False.
   Each build/preflight failure exits non-zero with an actionable message (see Edge cases).
3. `ctx = StageContext(store, progress, llm, tts, config)`; `result = pipeline.run(ctx)`.
4. If `result.status == NEEDS_REVIEW` and `--auto-accept`: apply auto-accept to a freshly
   loaded project (approve every `NEEDS_REVIEW` segment; resolve every `PENDING` suggestion),
   then `pipeline.run(ctx)` again. It will still halt at review if voices are unassigned.
5. Report: `COMPLETED` → print output M4B path (from `store.layout.output_dir`), exit 0;
   `NEEDS_REVIEW` → print `describe_blockers` + the speakers still needing a voice with their
   `speakers` indices + next-step hint, exit 3; `FAILED` → print `result.message`, exit 1;
   `STOPPED` → print "stopped; re-run to resume", exit 4.
- Calls: `Pipeline.run/stages`, each `stage.is_complete`, `build_llm_provider` /
  `build_tts_provider` (via `CliDeps` factories), `review_blockers`, `describe_blockers`,
  `unresolved_voices`, and the auto-accept helper.

### `status`
- `project = store.load()`; print `stage_status` for each `StageName` (or `-` if unset),
  the `next_stage(project).name` (or "complete"), and `describe_blockers(review_blockers)`.
- Calls: `store.load`, `Pipeline.next_stage`, `review_blockers`, `describe_blockers`.

### `speakers`
- List `project.speakers` in stable order (append order — narrator first, then discovered
  characters) with a fixed **index**, name, role, and voice state. Voice state = assigned +
  file exists, using `unresolved_voices(project)` membership to mark "NO VOICE".
- Calls: `store.load`, `unresolved_voices`.

### `assign-voice SPEAKER CLIP`
- Resolve `SPEAKER` = list index (int) or case-insensitive name match against
  `project.speakers`. Narrator is an ordinary speaker here.
- `svc = ReviewService(store, project)`; `clip = svc.register_voice_clip(CLIP, label=name)`
  (raises `ValueError` on a missing path — surface it cleanly); `svc.assign_voice(speaker, clip)`.
- Both persist via the service. Clears criterion 3 for that speaker.
- Calls: `ReviewService.register_voice_clip`, `ReviewService.assign_voice`.

### `--auto-accept` mechanics (helper `_auto_accept(store, project)`)
- `svc = ReviewService(store, project)`.
- For every segment with `review_status == NEEDS_REVIEW`: `svc.approve_attribution(seg)`
  (keeps the LLM-proposed speaker/role) → clears criterion 1.
- For every `TextSuggestion` with `status == PENDING`: **reject** it via
  `svc.reject_suggestion(line, sug.id)` → keeps `line.text` as the automated pass left it,
  clears criterion 2. (FLAG: accept vs reject direction — recommend reject = "text as-is".)
- Does **not** touch voices. After this, the only remaining blocker is criterion 3.

---

## Interfaces & data shapes (match existing code)

```python
# src/casttrophizer/cli.py

@dataclass
class CliDeps:
    """Injection seam so tests drive the CLI with fakes (no real API/model/ffmpeg)."""
    llm_factory: Callable[[AppConfig], LLMProvider] = _default_llm_factory
    tts_factory: Callable[[AppConfig], TTSProvider] = _default_tts_factory
    assembler: object | None = None          # -> AssembleStage(assembler=...); None = real M4BAssembler
    progress_factory: Callable[[], ProgressReporter] = PrintReporter
    config: AppConfig | None = None          # None -> AppConfig.from_env()

def main(argv: list[str] | None = None, *, deps: CliDeps | None = None) -> int: ...

def _build_pipeline(deps: CliDeps) -> Pipeline:
    """ParseStage, CorrectTextStage, SegmentAttributeStage, ReviewStage,
    SynthesizeStage, AssembleStage(assembler=deps.assembler)."""

def _new_project(epub: Path, name: str, workspace_dir: Path) -> Project:
    """Book(title=name, author='', source_ebook_path=str(epub.resolve()), chapters=[]);
    Project(schema_version=CURRENT_SCHEMA_VERSION, id=new_id('proj'), name=name,
            workspace_dir=str(workspace_dir), book=book)."""

class PrintReporter:                          # ProgressReporter protocol, like scripts/smoke_test.py
    def set_total(self, total: int) -> None: ...
    def advance(self, n: int = 1, *, message: str | None = None) -> None: ...
    def message(self, text: str) -> None: ...
    def should_stop(self) -> bool: ...        # returns self._stop (default False; see Ctrl-C FLAG)
```

Default factories (lazy — never import a concrete SDK at module import):
```python
def _default_llm_factory(cfg): return build_llm_provider(cfg)   # provider override applied in run
def _default_tts_factory(cfg): return build_tts_provider(cfg)
```

No schema changes. No new persisted shapes. `StageContext`, `ReviewService`, and the review
actions are used exactly as their current signatures define.

---

## Threading note
The CLI is single-threaded and synchronous — there is **no Qt event loop**, so the
QThread/worker rule does not apply. Long-running work (parse/attribute/synthesize) runs
inline in the foreground process; progress goes to `PrintReporter`. Nothing here touches
the Qt main thread because nothing imports Qt. Cooperative stop, if implemented, is a SIGINT
handler flipping `PrintReporter._stop` (polled by stages via `should_stop`).

---

## Provider + config wiring
- `config = deps.config or AppConfig.from_env()`.
- LLM: `build_llm_provider(config, provider=<--provider override>)`. Claude is primary and
  needs `ANTHROPIC_API_KEY`; LM Studio is the local backup. Built **only** when attribute is
  reachable (see `run` step 2). Preflight `llm.is_available()` → if False, exit 2 with:
  "attribution needs an LLM: set ANTHROPIC_API_KEY (or `--provider lmstudio` with LM Studio
  running)". This front-runs `AttributeStage`'s own `FAILED` guard for a cleaner message.
- TTS: `build_tts_provider(config)` imports `chatterbox` lazily inside the factory; wrap the
  build in `try/except ImportError` → exit 2 "synthesis needs the tts extra: pip install -e
  \".[tts]\"". Then `tts.is_available()` guard.
- ffmpeg: preflight `assembler.is_available()` (the real `M4BAssembler`, or the injected
  fake) → exit 2 "assembly needs ffmpeg on PATH". Mirrors `AssembleStage`'s own guard.
- Progress: `PrintReporter` prints per-stage messages/advances exactly like
  `scripts/smoke_test.py`'s `PrintReporter`.

---

## Edge cases (each with an actionable message + non-zero exit)
- **No `--epub` / bad EPUB** — `new` validates existence + `parser_for` support before writing.
- **Project doesn't exist** — any command but `new`: `store.exists()` False → "no project at
  <workdir>; run `castrun new --epub ...` first".
- **`new` over an existing project** — refuse unless `--force`.
- **`run` when already complete** — `Pipeline.run` returns synthetic `COMPLETED`; print
  "already complete; output: <m4b>" and exit 0 (no-op).
- **Attribute with no LLM/key** — preflight message above (exit 2), never a mid-stage crash.
- **Synthesize without the tts extra** — ImportError caught at build → exit 2.
- **Assemble without ffmpeg** — preflight (or `AssembleStage` FAILED) → clear message.
- **Speaker left without a voice** — `run` halts `NEEDS_REVIEW`; print
  `describe_blockers` ("voices needed for: narrator, Bob") + the `speakers` indices to fix.
  Backstopped by the synth voice precheck (`unresolved_voices`).
- **Resume after stop** — `Pipeline` skips complete stages and re-runs the stopped
  (idempotent) stage automatically; `run` needs no special handling.
- **`assign-voice` bad path** — `register_voice_clip` raises `ValueError` → print + exit 1.

---

## Impact on existing code (§8)
- **None to stages/services/schema** — pure orchestration.
- **One gap:** there is **no** "create a `Project` from an EPUB path" helper anywhere in
  `src/` (confirmed: `source_ebook_path` is only read by `ParseStage`; `scripts/smoke_test.py`
  hand-authors a fully-populated project). The CLI must construct the initial `Project`
  itself (`_new_project`, above). Recommend it live **in `cli.py`** for now (smallest change);
  FLAG that the future PySide6 "New Project" flow will want the same helper and it may later
  move to a shared `workspace/`-level factory. Keep the body trivial so a move is painless.

---

## Ordered task list (for the coder)
1. **`_new_project` factory** in `cli.py` — build the initial `Project`/`Book` from an epub
   path (absolute), placeholder metadata, empty chapters, `CURRENT_SCHEMA_VERSION`, `new_id`.
2. **`PrintReporter`** — copy the shape from `scripts/smoke_test.py`; add a `_stop` flag +
   `should_stop` returning it (wire SIGINT only if Ctrl-C is in scope — see FLAG).
3. **`CliDeps` + default lazy factories** — `build_llm_provider` / `build_tts_provider`
   wrappers; `progress_factory=PrintReporter`; `assembler=None`.
4. **`_build_pipeline(deps)`** — the six stages in order, `AssembleStage(assembler=deps.assembler)`.
5. **argparse wiring** — global `--workdir`; subparsers `new`, `run`, `status`, `speakers`,
   `assign-voice`; dispatch to handlers.
6. **`cmd_new`** — validate epub + parser support; resolve/guard workspace; save initial project.
7. **`cmd_run`** — provider-need computation (step 2 logic), preflights, `Pipeline.run`,
   `--auto-accept` re-run, status reporting with exit codes.
8. **`_auto_accept`** helper — approve `NEEDS_REVIEW` segments + resolve `PENDING` suggestions
   via `ReviewService`.
9. **`cmd_status`, `cmd_speakers`, `cmd_assign_voice`** — as specified (index/name resolution).
10. **`main(argv, *, deps)`** — parse, `deps = deps or CliDeps()`, dispatch, return int; module
    tail `if __name__ == "__main__": raise SystemExit(main())`.
11. **`pyproject.toml`** — add `castrun = "casttrophizer.cli:main"` under `[project.scripts]`.
12. **Tests** (see below).

---

## Verification strategy (for the tester)
All offline/deterministic — no real API, model, or ffmpeg. Inject via `CliDeps`:
`llm_factory=lambda cfg: FakeLLMProvider(text_script=...)`,
`tts_factory=lambda cfg: FakeTTSProvider()`, `assembler=FakeM4BAssembler()`. Parse/correct
run for real (offline: `ebooklib` + dictionary correctors). Reuse `make_sample_epub` and a
temp dummy clip file (any existing file passes `register_voice_clip`; `FakeTTS` ignores its
content).

`tests/cli/test_cli_flow.py`:
- `new --epub <sample>` creates `project.json`; `store.load()` has `source_ebook_path` set.
- First `run` (with fake LLM) advances parse→correct→attribute and **halts** at review;
  exit code 3; stdout contains blocker summary incl. "voices needed for".
- `speakers` lists narrator + discovered characters (Alice, Bob) with indices, all "NO VOICE".
- `assign-voice 0 <clip>` (and by name) assigns; re-running `speakers` shows them resolved.
- `run --auto-accept` after all voices assigned: fake LLM's low-confidence segment gets
  auto-approved, pending suggestions resolved, review passes, synthesize (fake TTS writes
  WAVs under `audio/`) and assemble (fake assembler writes stub M4B under `output/`) complete;
  exit 0; stdout names the output path; assert `FakeM4BAssembler.requests` captured ordered WAVs.
- `status` reflects `stage_status` transitions across the flow.
- A `run` on an already-complete project is a no-op reporting completion (exit 0).

`tests/cli/test_cli_errors.py`:
- `new` with a nonexistent/unsupported epub → non-zero + message.
- Any command with no project at `--workdir` → non-zero + "run `castrun new`".
- `run` when attribute is reachable but `llm.is_available()` is False (fake with
  `available=False`) → exit 2 naming `ANTHROPIC_API_KEY`; **no** stage ran.
- `tts_factory` raising `ImportError` → exit 2 naming the tts extra (only once review passed).
- `assembler` fake with `available=False` → ffmpeg preflight message.
- `assign-voice` with a missing clip path → `ValueError` surfaced, non-zero.

`tests/cli/test_cli_import.py`:
- `import casttrophizer.cli` leaves `PySide6`, `torch`, `anthropic`, `openai`, `chatterbox`
  **absent** from `sys.modules` (assert none newly imported). Mirrors `tests/test_lazy_imports.py`.

---

## Risks & open questions
- **Auto-accept suggestion direction** — accept (apply suggested text) vs reject (keep text
  as-is). Recommend **reject** (low-confidence OCR/spell guesses are often wrong; "as-is" is
  the safe non-interactive default). Confirm.
- **No granular per-item review in the CLI** — `--auto-accept` is all-or-nothing for
  attributions/suggestions. Granular `accept/reject <id>` commands are deferred; the terminal
  can still fully drive a book. Confirm this is acceptable for the pre-UI runner.
- **Workspace addressing** — `--workdir` (default cwd), no name→dir registry. A registry under
  `workspaces_root` can come later. Confirm.
- **`_new_project` placement** — in `cli.py` now vs a shared `workspace/` factory the UI will
  reuse. Recommend `cli.py` now, note the future move.

---

## DECISIONS FOR USER — CONFIRMED (build to these)
1. **CLI framework: CONFIRMED — stdlib `argparse`** (zero new deps).
2. **Command surface: CONFIRMED — subcommands** (`new`, `run`, `status`, `speakers`, `assign-voice`).
3. **Review-in-CLI strategy: CONFIRMED — resolution subcommands + re-run + `--auto-accept` on `run`.**
   `assign-voice` (register + assign a clip; narrator included) is the one mandatory manual step.
   `--auto-accept` clears criteria 1 & 2 non-interactively: approve NEEDS_REVIEW attributions
   as-proposed, and reject PENDING text suggestions (keep the text as-is — do NOT apply uncertain
   OCR/spellcheck guesses without review). Voice assignment (criterion 3) always stays manual.
4. **Entry-point name: CONFIRMED — `castrun`** (`[project.scripts]` -> the new CLI `main`; distinct
   from the GUI `casttrophizer`).
5. **Ctrl-C cooperative stop: CONFIRMED — include the minimal version** (a SIGINT handler that flips
   the reporter's stop flag; stages already checkpoint + persist on `should_stop`, so resume is free).

## Handoff
Coder: start with **task 1 (`_new_project` factory) and the `cmd_new` path**, then task 4/7
(`_build_pipeline` + `cmd_run` with the provider-need gating and `--auto-accept` re-run) —
that is the spine everything else hangs off. Build the `CliDeps` seam from the start so the
tester can inject fakes.

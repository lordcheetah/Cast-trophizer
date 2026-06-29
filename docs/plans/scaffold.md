# Plan: Initial Project Scaffold

**Goal:** Stand up the Cast-trophizer package skeleton — directory layout, core
interfaces/ABCs, the on-disk workspace state model, and a pytest harness — so the
coder and tester agents have real structure to build features against. SKELETON
ONLY: stubs with signatures + docstrings + `raise NotImplementedError`, no feature
logic.

---

## 1. Decisions (locked for this scaffold)

| Decision | Choice | Rationale |
|---|---|---|
| Package layout | **src layout** (`src/casttrophizer/`) | Prevents accidental import of the uninstalled package during tests; standard for libraries with a CLI/GUI entry point. |
| Import/package name | `casttrophizer` | Lowercase, no hyphen (PEP 8); distribution name `cast-trophizer`. |
| Build backend | `hatchling` | Minimal, src-layout-friendly, no setup.py. |
| State serialization | **JSON files** (one per project: `project.json`) + sidecar audio cache | Human-inspectable, diffable, trivially mockable in tests, resumable. No DB dependency for v1. |
| Provider boundary | ABCs in `providers/base.py`; concrete impls never imported by UI/pipeline | Keeps Claude/LM Studio/Chatterbox swappable per CLAUDE.md. |
| Pipeline orchestration | `Stage` ABC + `Pipeline` runner; stages communicate only through `Workspace` | Resumable, off-thread-friendly, testable in isolation. |
| Progress signaling | Framework-agnostic `ProgressReporter` callback protocol; Qt adapter emits signals | Pipeline/stages have **zero** Qt imports — keeps them unit-testable headless. |

---

## 2. Package & Directory Layout

```
cast-trophizer/
├── pyproject.toml                 # deps, tool config (ruff/black/mypy/pytest)
├── README.md                      # (stub) one-paragraph + quickstart placeholder
├── .gitignore                     # python, venv, workspaces/, __pycache__, .mypy_cache
├── docs/
│   └── plans/
│       └── scaffold.md            # this file
├── src/
│   └── casttrophizer/
│       ├── __init__.py            # __version__
│       ├── py.typed               # PEP 561 marker (typed package)
│       ├── config.py              # AppConfig dataclass; provider selection, paths, env
│       ├── errors.py              # exception hierarchy (CasttrophizerError + subtypes)
│       │
│       ├── domain/                # pure data shapes, no I/O, no provider/Qt imports
│       │   ├── __init__.py        # re-exports
│       │   ├── ids.py             # typed ID aliases / new_id() helper
│       │   ├── enums.py           # SpeakerRole, StageName, Confidence bands, ReviewStatus
│       │   ├── models.py          # Project, Book, Chapter, Line, Segment, Speaker, VoiceClip, Suggestion
│       │   └── serialization.py   # to_dict/from_dict (or pydantic) + schema_version
│       │
│       ├── workspace/             # on-disk per-project state model (read/write)
│       │   ├── __init__.py
│       │   ├── layout.py          # WorkspaceLayout: resolves all paths in a project dir
│       │   ├── store.py           # WorkspaceStore: load/save Project JSON (atomic write)
│       │   └── audio_cache.py     # AudioCache: cache-key + path for per-line audio
│       │
│       ├── pipeline/              # resumable stage framework (NO Qt imports)
│       │   ├── __init__.py
│       │   ├── stage.py           # Stage ABC, StageContext, StageResult
│       │   ├── progress.py        # ProgressReporter protocol + NullProgressReporter
│       │   ├── runner.py          # Pipeline: ordered stages, resume, stop-checking
│       │   └── stages/            # one stub module per pipeline stage
│       │       ├── __init__.py    # STAGE_ORDER registry
│       │       ├── parse.py       # ParseStage  (ebook → chapters/lines)
│       │       ├── correct.py     # CorrectTextStage (auto-fix + suggestions)
│       │       ├── attribute.py   # SegmentAttributeStage (speaker tagging via LLM)
│       │       ├── review.py      # ReviewStage (human-gate marker; mostly no-op compute)
│       │       ├── synthesize.py  # SynthesizeStage (TTS per line, cached)
│       │       └── assemble.py    # AssembleStage (ffmpeg/mutagen → M4B)
│       │
│       ├── providers/            # swappable external models behind ABCs
│       │   ├── __init__.py        # factory: build_llm_provider / build_tts_provider from config
│       │   ├── base.py            # LLMProvider ABC, TTSProvider ABC + request/response DTOs
│       │   ├── llm/
│       │   │   ├── __init__.py
│       │   │   ├── claude.py       # ClaudeProvider (Anthropic SDK) — primary
│       │   │   └── lmstudio.py     # LMStudioProvider (openai SDK, local endpoint) — backup
│       │   └── tts/
│       │       ├── __init__.py
│       │       └── chatterbox.py   # ChatterboxProvider wrapping Chatterbox TTS
│       │
│       ├── ebook/                # parsing behind an interface (EPUB first)
│       │   ├── __init__.py
│       │   ├── base.py            # EbookParser ABC, ParsedBook DTO
│       │   └── epub.py            # EpubParser (ebooklib + BeautifulSoup) stub
│       │
│       ├── audio/               # assembly helpers behind the synth/assemble stages
│       │   ├── __init__.py
│       │   └── assembler.py       # M4BAssembler (ffmpeg + mutagen) stub
│       │
│       └── ui/                   # PySide6 — ONLY place that imports Qt
│           ├── __init__.py
│           ├── app.py             # main() entry point: QApplication + MainWindow
│           ├── main_window.py     # MainWindow shell (stub)
│           └── workers.py         # PipelineWorker(QThread/QObject) + QtProgressReporter
│
└── tests/
    ├── conftest.py                # shared fixtures (workspace, fakes, sample data)
    ├── fakes/
    │   ├── __init__.py
    │   ├── fake_llm.py            # FakeLLMProvider (deterministic, offline)
    │   ├── fake_tts.py            # FakeTTSProvider (writes silent wav, offline)
    │   └── fake_progress.py       # RecordingProgressReporter (captures events)
    ├── data/
    │   ├── make_sample_epub.py    # builds tiny EPUB fixture at test time (no binary in repo)
    │   └── voices/                # tiny placeholder voice clips (generated, see harness §5)
    ├── domain/test_models.py
    ├── workspace/test_store.py
    ├── pipeline/test_runner.py
    └── providers/test_factory.py
```

**Entry point:** `pyproject.toml` exposes console + GUI script `casttrophizer =
"casttrophizer.ui.app:main"`. Headless logic is fully importable without Qt.

**Threading rule (enforced by layout):** only `ui/` imports `PySide6`. The pipeline,
providers, domain, and workspace packages are Qt-free, so the long-running work
(`parse`, `attribute`, `synthesize`) runs inside `ui/workers.py::PipelineWorker` on a
`QThread`, while progress reaches the main thread via `QtProgressReporter` Qt signals.
A lint guard (test) asserts no `PySide6` import outside `ui/`.

### pyproject.toml sketch

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "cast-trophizer"
version = "0.1.0"
description = "Convert ebooks into full-cast audiobooks using Chatterbox TTS."
requires-python = ">=3.11"
dependencies = [
    "PySide6>=6.6",            # Qt GUI
    "ebooklib>=0.18",         # EPUB parsing
    "beautifulsoup4>=4.12",   # HTML extraction within EPUB
    "mutagen>=1.47",          # M4B tagging / chapters / cover
    "anthropic>=0.39",        # primary LLM provider (Claude)
    "openai>=1.40",           # backup LLM via LM Studio OpenAI-compatible endpoint
]
# Note: ffmpeg is an external binary dependency (not pip). Documented in README;
# AssembleStage shells out to it.

[project.optional-dependencies]
# Heavy torch-based TTS gated behind an extra so dev/CI installs stay light.
# Import path: `from chatterbox.tts import ChatterboxTTS` (dist: chatterbox-tts).
tts = [
    "chatterbox-tts",
]
dev = [
    "pytest>=8",
    "pytest-mock>=3.12",
    "ruff>=0.6",
    "black>=24",
    "mypy>=1.11",
]

[project.scripts]
casttrophizer = "casttrophizer.ui.app:main"

[tool.hatch.build.targets.wheel]
packages = ["src/casttrophizer"]

[tool.ruff]
line-length = 100
target-version = "py311"
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.black]
line-length = 100
target-version = ["py311"]

[tool.mypy]
python_version = "3.11"
strict = true
files = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]      # if not relying on editable install
testpaths = ["tests"]
addopts = "-q"
```

---

## 3. Pipeline-Stage Interface

Stages are pure, resumable units that read/write **only** through the `Workspace`
and report progress through an injected `ProgressReporter`. No Qt, no provider
construction inside a stage — providers are passed in via `StageContext`.

`src/casttrophizer/pipeline/progress.py`
```python
from typing import Protocol

class ProgressReporter(Protocol):
    def set_total(self, total: int) -> None: ...
    def advance(self, n: int = 1, *, message: str | None = None) -> None: ...
    def message(self, text: str) -> None: ...
    def should_stop(self) -> bool:
        """True if the caller requested a cooperative stop; stages poll this."""

class NullProgressReporter:
    """No-op reporter for headless/non-interactive runs and tests."""
    # ...trivial implementations...
```

`src/casttrophizer/pipeline/stage.py`
```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from casttrophizer.domain.enums import StageName, ReviewStatus
from casttrophizer.domain.models import Project
from casttrophizer.workspace.store import WorkspaceStore
from casttrophizer.providers.base import LLMProvider, TTSProvider
from casttrophizer.pipeline.progress import ProgressReporter

@dataclass
class StageContext:
    """Everything a stage needs, injected (keeps stages provider-agnostic)."""
    store: WorkspaceStore
    progress: ProgressReporter
    llm: LLMProvider | None = None     # None when stage doesn't need it
    tts: TTSProvider | None = None
    config: "AppConfig | None" = None

@dataclass
class StageResult:
    stage: StageName
    status: ReviewStatus            # COMPLETED / NEEDS_REVIEW / STOPPED / FAILED
    message: str = ""

class Stage(ABC):
    name: StageName

    @abstractmethod
    def is_complete(self, project: Project) -> bool:
        """True if persisted state shows this stage already finished — enables resume."""

    @abstractmethod
    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Do the work, mutating `project` and persisting via ctx.store.
        MUST poll ctx.progress.should_stop() at safe checkpoints and return a
        STOPPED result (with partial state already saved) when asked to stop.
        MUST be idempotent / resumable: re-running after a stop continues, not restarts.
        """
```

`src/casttrophizer/pipeline/runner.py`
```python
class Pipeline:
    """Ordered, resumable runner over STAGE_ORDER."""
    def __init__(self, stages: list[Stage]) -> None: ...

    def run(self, ctx: StageContext, *, until: StageName | None = None) -> StageResult:
        """Load project, run each not-yet-complete stage in order.
        Stops early on STOPPED/NEEDS_REVIEW/FAILED, or after `until`.
        State is persisted by each stage, so a later call resumes where it left off.
        """

    def next_stage(self, project: Project) -> Stage | None:
        """First stage whose is_complete() is False — drives 'Resume' in the UI."""
```

**Resume model:** each stage records its own completion in `project.json`
(`stage_status[stage] = COMPLETED`). `Pipeline.run` skips complete stages.
Within a long stage (e.g. synthesize), per-item progress is recoverable because the
unit of work (a `Line`) carries its own status + audio-cache reference, so a restart
re-scans lines and skips ones already done.

**Stage stubs** (`pipeline/stages/*.py`) — each subclasses `Stage`, sets `name`, and
stubs `is_complete`/`run` with `raise NotImplementedError`. Required signatures noted:
- `ParseStage` — uses `EbookParser` (from ctx via factory) to fill chapters/lines.
- `CorrectTextStage` — populates `Line.suggestions`; auto-applies only high-confidence.
- `SegmentAttributeStage` — uses `ctx.llm` to set `Segment.speaker_id` + `confidence`.
- `ReviewStage` — human gate: returns `NEEDS_REVIEW` until UI marks lines reviewed.
- `SynthesizeStage` — uses `ctx.tts`; writes per-line audio to `AudioCache`, skips cached.
- `AssembleStage` — uses `M4BAssembler` to produce the final M4B.

---

## 4. Provider Interfaces

UI and pipeline depend only on the ABCs in `providers/base.py`. Concrete providers
are built by the factory in `providers/__init__.py` from `AppConfig`, then injected
through `StageContext`.

`src/casttrophizer/providers/base.py`
```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

# ---- LLM ----
@dataclass
class AttributionCandidate:
    segment_id: str
    speaker_name: str | None     # None => narrator
    confidence: float            # 0.0–1.0; low values flagged for review
    rationale: str = ""

@dataclass
class LLMMessage:
    role: str                    # "system" | "user" | "assistant"
    content: str

class LLMProvider(ABC):
    name: str

    @abstractmethod
    def complete(self, messages: list[LLMMessage], *, max_tokens: int = 1024,
                 temperature: float = 0.0) -> str:
        """Single completion. Used by text-correction + generic prompts."""

    @abstractmethod
    def attribute_speakers(self, *, context: str, candidates: list[str],
                           known_speakers: list[str]) -> list[AttributionCandidate]:
        """Propose speaker per segment with confidence. LLM proposes, user confirms."""

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap reachability/credentials check; lets UI fall back primary→backup."""

# ---- TTS ----
@dataclass
class SynthesisRequest:
    text: str
    voice_clip_path: Path        # read-only reference clip to clone
    params: dict                 # exaggeration, cfg, seed, etc. (provider-specific)

@dataclass
class SynthesisResult:
    audio_path: Path             # written into the workspace audio cache
    sample_rate: int
    duration_s: float

class TTSProvider(ABC):
    name: str

    @abstractmethod
    def synthesize(self, request: SynthesisRequest, out_path: Path) -> SynthesisResult:
        """Generate one line of audio to out_path. Deterministic given same seed."""

    @abstractmethod
    def is_available(self) -> bool: ...
```

`src/casttrophizer/providers/__init__.py`
```python
def build_llm_provider(config: AppConfig) -> LLMProvider:
    """primary='claude' -> ClaudeProvider; 'lmstudio' -> LMStudioProvider.
    Only this factory imports concrete provider modules."""

def build_tts_provider(config: AppConfig) -> TTSProvider:
    """Returns ChatterboxProvider (only TTS impl for v1)."""
```

Concrete stubs:
- `llm/claude.py::ClaudeProvider` — wraps `anthropic.Anthropic`; reads model id +
  API key from config/env. (See `claude-api` skill for current model ids; coder must
  not hardcode from memory.)
- `llm/lmstudio.py::LMStudioProvider` — wraps `openai.OpenAI(base_url=..., api_key="lm-studio")`
  pointed at the local LM Studio OpenAI-compatible endpoint.
- `tts/chatterbox.py::ChatterboxProvider` — wraps Chatterbox; loads model lazily so
  importing the module is cheap and offline-safe (model load only in `synthesize`).

---

## 5. Domain / Data Model & On-Disk Layout

Pure dataclasses in `domain/models.py` (no I/O). Serialization is centralized so the
on-disk schema is versioned and mockable.

```python
# domain/enums.py
class SpeakerRole(StrEnum): NARRATOR = "narrator"; CHARACTER = "character"
class StageName(StrEnum):
    PARSE="parse"; CORRECT="correct"; ATTRIBUTE="attribute"
    REVIEW="review"; SYNTHESIZE="synthesize"; ASSEMBLE="assemble"
class ReviewStatus(StrEnum):
    PENDING="pending"; AUTO_APPLIED="auto_applied"; APPROVED="approved"
    REJECTED="rejected"; NEEDS_REVIEW="needs_review"; COMPLETED="completed"
    STOPPED="stopped"; FAILED="failed"

# domain/models.py (signatures; all @dataclass, typed)
@dataclass
class TextSuggestion:
    id: str
    original: str
    suggested: str
    reason: str                 # "ocr-artifact" | "spellcheck" | ...
    confidence: float
    status: ReviewStatus        # AUTO_APPLIED for high-confidence, else PENDING

@dataclass
class Segment:                  # an attributable unit within a line/paragraph
    id: str
    text: str
    speaker_id: str | None      # resolves to Speaker.id; None == narrator
    role: SpeakerRole
    confidence: float           # attribution confidence; low => flagged in review
    review_status: ReviewStatus

@dataclass
class Line:                     # one renderable text unit (paragraph or quote run)
    id: str
    chapter_id: str
    order: int
    text: str
    segments: list[Segment]
    suggestions: list[TextSuggestion]
    audio_cache_key: str | None # -> AudioCache; None until synthesized
    audio_status: ReviewStatus  # PENDING/APPROVED/REJECTED for per-line review

@dataclass
class Chapter:
    id: str
    order: int
    title: str
    lines: list[Line]

@dataclass
class Speaker:
    id: str
    name: str                   # "narrator" reserved; characters by display name
    role: SpeakerRole
    voice_clip_id: str | None   # -> VoiceClip; mapping is user-assigned

@dataclass
class VoiceClip:
    id: str
    source_path: str            # READ-ONLY input path (not copied/mutated)
    label: str

@dataclass
class Book:
    title: str
    author: str
    source_ebook_path: str      # READ-ONLY input
    cover_image_path: str | None
    chapters: list[Chapter]

@dataclass
class Project:
    schema_version: int         # bump => migration hook in serialization.py
    id: str
    name: str
    workspace_dir: str
    book: Book
    speakers: list[Speaker]
    voice_clips: list[VoiceClip]
    stage_status: dict[str, ReviewStatus]   # StageName -> status (drives resume)
    tts_params: dict                          # global synthesis defaults
```

### On-disk workspace (`workspace/layout.py`)

Per-project workspace dir (chosen by user); **source ebook & voice clips stay where
they are — referenced by absolute path, never copied or modified.**

```
<workspace_dir>/
├── project.json          # serialized Project (atomic write: tmp + os.replace)
├── audio/                # AudioCache: per-line generated audio
│   └── <cache_key>.wav   # cache_key = hash(line_text + voice_clip_id + tts_params)
├── output/
│   └── <book>.m4b        # final assembled audiobook
└── logs/                 # optional stage logs
```

`WorkspaceLayout` resolves these paths; `WorkspaceStore.load()/save(project)` does the
atomic JSON round-trip via `domain/serialization.py`. `AudioCache.key_for(line, project)`
computes the deterministic cache key so `SynthesizeStage` regenerates a line **only**
when text/voice/params change (per the "full render is expensive" principle).

`domain/serialization.py` provides `project_to_dict` / `project_from_dict` with a
`schema_version` check and a migration dispatch stub — so the persisted shape can
evolve without breaking old workspaces.

---

## 6. pytest Harness

Tests run fully offline and deterministically — no network, no real TTS/LLM, no
ffmpeg/Chatterbox model loads.

**Fakes** (`tests/fakes/`):
- `FakeLLMProvider(LLMProvider)` — `attribute_speakers` returns scripted candidates
  keyed by input; `complete` echoes/returns a canned string. Lets attribution tests
  assert deterministic confidence handling (including a low-confidence → review path).
- `FakeTTSProvider(TTSProvider)` — writes a tiny silent WAV to `out_path` (via stdlib
  `wave`), returns a fixed sample_rate/duration. No model load, no audio deps.
- `RecordingProgressReporter(ProgressReporter)` — records `advance`/`message` calls and
  exposes a controllable `should_stop` so stop/resume can be tested without Qt.

**Fixtures** (`tests/conftest.py`):
- `tmp_workspace(tmp_path)` → initialized `WorkspaceLayout` + `WorkspaceStore` in a
  temp dir.
- `sample_epub(tmp_path)` → built by `tests/data/make_sample_epub.py` using `ebooklib`
  at test time (2 chapters, a few quoted lines). **No binary EPUB committed** — keeps
  the repo clean and the fixture transparent.
- `fake_voice_clips(tmp_path)` → 2 tiny generated WAVs written via stdlib `wave`
  (read-only inputs). No real voice data in the repo.
- `sample_project(tmp_workspace, sample_epub, fake_voice_clips)` → a minimal `Project`
  saved to disk, ready for stage/store tests.
- `fakes` fixtures returning the fake providers above.

**Seed tests** (prove the skeleton holds together — coder fills bodies as features land):
- `domain/test_models.py` — round-trip `project_to_dict`→`project_from_dict` is lossless;
  schema_version present.
- `workspace/test_store.py` — `save` then `load` returns an equal `Project`; write is
  atomic (no partial file on simulated failure); `AudioCache.key_for` is stable and
  changes when text/voice/params change.
- `pipeline/test_runner.py` — `Pipeline.next_stage` returns first incomplete stage;
  a stage returning `STOPPED` halts the run and a re-run resumes; `should_stop` is
  honored. Uses fake providers + `RecordingProgressReporter`.
- `providers/test_factory.py` — `build_llm_provider` selects Claude vs LM Studio from
  config **without** importing concrete modules at top level (lazy import inside factory),
  and the result satisfies the `LLMProvider` ABC.
- `test_qt_isolation` (in conftest area or a dedicated test) — greps `src/casttrophizer`
  and asserts no `PySide6`/`PyQt` import appears outside `ui/`. Guards the threading rule.

CI runs `ruff check`, `black --check`, `mypy src`, `pytest`.

---

## 7. Ordered Task List (coder executes in sequence)

1. **Project metadata** — create `pyproject.toml` (§2 sketch), `.gitignore`, `README.md`
   stub, `src/casttrophizer/__init__.py` (`__version__`), `py.typed`. Verify
   `pip install -e ".[dev]"` succeeds (or that deps resolve — see Risks on chatterbox/PySide6).
2. **errors.py + config.py** — exception hierarchy; `AppConfig` dataclass (provider
   selection: primary/backup, model ids via env, workspace defaults).
3. **domain/** — `ids.py`, `enums.py`, `models.py` (dataclasses per §5),
   `serialization.py` (to/from dict + schema_version + migration stub). Add
   `domain/__init__.py` re-exports.
4. **workspace/** — `layout.py`, `store.py` (atomic save/load), `audio_cache.py`
   (deterministic key). 
5. **providers/base.py** — `LLMProvider`/`TTSProvider` ABCs + DTOs (§4).
6. **providers/__init__.py** — `build_llm_provider`/`build_tts_provider` factories
   (lazy concrete imports). Concrete stubs: `llm/claude.py`, `llm/lmstudio.py`,
   `tts/chatterbox.py` (all `NotImplementedError`, lazy heavy imports).
7. **ebook/** — `base.py` (`EbookParser` ABC + `ParsedBook`), `epub.py` (`EpubParser` stub).
8. **audio/assembler.py** — `M4BAssembler` stub (ffmpeg/mutagen, lazy).
9. **pipeline/** — `progress.py`, `stage.py`, `runner.py`, then `stages/*` stubs +
   `STAGE_ORDER` registry (§3).
10. **ui/** — `app.py::main`, `main_window.py::MainWindow` shell, `workers.py`
    (`PipelineWorker` QThread/QObject + `QtProgressReporter` implementing
    `ProgressReporter` via Qt signals). Qt imports live ONLY here.
11. **tests/** — `fakes/`, `data/make_sample_epub.py`, `conftest.py` fixtures, then the
    seed tests in §6. Ensure `pytest` is green (stubs may `pytest.skip`/`xfail` where a
    feature body is required, but structural tests — serialization round-trip, store,
    factory, qt-isolation — must actually pass).
12. **Tooling pass** — run `ruff`, `black`, `mypy src`; fix until clean.

Dependencies: 1→2→3→4 (store needs models); 5→6 (factory needs ABCs); 3 before 9
(stages reference enums/models); 9 before 10 (worker drives Pipeline); 11 last.

---

## 8. Risks & Open Questions

- **Chatterbox PyPI name & import path** — **RESOLVED:** dist `chatterbox-tts`, import
  `from chatterbox.tts import ChatterboxTTS`, gated behind the `tts` optional-extra. It pulls
  heavy torch deps that must NOT be imported at module top level — `ChatterboxProvider` lazy-imports
  inside `synthesize`/`is_available`, so importing `providers.tts.chatterbox` stays cheap and CI
  (which installs without the `tts` extra) never loads torch.
- **ffmpeg is not a pip dependency** — it's an external binary. Decide: document as a
  prerequisite (simplest, recommended for v1) vs. bundle. `AssembleStage` shells out.
- **Claude model id** — coder must pull the current model id from the `claude-api` skill,
  not hardcode from memory. Config should make it overridable via env.
- **Serialization: dataclasses+manual vs. pydantic** — **RESOLVED: stdlib dataclasses +
  hand-written serialization** with `schema_version` (no pydantic). Zero extra runtime deps,
  easy mocking; a future switch to pydantic stays isolated to `domain/`.
- **Segment vs. Line granularity** — **RESOLVED: TTS renders per-Segment.** A Line is the
  renderable text unit; a Segment is the attributable sub-unit (narration vs. inline quote),
  and each Segment is synthesized with its own voice and stitched in order during assembly.
  **AudioCache key is therefore per-Segment** = `hash(segment_text + voice_clip_id + tts_params)`
  (move `audio_cache_key`/`audio_status` onto `Segment`, not `Line`).
- **PySide6 + Chatterbox/torch env conflicts** — large native deps; CI may need them
  optional. The Qt-isolation rule keeps headless tests installable without a display.
- **GSD escalation** — this is a multi-module greenfield scaffold; if the main agent
  wants per-stage rigor (research + plan-checker gates), `/gsd-plan-phase` is the heavier
  path. For a skeleton, this single plan is sufficient.

---

## 9. Verification Strategy (for the tester)

The tester must prove the skeleton is real structure, not empty files:

1. **Importability** — every module imports without side effects and **without** loading
   torch/Chatterbox/Anthropic network clients (assert lazy: importing
   `providers.tts.chatterbox` does not import torch).
2. **Domain round-trip** — `project_from_dict(project_to_dict(p)) == p`; `schema_version`
   serialized; unknown future version raises a clear migration error.
3. **Workspace persistence** — save/load equality; atomic write leaves no partial
   `project.json` on simulated mid-write failure; source ebook/voice paths are referenced,
   never copied or mutated (assert input files unchanged).
4. **AudioCache determinism** — key stable across runs; changes iff text/voice/params change.
5. **Pipeline resume/stop** — with fakes: `next_stage` finds first incomplete stage; a
   stage honoring `should_stop` returns `STOPPED` with state saved; re-run resumes and
   completes; completed stages are skipped.
6. **Provider boundary** — `build_llm_provider` returns the configured impl satisfying the
   ABC; UI/pipeline modules never import concrete provider classes (grep assertion).
7. **Qt isolation** — no `PySide6`/`PyQt` import outside `ui/`; headless test suite runs
   with no display/QApplication.
8. **Offline guarantee** — full suite passes with networking disabled and no real models;
   all LLM/TTS go through fakes.
9. **Tooling gate** — `ruff check`, `black --check`, `mypy src`, `pytest` all green.

Tests mock TTS/LLM via the fakes in `tests/fakes/`; **no test calls a real model.**

---

## Handoff

**Coder, build Task 1 first:** create `pyproject.toml` (per the §2 sketch), `.gitignore`,
`README.md` stub, and `src/casttrophizer/__init__.py` + `py.typed`, then confirm the dev
environment installs and `python -c "import casttrophizer"` works before proceeding to the
domain layer (Task 3). Resolve the chatterbox/ffmpeg packaging open questions (§8) at Task 1
if they block install.

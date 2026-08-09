# Cast-trophizer

Convert ebooks into **full-cast audiobooks** using [Chatterbox](https://github.com/resemble-ai/chatterbox)
TTS. You supply voice clips that Chatterbox clones to voice individual speakers; the app figures
out which lines belong to the narrator vs. specific characters, lets you review and correct the
text and per-line audio, then assembles a chaptered M4B with cover art.

## Status

All six pipeline stages **plus a complete PySide6 desktop app** are implemented and tested
(786 offline tests), and the full path has been verified end-to-end on real hardware — a real
Chatterbox render and a real `ffmpeg`/`mutagen` assembly produce a playable, chaptered M4B with
per-speaker voices and embedded cover art.

Two ways to drive it, sharing one Qt-free core:

- **Desktop app** (`casttrophizer`) — open/create a project, run the pipeline with live progress,
  and review everything in-app: text corrections, speaker attribution, voice assignment (with
  reference-clip audition), and per-line audio with in-session **regenerate**. See
  [Using the desktop app](#using-the-desktop-app).
- **CLI** (`castrun`) — the same pipeline headless from the terminal, plus
  [`scripts/smoke_test.py`](scripts/smoke_test.py) for a runnable real-render example.

Heavy TTS/LLM/ffmpeg boundaries are mocked in the test suite, so a green `pytest` never touches a
real model.

## How it works

The pipeline runs in separable, **resumable** stages, each persisting state to a per-project
workspace directory so you can stop and pick up where you left off:

```
parse → correct text → segment & attribute → review → synthesize → assemble
```

- **parse** — EPUB → chapters/lines (`ebooklib` + BeautifulSoup).
- **correct** — deterministic OCR-artifact + spellcheck pass; only high-confidence fixes auto-apply,
  the rest are surfaced for review.
- **attribute** — split each line into narration/quote segments and use an LLM to propose a speaker
  per segment with a confidence score; low-confidence attributions are flagged, never silently
  committed.
- **review** — a gate (no NEEDS_REVIEW segments, no PENDING text suggestions, every speaker has an
  assigned voice) plus the operations the desktop app and CLI drive (confirm attribution, edit text,
  register and assign voice clips, audition and regenerate per-line audio).
- **synthesize** — render each segment with its speaker's voice via Chatterbox; per-segment audio is
  cached and only re-rendered when its text/voice/params change.
- **assemble** — stitch the per-segment WAVs into a chaptered M4B with `ffmpeg` + `mutagen`, with an
  embedded cover.

Providers live behind interfaces so they can be swapped without touching the pipeline: **Claude**
(primary) / **LM Studio** (local backup) for attribution, **Chatterbox** for TTS. Qt is confined to
the `ui/` package; everything else is Qt-free and headless-testable.

## Prerequisites

- **Python 3.11+**
- **ffmpeg** — required for assembly (an external binary, *not* a pip dependency). Install via your
  OS package manager (`winget install ffmpeg`, `brew install ffmpeg`, `apt install ffmpeg`) and make
  sure it's on your `PATH`.
- **A TTS backend (for real audio):** Chatterbox, gated behind the `tts` extra (it pulls heavy torch
  deps). Without it the app still imports, runs, and tests fully — synthesis just isn't available.
- **An LLM (for attribution):** an Anthropic API key (`ANTHROPIC_API_KEY`) for Claude, or a local
  [LM Studio](https://lmstudio.ai/) server for the offline backup.

## Install

```sh
python -m venv .venv
# activate the venv (Windows: .venv\Scripts\Activate.ps1), then:

# Dev only — everything except Chatterbox (enough to run the whole test suite):
pip install -e ".[dev]"

# With real TTS (Chatterbox + torch):
pip install -e ".[dev,tts]"

python -c "import casttrophizer; print(casttrophizer.__version__)"
```

### GPU (NVIDIA) — important for render speed

Installing the `tts` extra pulls a **CPU-only** torch by default, so Chatterbox runs on the CPU
(slow). To use an NVIDIA GPU, install a CUDA build of torch **from PyTorch's CUDA index**:

```sh
pip uninstall -y torch torchaudio
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

You want `is_available()` to print `True`.

> **RTX 50-series (Blackwell) note:** a 50-series card (e.g. RTX 5080, compute capability `sm_120`)
> only works with the **CUDA 12.8 (`cu128`)** wheels or newer — the older `cu124`/`cu126` builds have
> no Blackwell kernels and will report CUDA as unavailable even with the right driver. `cu128` is the
> stable target; CUDA 13 wheels are still bleeding-edge. `ChatterboxProvider` auto-selects the GPU
> when torch reports CUDA available — no code change needed.

## Configuration

`AppConfig.from_env()` reads these environment variables (all optional; sensible defaults otherwise):

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (attribution) | — (read from env) |
| `CASTTROPHIZER_CLAUDE_MODEL` | Claude model id | `claude-opus-4-8` |
| `CASTTROPHIZER_LLM_PROVIDER` | Primary LLM (`claude` / `lmstudio`) | `claude` |
| `CASTTROPHIZER_LLM_BACKUP` | Backup LLM | `lmstudio` |
| `CASTTROPHIZER_LMSTUDIO_BASE_URL` | LM Studio OpenAI-compatible endpoint | `http://localhost:1234/v1` |
| `CASTTROPHIZER_TTS_PROVIDER` | TTS backend | `chatterbox` |
| `CASTTROPHIZER_WORKSPACES_ROOT` | Parent dir for new project workspaces | `~/.casttrophizer/workspaces` |

## Using the desktop app

Launch the GUI with the `casttrophizer` console script (installed by `pip install -e .`). For a full
session you'll want `ANTHROPIC_API_KEY` set (attribution), the `tts` extra installed (synthesis +
Regenerate), and `ffmpeg` on your `PATH` (assembly) — the same prerequisites as the headless flow.

```sh
casttrophizer
```

### The shell

The home page drives the pipeline and gates the review work:

```
┌ Cast-trophizer ───────────────────────────────┐
│ [Open Project…] [New Project…]                 │
│ Encyclopedia Brown 01                          │
│   work/eb1b                                    │
│ ┌────────────────────────────────────────────┐│
│ │    parse       completed                   ││
│ │    correct     completed                   ││
│ │ ▶  attribute   needs_review                ││
│ │    review      -                           ││
│ │    synthesize  -                           ││
│ │    assemble    -                           ││
│ └────────────────────────────────────────────┘│
│ next: attribute                                │
│ [Run] [Stop]   ▓▓▓▓▓░░░░░                       │
│ (live log output…)                             │
│ Needs review: 6 attributions, 121 text …       │
│ [Review text] [Review attributions]            │
│ [Assign voices] [Review audio]                 │
└────────────────────────────────────────────────┘
```

- **Open / New** — *New* picks an EPUB and creates a workspace (parses immediately); *Open* picks an
  existing workspace directory.
- **Stage list** — one row per stage; `▶` marks what the next **Run** will execute.
- **Run / Stop** — Run advances the pipeline on a background thread (progress bar and log update
  live); Stop is a cooperative stop.
- **Review buttons** — each stays disabled until it has work, and all four are disabled while a run
  is in flight (so an edit can't race the worker's save).

### The workflow

**1. New/Open, then Run.** parse → correct → attribute run, then the pipeline halts for review and
the relevant buttons enable.

**2. Resolve the review gate.** Each page has a `← Back to project` button, and the shell's blocker
summary ticks down live as you edit:

| Page | Filter (default on) | What you do |
|---|---|---|
| **Review text** | Pending only | `Accept` / `Reject` / `Edit line…` — an accepted fix propagates into the spoken segment |
| **Review attributions** | Needs review only | `Approve` / `Reassign` (existing speaker) / `New speaker…` / `Reject`; keys **N/J** next, **P/K** prev, **Enter** approve |
| **Assign voices** | Needs voice only | per speaker: `Assign clip…` / `Unassign` / category / **▶** audition; bulk rows per category (man/woman/boy/girl/default) with `Pick…`, **▶**, and `Apply to unvoiced` |

**3. Run again.** With the gate clear, synthesize renders each segment (on your GPU) and assemble
produces the M4B — the outcome line shows its path. **Review audio** now enables.

**4. Review audio.** Audition and curate before you're done (`Unapproved only` filter by default):

- **▶ Play** / **Play next** — listen through the takes.
- **Approve** — mark a good take.
- **Regenerate** — re-renders that one segment *immediately* (a fresh seed each time) and auto-plays
  the new take, so you can re-roll until happy. Disabled if the `tts` extra isn't installed.
- **Re-render on next run** — the lighter "mark it, batch it later" option.

A regenerate re-opens synthesize + assemble, so a final **Run** re-renders only the changed segments
(the rest are cached) and rebuilds the M4B.

In short: **New → Run → review text/attribution/voice → Run → audition & re-roll audio → Run → M4B.**

## Running the tests

From the repo root with the venv active:

```sh
pytest                    # 786 tests, fully offline (TTS/LLM/ffmpeg are all mocked)

# the full gate:
ruff check src tests
black --check src tests
mypy src
```

The unit tests never hit a real model, API, or `ffmpeg` — they mock those boundaries, so a green
`pytest` does not exercise the real Chatterbox or ffmpeg paths. That's what the smoke test is for.

## Smoke test (real Chatterbox + ffmpeg)

[`scripts/smoke_test.py`](scripts/smoke_test.py) hand-builds a tiny, already-attributed two-chapter
project from voice clips you supply and runs the **real** synthesize and assemble stages, producing
a playable M4B. It needs no ebook and no LLM key — just reference clips and the `tts` extra +
`ffmpeg` installed.

```sh
# narrator only:
python scripts/smoke_test.py --narrator path/to/narrator.wav

# full cast + cover, into an explicit workdir:
python scripts/smoke_test.py \
  --narrator narrator.wav \
  --character Alice=alice.wav \
  --character Bob=bob.wav \
  --cover cover.jpg \
  --workdir .smoke-out
```

On success it prints the path to the assembled `.m4b`. Play it and confirm the per-speaker voices,
chapter markers, tags, and cover. The first run downloads the Chatterbox model (network + disk).

## Project layout

```
src/casttrophizer/
├── domain/        # dataclass models + versioned JSON serialization
├── workspace/     # on-disk project state (atomic writes) + per-segment audio cache
├── pipeline/      # Stage framework + the six stages
├── providers/     # LLM (Claude / LM Studio) + TTS (Chatterbox) behind interfaces
├── ebook/         # EPUB parsing (ebooklib + BeautifulSoup)
├── text/          # OCR-artifact + spellcheck correction
├── attribution/   # quote segmentation + LLM speaker attribution
├── review/        # headless review gate + review actions
├── audio/         # synthesize orchestration + the M4B assembler
└── ui/            # PySide6 desktop app (shell + review panels, MVP split)
```

Per-stage design docs live under [`docs/plans/`](docs/plans/).

## License

TBD.

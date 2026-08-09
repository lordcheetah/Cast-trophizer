# Cast-trophizer

Convert ebooks into **full-cast audiobooks** using [Chatterbox](https://github.com/resemble-ai/chatterbox)
TTS. You supply voice clips that Chatterbox clones to voice individual speakers; the app figures
out which lines belong to the narrator vs. specific characters, lets you review and correct the
text and per-line audio, then assembles a chaptered M4B with cover art.

## Status

All six pipeline stages are implemented and tested (410 offline tests), and the full path has
been verified end-to-end on real hardware — a real Chatterbox render and a real `ffmpeg`/`mutagen`
assembly produce a playable, chaptered M4B with per-speaker voices and embedded cover art.

The pipeline is currently **headless**. The desktop UI (voice-clip assignment, per-line audio
audition, running stages off the Qt thread) is the next milestone; a minimal PySide6 shell exists
but isn't wired up yet. For now the pipeline is driven programmatically — see
[`scripts/smoke_test.py`](scripts/smoke_test.py) for a runnable example.

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
  assigned voice) plus the operations a UI will drive (confirm attribution, edit text, register and
  assign voice clips).
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

## Running the tests

From the repo root with the venv active:

```sh
pytest                    # 410 tests, fully offline (TTS/LLM/ffmpeg are all mocked)

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
└── ui/            # PySide6 shell (work in progress)
```

Per-stage design docs live under [`docs/plans/`](docs/plans/).

## License

TBD.

# Cast-trophizer

Convert ebooks into **full-cast audiobooks** using [Chatterbox](https://github.com/resemble-ai/chatterbox)
TTS. You supply voice clips that Chatterbox clones to voice individual speakers; the app figures
out which lines belong to the narrator vs. specific characters, lets you review and correct text
and per-line audio, then assembles a chaptered M4B with cover art.

## Status

Early skeleton. The pipeline framework, domain model, workspace store, provider interfaces, and a
minimal PySide6 UI shell are in place; feature bodies are stubs (`raise NotImplementedError`).

## Quickstart (development)

```sh
python -m venv .venv
# activate the venv, then:
pip install -e ".[dev]"
python -c "import casttrophizer; print(casttrophizer.__version__)"

# run the tooling gate
ruff check
black --check .
mypy src
pytest
```

The GUI entry point is `casttrophizer` (also `python -m casttrophizer.ui.app`).

## Prerequisites

- **Python 3.11+**
- **ffmpeg** — required for audiobook assembly. It is an external binary, *not* a pip dependency.
  Install it via your OS package manager (e.g. `winget install ffmpeg`, `brew install ffmpeg`,
  `apt install ffmpeg`) and make sure it is on your `PATH`. `AssembleStage` shells out to it.
- **TTS (optional):** Chatterbox pulls heavy torch dependencies and is gated behind the `tts`
  optional extra: `pip install -e ".[dev,tts]"`. Without it, the app imports and runs, but
  synthesis raises until a TTS provider is available.

## Architecture

The pipeline runs in separable, resumable stages:

```
parse -> correct text -> segment & attribute -> review -> synthesize -> assemble
```

State persists to a per-project workspace directory so you can stop and resume. LLM and TTS
providers live behind interfaces (Claude / LM Studio / Chatterbox) so they can be swapped without
touching the UI or pipeline. Qt is confined to the `ui/` package; everything else is Qt-free and
headless-testable.

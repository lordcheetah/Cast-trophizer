# Cast-trophizer

Convert ebooks into **full-cast audiobooks** using [Chatterbox](https://github.com/resemble-ai/chatterbox) TTS. The user supplies voice clips that Chatterbox clones to voice individual speakers; the app figures out which lines belong to the narrator vs. specific characters, lets the user review/correct text and per-line audio, then assembles a chaptered M4B with cover art.

## Goals (in priority order)

1. **Correct attribution** — every line is tagged narrator or a named speaker, with low-confidence cases surfaced for the user.
2. **Editable text** — OCR errors / typos are auto-fixed where safe, and the rest surfaced for the user to fix or ignore before audio is generated.
3. **Per-line review** — the user can listen to each generated line, approve it, or regenerate before committing to the full render.
4. **Polished output** — full-cast M4B with chapter markers and a cover image.
5. **Automate as much as possible**, surface the rest in a friendly UI.

## Tech stack (decided)

- **Language:** Python 3.11+
- **TTS:** Chatterbox (voice cloning from user-provided reference clips)
- **GUI:** PySide6 (Qt for Python) — desktop, single-user, handles large local audio/ebook files
- **Ebook parsing:** `ebooklib` + `BeautifulSoup` for EPUB; design the parser behind an interface so other formats can be added
- **Speaker attribution:** pluggable LLM provider — **LLM proposes, user confirms**. Low-confidence attributions must be flagged, never silently committed.
  - **Primary provider: Claude API** (Anthropic SDK).
  - **Backup provider: LM Studio** (local, OpenAI-compatible endpoint). Note: a local model like Qwen 2.5 32B Instruct makes obvious attribution mistakes, so the backup is a fallback, not the default — keep the review/confirm UX strong enough that a weaker model is still usable.
- **Text correction:** automated pass (spellcheck + OCR-artifact heuristics) that only auto-applies high-confidence fixes; everything else is surfaced as a suggestion the user accepts/rejects.
- **TTS render unit: per-segment.** Each line is split into attributable segments (narration vs. inline quotes); each segment is rendered with its own voice and the per-segment audio is keyed/cached and stitched back in order during assembly. This is what enables full-cast voicing of mixed paragraphs.
- **State serialization: stdlib dataclasses + manual `to_dict`/`from_dict`** with a `schema_version` (no pydantic). Zero extra runtime deps, easy to mock; a future switch to pydantic stays isolated to `domain/`.
- **Audiobook assembly:** `ffmpeg` + `mutagen` → M4B with chapters + embedded cover. `ffmpeg` is an external binary prerequisite (documented in README, not a pip dep).
- **Audio playback (review UI):** Qt multimedia.

## Architecture principles

- **Pipeline stages are separable and resumable:** `parse → correct text → segment & attribute → review → synthesize → assemble`. A project on disk persists state between stages so the user can stop and resume.
- **Nothing irreversible is automatic.** Auto-fixes are high-confidence only; attribution and text edits are always reviewable.
- **Full render is expensive** (TTS is slow). Per-line audio is cached and only regenerated when the line's text/voice/params change.
- **Keep the LLM and TTS providers behind interfaces** so they can be swapped (local vs. API) without touching the UI or pipeline.
- **Treat the user's source ebooks and voice clips as read-only inputs.** Write all derived artifacts into a per-project workspace directory.

## Conventions

- Format with `ruff` / `black`; type hints everywhere; `mypy`-clean where practical.
- Tests with `pytest`. TTS and LLM calls are mocked in unit tests — never hit a real model in CI.
- Long-running work (parsing, attribution, synthesis) runs off the Qt main thread (QThread / worker signals) so the UI never freezes.

## Subagents

This repo defines four project subagents in `.claude/agents/`: **planner**, **coder**, **reviewer**, **tester**. They are GSD-aware — the global GSD skills (`/gsd-*`) are available for heavier planning/review workflows when a task warrants it.

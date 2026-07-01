#!/usr/bin/env python
"""End-to-end smoke test for the two real-hardware paths: Chatterbox TTS + ffmpeg assembly.

The unit tests mock Chatterbox and ffmpeg, so a green ``pytest`` does NOT exercise
``ChatterboxProvider.synthesize`` or ``M4BAssembler.assemble`` — the two spots marked
``# VERIFY:`` in the source. This script drives both for real on a machine that has the
``tts`` extra installed (``pip install -e ".[dev,tts]"``) and ``ffmpeg`` on PATH.

It skips parse/correct/attribute/review entirely: it hand-authors a tiny, already-attributed
project (narrator + optional characters, voices pre-assigned), then runs only the
SynthesizeStage and AssembleStage. So it needs NO ebook and NO Claude API key — just your
reference voice clips.

Usage (from the repo root, venv active)::

    python scripts/smoke_test.py --narrator path/to/narrator.wav
    python scripts/smoke_test.py --narrator narr.wav --character Alice=alice.wav
    python scripts/smoke_test.py --narrator narr.wav --cover cover.jpg --workdir .smoke-out

Options:
    --narrator PATH         Reference clip for the narrator voice (required).
    --character NAME=PATH    A character voice (repeatable). Each adds a spoken quote line.
    --cover PATH            Optional cover image embedded in the M4B.
    --workdir DIR           Workspace dir for the run (default: a fresh temp dir).
    --exaggeration FLOAT    Chatterbox param (optional; folded into tts_params + cache key).
    --cfg-weight FLOAT      Chatterbox param (optional).
    --seed INT              Chatterbox seed for determinism (optional).

The first run downloads the Chatterbox model, which can take a while and needs network +
disk. On success it prints the path to the assembled ``.m4b`` — play it and confirm the
voices and chapter markers are right.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    VoiceClip,
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.assemble import AssembleStage
from casttrophizer.pipeline.stages.synthesize import SynthesizeStage
from casttrophizer.providers.tts.chatterbox import ChatterboxProvider
from casttrophizer.workspace.store import WorkspaceStore


class PrintReporter:
    """A minimal ProgressReporter that prints to the console; never requests a stop."""

    def __init__(self) -> None:
        self._total = 0
        self._done = 0

    def set_total(self, total: int) -> None:
        self._total = total
        self._done = 0

    def advance(self, n: int = 1, *, message: str | None = None) -> None:
        self._done += n
        suffix = f" — {message}" if message else ""
        print(f"    [{self._done}/{self._total}]{suffix}")

    def message(self, text: str) -> None:
        print(f"    · {text}")

    def should_stop(self) -> bool:
        return False


def _renderable_segment(text: str, speaker: Speaker) -> Segment:
    """A ready-to-synthesize segment attributed to ``speaker`` (approved, no audio yet)."""
    return Segment(
        id=new_id("seg"),
        text=text,
        speaker_id=speaker.id,
        role=speaker.role,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )


def build_project(
    workdir: Path,
    narrator_clip: Path,
    characters: list[tuple[str, Path]],
    cover: Path | None,
    tts_params: dict[str, object],
) -> Project:
    """Hand-author a tiny, fully-attributed, voices-assigned project (no ebook, no LLM)."""
    voice_clips: list[VoiceClip] = []
    speakers: list[Speaker] = []

    def add_voice(label: str, clip: Path) -> VoiceClip:
        vc = VoiceClip(id=new_id("voice"), source_path=str(clip.resolve()), label=label)
        voice_clips.append(vc)
        return vc

    narrator = Speaker(
        id=new_id("spk"),
        name="narrator",
        role=SpeakerRole.NARRATOR,
        voice_clip_id=add_voice("narrator", narrator_clip).id,
    )
    speakers.append(narrator)

    char_speakers: list[Speaker] = []
    for name, clip in characters:
        spk = Speaker(
            id=new_id("spk"),
            name=name,
            role=SpeakerRole.CHARACTER,
            voice_clip_id=add_voice(name, clip).id,
        )
        speakers.append(spk)
        char_speakers.append(spk)

    # Two short chapters. Each opens with a narrated heading line, then narration, then one
    # quote line per provided character — enough to exercise full-cast voicing + 2 chapter marks.
    chapters: list[Chapter] = []
    for ch_i, title in enumerate(("Chapter One", "Chapter Two"), start=1):
        chapter_id = new_id("ch")
        line_segments: list[list[Segment]] = [
            [_renderable_segment(title, narrator)],
            [_renderable_segment(f"This is narration in {title.lower()}.", narrator)],
        ]
        for spk in char_speakers:
            line_segments.append(
                [_renderable_segment(f"Hello, I am {spk.name}, speaking aloud.", spk)]
            )
        lines = [
            Line(
                id=new_id("ln"),
                chapter_id=chapter_id,
                order=order,
                text=" ".join(s.text for s in segs),
                segments=segs,
            )
            for order, segs in enumerate(line_segments, start=1)
        ]
        chapters.append(Chapter(id=chapter_id, order=ch_i, title=title, lines=lines))

    book = Book(
        title="Smoke Test Audiobook",
        author="Cast-trophizer",
        source_ebook_path="(synthetic smoke-test book — no source ebook)",
        cover_image_path=str(cover.resolve()) if cover else None,
        chapters=chapters,
    )

    return Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="smoke-test",
        workspace_dir=str(workdir),
        book=book,
        speakers=speakers,
        voice_clips=voice_clips,
        tts_params=tts_params,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--narrator", required=True, type=Path, help="reference WAV for the narrator")
    p.add_argument(
        "--character",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="a character voice, e.g. --character Alice=alice.wav (repeatable)",
    )
    p.add_argument("--cover", type=Path, default=None, help="optional cover image for the M4B")
    p.add_argument("--workdir", type=Path, default=None, help="workspace dir (default: temp dir)")
    p.add_argument("--exaggeration", type=float, default=None)
    p.add_argument("--cfg-weight", type=float, default=None)
    p.add_argument("--seed", type=int, default=None)
    return p.parse_args(argv)


def _resolve_characters(raw: list[str]) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for item in raw:
        if "=" not in item:
            sys.exit(f"error: --character must be NAME=PATH, got {item!r}")
        name, _, path = item.partition("=")
        clip = Path(path)
        if not clip.is_file():
            sys.exit(f"error: character '{name}' voice clip not found: {clip}")
        out.append((name.strip(), clip))
    return out


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if not args.narrator.is_file():
        sys.exit(f"error: narrator clip not found: {args.narrator}")
    if args.cover is not None and not args.cover.is_file():
        sys.exit(f"error: cover image not found: {args.cover}")
    characters = _resolve_characters(args.character)

    tts_params: dict[str, object] = {}
    if args.exaggeration is not None:
        tts_params["exaggeration"] = args.exaggeration
    if args.cfg_weight is not None:
        tts_params["cfg_weight"] = args.cfg_weight
    if args.seed is not None:
        tts_params["seed"] = args.seed

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="casttrophizer-smoke-"))
    workdir.mkdir(parents=True, exist_ok=True)

    print("Cast-trophizer smoke test")
    print(f"  workspace : {workdir}")
    print(f"  narrator  : {args.narrator}")
    for name, clip in characters:
        print(f"  character : {name} -> {clip}")
    if args.cover:
        print(f"  cover     : {args.cover}")
    print(f"  tts_params: {tts_params or '(defaults)'}")
    print()

    store = WorkspaceStore.for_dir(workdir)
    project = build_project(workdir, args.narrator, characters, args.cover, tts_params)
    store.save(project)
    n_segments = sum(len(ln.segments) for ch in project.book.chapters for ln in ch.lines)
    print(f"Built a {len(project.book.chapters)}-chapter project with {n_segments} segments.\n")

    # ---- Stage 1: SYNTHESIZE (real Chatterbox — exercises providers/tts/chatterbox.py) ----
    print("== SYNTHESIZE (real Chatterbox) ==")
    print("  (first run downloads the model — this can take a few minutes)")
    tts = ChatterboxProvider()
    if not tts.is_available():
        sys.exit(
            "error: Chatterbox/torch not importable — install the 'tts' extra: "
            'pip install -e ".[dev,tts]"'
        )
    t0 = time.perf_counter()
    result = SynthesizeStage().run(
        project, StageContext(store=store, progress=PrintReporter(), tts=tts)
    )
    dt = time.perf_counter() - t0
    print(f"  -> {result.status.value.upper()}: {result.message}  ({dt:.1f}s)")
    if result.status is not ReviewStatus.COMPLETED:
        sys.exit(
            "SYNTHESIZE failed — the Chatterbox `# VERIFY:` path needs attention "
            "(check the generate() signature / model.sr in providers/tts/chatterbox.py)."
        )
    wavs = list((store.layout.audio_dir).glob("*.wav"))
    print(f"  wrote {len(wavs)} cached segment WAV(s) under {store.layout.audio_dir}\n")

    # ---- Stage 2: ASSEMBLE (real ffmpeg + mutagen — exercises audio/assembler.py) ----
    print("== ASSEMBLE (real ffmpeg + mutagen) ==")
    project = store.load()  # reload persisted state, exactly like a resumed pipeline
    t0 = time.perf_counter()
    result = AssembleStage().run(project, StageContext(store=store, progress=PrintReporter()))
    dt = time.perf_counter() - t0
    print(f"  -> {result.status.value.upper()}: {result.message}  ({dt:.1f}s)")
    if result.status is not ReviewStatus.COMPLETED:
        sys.exit(
            "ASSEMBLE failed — the ffmpeg/mutagen `# VERIFY:` path needs attention "
            "(check the ffmpeg args / ffmetadata chapters / mutagen cover in audio/assembler.py)."
        )

    m4bs = list((store.layout.output_dir).glob("*.m4b"))
    print()
    print("SMOKE TEST PASSED ✅")
    for m4b in m4bs:
        size_mb = m4b.stat().st_size / (1024 * 1024)
        print(f"  output: {m4b}  ({size_mb:.2f} MB)")
    print(
        "\nPlay the .m4b and confirm: correct per-speaker voices, chapter markers at the right "
        "spots, title/author tags, and the cover (if you passed one)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

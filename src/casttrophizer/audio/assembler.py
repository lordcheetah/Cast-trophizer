"""Final audiobook assembly: stitch per-segment audio into a chaptered M4B.

``ffmpeg`` is an external **binary** prerequisite (documented in the README, not a pip
dep); the assembler shells out to it. ``mutagen`` (a pip dep) is imported lazily inside
methods to tag chapters + embed the cover. Feature bodies are stubs
(``raise NotImplementedError``) in this skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ChapterMarker", "M4BAssembler"]


@dataclass
class ChapterMarker:
    """A chapter boundary in the assembled audiobook (times in seconds)."""

    title: str
    start_s: float
    end_s: float


@dataclass
class AssemblyRequest:
    """Inputs needed to assemble the final M4B.

    ``segment_audio_paths`` are the per-segment ``.wav`` files in playback order; the
    assembler concatenates them and writes chapter markers + cover into ``out_path``.
    """

    segment_audio_paths: list[Path]
    chapters: list[ChapterMarker]
    out_path: Path
    title: str
    author: str
    cover_image_path: Path | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class M4BAssembler:
    """Assembles per-segment audio into an M4B with chapter markers and cover art."""

    def __init__(self, ffmpeg_path: str = "ffmpeg") -> None:
        self._ffmpeg_path = ffmpeg_path

    def is_available(self) -> bool:
        """Report whether the ffmpeg binary is resolvable on PATH."""
        import shutil

        return shutil.which(self._ffmpeg_path) is not None

    def assemble(self, request: AssemblyRequest) -> Path:
        # Shells out to ffmpeg to concatenate/encode; uses mutagen (lazy import) to write
        # chapter markers + embed the cover.
        raise NotImplementedError("M4BAssembler.assemble is not yet implemented")

"""Final audiobook assembly: stitch per-segment audio into a chaptered M4B.

``ffmpeg`` is an external **binary** prerequisite (documented in the README, not a pip
dep); the assembler shells out to it. ``mutagen`` (a pip dep) is imported lazily inside
:meth:`M4BAssembler.assemble` to embed the cover + write title/author tags.

Every real ffmpeg command string and mutagen call is marked ``# VERIFY:`` — the coder
cannot run ffmpeg or web-verify flags in CI, so the exact concat-demuxer invocation, the
AAC/M4B codec/container flags, the ffmetadata ``[CHAPTER]`` syntax, and the mutagen MP4
cover atom + tag keys must be confirmed at runtime on a machine with ffmpeg. All tests use a
fake assembler or a mocked subprocess/mutagen boundary — real ffmpeg/mutagen never run in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from casttrophizer.errors import AssemblyError

__all__ = ["ChapterMarker", "AssemblyRequest", "M4BAssembler"]

#: AAC bitrate for the M4B encode. Fixed for v1 (not surfaced in config/UI yet).
_AAC_BITRATE = "128k"


@dataclass
class ChapterMarker:
    """A chapter boundary in the assembled audiobook (times in seconds)."""

    title: str
    start_s: float
    end_s: float


@dataclass
class AssemblyRequest:
    """Inputs needed to assemble the final M4B.

    ``segment_audio_paths`` are the per-segment ``.wav`` files (plus any silence-gap WAVs the
    orchestration inserted) in playback order; the assembler concatenates them and writes
    chapter markers + cover into ``out_path``.
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
        return shutil.which(self._ffmpeg_path) is not None

    def assemble(self, request: AssemblyRequest) -> Path:
        """Concatenate the WAVs into a chaptered, tagged M4B at ``request.out_path``.

        Steps: (1) write a concat-demuxer file list + an ffmetadata chapter file into a temp
        build dir; (2) shell ffmpeg once to concat + AAC-encode + apply the chapters into a
        temp ``.m4b``; (3) embed the cover + title/author tags via lazy ``mutagen``;
        (4) atomically move the temp file into ``out_path``. The temp build dir is cleaned up
        on both success and failure; a failure never leaves a half-written file at ``out_path``.

        Raises :class:`AssemblyError` on any ffmpeg non-zero exit or mutagen failure. A
        missing/bad/unreadable cover is skipped with a warning (the M4B is still produced).
        """
        if not request.segment_audio_paths:
            raise AssemblyError("no audio segments to assemble")

        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        build_dir = Path(tempfile.mkdtemp(prefix=".assemble-", dir=str(request.out_path.parent)))
        try:
            concat_list = build_dir / "concat_list.txt"
            _write_concat_list(concat_list, request.segment_audio_paths)

            metadata_file = build_dir / "chapters.ffmeta"
            _write_ffmetadata(metadata_file, request)

            tmp_out = build_dir / "out.m4b"
            self._run_ffmpeg(concat_list, metadata_file, tmp_out)
            _embed_cover_and_tags(tmp_out, request)

            # Atomic move into output/ only after a fully successful encode + tag (mirrors the
            # store's atomic-write ethos: no half-written .m4b visible at out_path).
            os.replace(str(tmp_out), str(request.out_path))
            return request.out_path
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

    def _run_ffmpeg(self, concat_list: Path, metadata_file: Path, out_path: Path) -> None:
        """Run the single concat -> AAC/M4B + chapters ffmpeg invocation."""
        # VERIFY: the exact ffmpeg concat-demuxer + AAC/M4B + ffmetadata-chapters invocation.
        #   Approx (confirm flags/order at runtime on a machine with ffmpeg):
        #     ffmpeg -y -f concat -safe 0 -i concat_list.txt \
        #            -i chapters.ffmeta -map_metadata 1 -map_chapters 1 \
        #            -c:a aac -b:a 128k -movflags +faststart out.m4b
        #   - concat *demuxer* (-f concat), NOT the concat filter and NOT a huge -i arg list, so
        #     a book with thousands of segments stays a small file list (no OS arg-length limit).
        #   - inputs are same-sample-rate PCM WAV (Chatterbox + our silence WAVs match the
        #     segment format); VERIFY whether an explicit -ar/-ac normalize pass is ever needed.
        #   - -i <ffmeta> as the SECOND input with -map_metadata 1 -map_chapters 1 applies the
        #     [CHAPTER] blocks + top-level title/artist tags. VERIFY this is how ffmpeg ingests
        #     an ffmetadata chapter file (vs. -map_metadata on the concat input).
        #   - .m4b is an MP4/AAC container; VERIFY the codec (aac) + bitrate (128k) the user wants.
        cmd = [
            self._ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-i",
            str(metadata_file),
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            _AAC_BITRATE,
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:  # ffmpeg vanished between is_available() and here
            raise AssemblyError(f"failed to launch ffmpeg: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-20:]
            raise AssemblyError(
                "ffmpeg failed to assemble the M4B "
                f"(exit {proc.returncode}):\n" + "\n".join(tail)
            )


def _write_concat_list(list_path: Path, audio_paths: list[Path]) -> None:
    """Write the ffmpeg concat-demuxer file list (one ``file '<abs path>'`` per WAV)."""
    # VERIFY: the concat-demuxer line format + path escaping, especially on Windows with
    #   backslashes/quotes/unicode in a path. The concat demuxer wants:  file '<path>'
    #   with embedded single-quotes escaped as  '\''  and (on Windows) forward slashes are
    #   generally accepted by ffmpeg. Confirm the exact escaping rule against ffmpeg docs.
    lines: list[str] = []
    for path in audio_paths:
        # ffmpeg accepts forward slashes on Windows; normalize so a Windows backslash is not
        # mistaken for an escape inside the single-quoted concat entry.
        text = path.as_posix().replace("'", "'\\''")
        lines.append(f"file '{text}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_ffmetadata(metadata_file: Path, request: AssemblyRequest) -> None:
    """Write an ffmetadata file with top-level tags + one ``[CHAPTER]`` block per marker."""
    # VERIFY: the ffmetadata file format — the ';FFMETADATA1' header, key=value escaping, and
    #   the [CHAPTER] block with TIMEBASE / START / END (units below are milliseconds) + title.
    #   Confirm TIMEBASE=1/1000 with START/END in ms is honored, and that titles are escaped
    #   per ffmetadata rules ('=', ';', '#', '\\', newline -> backslash-escaped).
    # Imported here (not at module top) to avoid an import cycle: assemble.py imports the DTOs
    # from this module, and this module borrows the escape helper from assemble.py.
    from casttrophizer.audio.assemble import escape_ffmetadata  # noqa: PLC0415

    parts: list[str] = [";FFMETADATA1"]
    parts.append(f"title={escape_ffmetadata(request.title)}")
    parts.append(f"artist={escape_ffmetadata(request.author)}")
    parts.append(f"album={escape_ffmetadata(request.title)}")
    for marker in request.chapters:
        start_ms = int(round(marker.start_s * 1000))
        end_ms = int(round(marker.end_s * 1000))
        parts.append("")
        parts.append("[CHAPTER]")
        parts.append("TIMEBASE=1/1000")
        parts.append(f"START={start_ms}")
        parts.append(f"END={end_ms}")
        parts.append(f"title={escape_ffmetadata(marker.title)}")
    metadata_file.write_text("\n".join(parts) + "\n", encoding="utf-8")


def _embed_cover_and_tags(m4b_path: Path, request: AssemblyRequest) -> None:
    """Embed the cover (if set + readable) and write title/author tags via lazy ``mutagen``.

    A missing/unset cover writes tags only. A bad/unreadable cover is skipped with a warning
    (the M4B is still produced) — a nice-to-have image must not fail the whole render.
    """
    # Lazy import: the lazy-import test forbids mutagen at module top. Typed ``Any`` (mirroring
    # the chatterbox provider's ``self._model: Any``) so the untyped MP4 dict/save calls do not
    # trip mypy --strict; the real API is ``# VERIFY:``-marked and mocked in CI.
    import mutagen.mp4 as _mp4  # noqa: PLC0415

    mp4_cls: Any = _mp4.MP4

    # VERIFY: the mutagen MP4 tag atoms + save() behavior on an ffmpeg-produced .m4b.
    #   '\xa9nam' = title, '\xa9ART' = artist/author, '\xa9alb' = album. Confirm these atom
    #   keys and that MP4(str(path)).save() round-trips without corrupting ffmpeg's chapters.
    try:
        mp4: Any = mp4_cls(str(m4b_path))
    except Exception as exc:  # mutagen raises its own error types; wrap uniformly
        raise AssemblyError(f"failed to open {m4b_path} for tagging: {exc}") from exc

    mp4["\xa9nam"] = [request.title]
    mp4["\xa9ART"] = [request.author]
    mp4["\xa9alb"] = [request.title]

    cover = request.cover_image_path
    if cover is not None and cover.is_file():
        cover_atom = _read_cover(cover)
        if cover_atom is not None:
            # VERIFY: the mutagen cover atom key 'covr' + MP4Cover(data, imageformat=...).
            mp4["covr"] = [cover_atom]

    try:
        mp4.save()
    except Exception as exc:
        raise AssemblyError(f"failed to write tags to {m4b_path}: {exc}") from exc


def _read_cover(cover: Path) -> Any | None:
    """Read a cover image into an ``MP4Cover``; return None (with a warning) on any problem."""
    import logging  # noqa: PLC0415

    import mutagen.mp4 as _mp4  # noqa: PLC0415

    mp4_cover_cls: Any = _mp4.MP4Cover

    logger = logging.getLogger(__name__)
    suffix = cover.suffix.lower()
    # VERIFY: cover format detection. Approx: PNG for .png, JPEG for .jpg/.jpeg; other formats
    #   are skipped. Confirm MP4Cover.FORMAT_PNG / FORMAT_JPEG constants and that these two
    #   cover the formats we accept (a bad/unsupported cover is skipped, not fatal).
    if suffix == ".png":
        image_format = mp4_cover_cls.FORMAT_PNG
    elif suffix in (".jpg", ".jpeg"):
        image_format = mp4_cover_cls.FORMAT_JPEG
    else:
        logger.warning("skipping cover %s: unsupported image format %r", cover, suffix)
        return None
    try:
        data = cover.read_bytes()
    except OSError as exc:
        logger.warning("skipping cover %s: cannot read file (%s)", cover, exc)
        return None
    return mp4_cover_cls(data, imageformat=image_format)

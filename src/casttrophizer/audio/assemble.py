"""Offline assembly orchestration: precheck -> ordered WAVs -> markers -> AssemblyRequest.

Keeps :class:`~casttrophizer.pipeline.stages.assemble.AssembleStage` thin (mirrors how
:mod:`casttrophizer.audio.synthesize` holds the synthesize stage's policy). Everything here
is **pure/offline** — no ffmpeg, no mutagen, no subprocess — so it is fully unit-testable
against the tiny silent-WAV fixtures. The only external-tool work (ffmpeg + mutagen) lives in
:class:`~casttrophizer.audio.assembler.M4BAssembler`, which consumes the
:class:`~casttrophizer.audio.assembler.AssemblyRequest` this module produces.

Three seams the stage drives:

``unrendered_segments(project, cache)``
    The fail-fast precheck. Returns short human identifiers for every **renderable** segment
    (same whitespace-skip rule as synthesize) that is not backed by a COMPLETED/APPROVED
    ``audio_cache_key`` whose WAV exists on disk. A non-empty list means "run synthesize
    first" and the stage returns FAILED before any ffmpeg work.

``wav_duration_s(path)``
    Dependency-free PCM-WAV duration via stdlib :mod:`wave` (frames / framerate). Used to
    compute cumulative chapter start/end times without ffprobe.

``build_assembly_request(project, cache, layout, *, progress, should_stop)``
    Stitches segments in reading order (chapter -> line -> segment list order, whitespace
    skipped identically to synthesize), inserts silence-gap WAVs (§3d padding constants),
    computes one :class:`ChapterMarker` per chapter from real WAV durations + those gaps,
    resolves the cover (embed-if-set), derives the sanitized ``output/<title>.m4b`` path, and
    returns a fully-computed :class:`AssemblyRequest`. Polls ``should_stop`` per chapter and
    returns ``(None, 0)`` when stopped.

CRITICAL: the "renderable" predicate and ``RENDERED_STATUSES`` are **imported** from
:mod:`casttrophizer.audio.synthesize` (not redefined) so "renderable"/"rendered" can never
drift between the synthesize and assemble stages.
"""

from __future__ import annotations

import unicodedata
import wave
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from casttrophizer.audio.assembler import AssemblyRequest, ChapterMarker
from casttrophizer.audio.synthesize import RENDERED_STATUSES
from casttrophizer.domain.models import Project, Segment
from casttrophizer.errors import AssemblyError
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.layout import WorkspaceLayout

if TYPE_CHECKING:
    from casttrophizer.pipeline.progress import ProgressReporter

__all__ = [
    "INTER_SEGMENT_GAP_S",
    "INTER_LINE_GAP_S",
    "INTER_CHAPTER_GAP_S",
    "is_renderable",
    "wav_duration_s",
    "unrendered_segments",
    "sanitize_filename",
    "escape_ffmetadata",
    "build_assembly_request",
]

#: Padding inserted between playback units (seconds). Fixed module constants for v1 — not
#: surfaced in ``AppConfig``/UI. The SAME constants feed both the concat silence WAVs and the
#: chapter-marker timing so the markers cannot drift from the audio.
INTER_SEGMENT_GAP_S = 0.0  # segments within a line are one continuous utterance
INTER_LINE_GAP_S = 0.4  # a brief breath between paragraphs / quote runs
INTER_CHAPTER_GAP_S = 1.0  # a longer beat between chapters

#: Windows/POSIX filesystem-reserved characters stripped from a derived filename.
_RESERVED_FILENAME_CHARS = set('/\\:*?"<>|')

#: Fallback stem when a sanitized title is empty / all-stripped.
_FALLBACK_FILENAME = "audiobook"

#: ffmetadata reserved characters that must be backslash-escaped in a value (§4b).
_FFMETADATA_ESCAPE = ("\\", "=", ";", "#", "\n")


def is_renderable(segment: Segment) -> bool:
    """True iff ``segment`` has audible text — the identical whitespace-skip rule synthesize
    uses (``if not segment.text.strip(): skip``). Whitespace-only segments have no WAV and are
    skipped identically here so the concat / chapter timing cannot desync.
    """
    return bool(segment.text.strip())


def wav_duration_s(path: Path) -> float:
    """Return the duration of a PCM WAV in seconds via stdlib :mod:`wave`.

    ``duration = frames / framerate`` — exact for the PCM WAVs Chatterbox/``FakeTTSProvider``
    write, dependency-free, and offline-testable on the fixture WAVs. A WAV that fails to open
    (corrupt/truncated cache) raises :class:`AssemblyError` — fail loudly rather than guess 0
    and silently desync the timeline.
    """
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
    except (OSError, wave.Error) as exc:
        raise AssemblyError(f"cannot read WAV duration from {path}: {exc}") from exc
    if rate <= 0:
        raise AssemblyError(f"WAV {path} has an invalid framerate ({rate})")
    return frames / rate


def unrendered_segments(project: Project, cache: AudioCache) -> list[str]:
    """Human identifiers for renderable segments lacking a usable rendered WAV (§2 precheck).

    Scans every **renderable** segment (whitespace-only skipped identically to synthesize) and
    collects ``"<chapter title> / seg <id>"`` for any where any of:

    1. ``audio_cache_key is None`` (never rendered), or
    2. ``audio_status not in RENDERED_STATUSES`` (PENDING/FAILED/NEEDS_REVIEW), or
    3. ``not cache.has(audio_cache_key)`` (key stamped but WAV missing on disk).

    An empty list means every renderable segment is backed by an on-disk WAV and assembly can
    proceed. ``RENDERED_STATUSES`` is reused from :mod:`casttrophizer.audio.synthesize` so
    "rendered" means the same thing in both stages.
    """
    missing: list[str] = []
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                if not is_renderable(segment):
                    continue
                if (
                    segment.audio_cache_key is None
                    or segment.audio_status not in RENDERED_STATUSES
                    or not cache.has(segment.audio_cache_key)
                ):
                    missing.append(f"{chapter.title} / seg {segment.id}")
    return missing


def sanitize_filename(title: str) -> str:
    """Derive a filesystem-safe stem from a book title (§7).

    Strips filesystem-reserved chars (``/ \\ : * ? " < > |``) and control chars, collapses
    runs of whitespace to a single space, trims, and falls back to ``"audiobook"`` when the
    result is empty. Unicode *letters* are preserved (an ``ë``/CJK title is a valid filename on
    modern filesystems) — only the reserved set and control chars are removed.
    """
    kept: list[str] = []
    for ch in title:
        if ch in _RESERVED_FILENAME_CHARS:
            continue
        if unicodedata.category(ch).startswith("C"):  # control / format / surrogate chars
            continue
        kept.append(ch)
    collapsed = " ".join("".join(kept).split())
    return collapsed if collapsed else _FALLBACK_FILENAME


def escape_ffmetadata(text: str) -> str:
    """Escape a value for the ffmpeg **ffmetadata** format (§4b).

    ffmetadata requires backslash-escaping ``\\``, ``=``, ``;``, ``#`` and newlines. The
    backslash is escaped first so the escapes added for the other characters are not
    double-escaped. Unicode passes through untouched (ffmetadata is UTF-8).
    """
    out = text
    for ch in _FFMETADATA_ESCAPE:
        out = out.replace(ch, "\\" + ch)
    return out


def build_assembly_request(
    project: Project,
    cache: AudioCache,
    layout: WorkspaceLayout,
    *,
    progress: ProgressReporter,
    should_stop: Callable[[], bool],
) -> tuple[AssemblyRequest | None, int]:
    """Build the fully-computed :class:`AssemblyRequest` from cached per-segment WAVs.

    Iterates chapters/lines/segments in **list order** (authoritative, as synthesize uses;
    NOT re-sorted by ``.order``), skipping whitespace-only segments. For each kept segment it
    appends ``cache.path_for_key(segment.audio_cache_key)`` to the flat playback list and folds
    its duration into a running cursor; it inserts silence-gap WAVs (§3d) between segments,
    lines and chapters, and folds the SAME gap durations into the cursor so the chapter markers
    match the concatenated audio. One :class:`ChapterMarker` is emitted per chapter — a chapter
    with zero renderable segments yields a **zero-length** marker at the current cursor (1:1
    with the book).

    Polls ``should_stop`` once per chapter; on stop returns ``(None, 0)`` with nothing written.
    Returns ``(request, chapter_count)`` otherwise. ``request.segment_audio_paths`` is empty iff
    the whole book has no renderable segments — the stage treats that as FAILED.

    The precheck (:func:`unrendered_segments`) is expected to have passed already, so every
    kept segment resolves to an on-disk WAV; :func:`wav_duration_s` still raises
    :class:`AssemblyError` on a corrupt WAV as a defensive guard.
    """
    chapters = project.book.chapters
    progress.set_total(len(chapters))

    silence_dir = _build_dir(layout)
    audio_paths: list[Path] = []
    markers: list[ChapterMarker] = []
    cursor_s = 0.0
    # The last segment appended to the concat, used as the sample-format reference for an
    # inter-chapter silence gap (there is no "current segment" in scope at the chapter boundary).
    last_reference: Segment | None = None

    for ch_index, chapter in enumerate(chapters):
        if should_stop():
            return None, 0

        chapter_start_s = cursor_s
        first_line_in_chapter = True

        for line in chapter.lines:
            kept = [seg for seg in line.segments if is_renderable(seg)]
            if not kept:
                continue

            if not first_line_in_chapter and INTER_LINE_GAP_S > 0:
                gap = _silence_wav(kept[0], cache, silence_dir, INTER_LINE_GAP_S)
                audio_paths.append(gap)
                cursor_s += INTER_LINE_GAP_S
            first_line_in_chapter = False

            for seg_index, segment in enumerate(kept):
                if seg_index > 0 and INTER_SEGMENT_GAP_S > 0:
                    gap = _silence_wav(segment, cache, silence_dir, INTER_SEGMENT_GAP_S)
                    audio_paths.append(gap)
                    cursor_s += INTER_SEGMENT_GAP_S

                assert segment.audio_cache_key is not None  # guaranteed by the precheck
                wav_path = cache.path_for_key(segment.audio_cache_key)
                audio_paths.append(wav_path)
                cursor_s += wav_duration_s(wav_path)
                last_reference = segment

        markers.append(ChapterMarker(title=chapter.title, start_s=chapter_start_s, end_s=cursor_s))

        # Inter-chapter gap plays *between* chapters: insert real silence into the concat AND
        # advance the cursor by the same amount, so chapter markers stay in sync with the audio.
        # Guarded on last_reference so we only pad when there is preceding audio to pad after
        # (and thus a WAV whose sample format the silence must match).
        if ch_index < len(chapters) - 1 and INTER_CHAPTER_GAP_S > 0 and last_reference is not None:
            gap = _silence_wav(last_reference, cache, silence_dir, INTER_CHAPTER_GAP_S)
            audio_paths.append(gap)
            cursor_s += INTER_CHAPTER_GAP_S

        progress.advance(1, message=chapter.title or None)

    book = project.book
    cover: Path | None = None
    if book.cover_image_path:
        candidate = Path(book.cover_image_path)
        if candidate.is_file():
            cover = candidate  # read-only input; referenced, never copied

    out_path = layout.output_dir / f"{sanitize_filename(book.title)}.m4b"

    request = AssemblyRequest(
        segment_audio_paths=audio_paths,
        chapters=markers,
        out_path=out_path,
        title=book.title,
        author=book.author,
        cover_image_path=cover,
    )
    return request, len(chapters)


def _build_dir(layout: WorkspaceLayout) -> Path:
    """Scratch dir for generated silence-gap WAVs.

    Lives under ``audio/.silence`` (a cross-run cache alongside the per-segment audio), NOT
    under ``output/`` — so ``output/`` holds only the final ``.m4b`` deliverable.
    """
    build = layout.audio_dir / ".silence"
    build.mkdir(parents=True, exist_ok=True)
    return build


def _silence_wav(
    reference: Segment,
    cache: AudioCache,
    silence_dir: Path,
    seconds: float,
) -> Path:
    """Generate (once, cached by duration + audio format) a silent WAV to pad the concat.

    The silence must match the surrounding segments' sample rate / channels / sample width so
    the concat demuxer sees a homogeneous WAV stream (no re-encode / filter graph). The format
    is read from the reference segment's rendered WAV via :mod:`wave`.
    """
    assert reference.audio_cache_key is not None  # guaranteed by the precheck
    ref_path = cache.path_for_key(reference.audio_cache_key)
    try:
        with wave.open(str(ref_path), "rb") as wav:
            channels = wav.getnchannels()
            sampwidth = wav.getsampwidth()
            rate = wav.getframerate()
    except (OSError, wave.Error) as exc:
        raise AssemblyError(f"cannot read WAV format from {ref_path}: {exc}") from exc

    ms = round(seconds * 1000)
    out_path = silence_dir / f"silence_{ms:04d}ms_{rate}hz_{channels}ch_{sampwidth}b.wav"
    if out_path.is_file():
        return out_path

    n_frames = int(round(rate * seconds))
    try:
        with wave.open(str(out_path), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(sampwidth)
            out.setframerate(rate)
            out.writeframes(b"\x00" * (n_frames * channels * sampwidth))
    except (OSError, wave.Error) as exc:
        raise AssemblyError(f"cannot write silence WAV {out_path}: {exc}") from exc
    return out_path

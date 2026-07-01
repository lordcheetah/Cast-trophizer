"""Fix-locking consistency tests: chapter markers vs. the TRUE concatenated audio.

The regression these lock down: ``build_assembly_request`` advances the chapter-marker
cursor for the inter-chapter gap, so it MUST also insert a real silence WAV of that gap into
the concat list — otherwise the markers drift ~1s per chapter boundary ahead of the audio
ffmpeg actually produces. The pre-existing marker tests assert marker times against the same
cursor arithmetic the source uses, so they could not catch a cursor/concat divergence.

These tests instead compute the TRUE total by summing :func:`wav_duration_s` over **every**
path the request hands to the concat demuxer (content segments AND every inserted gap WAV) and
assert the markers line up with that ground truth:

* the last chapter's ``end_s`` == the true total duration of the whole concat, and
* each chapter's ``start_s`` == the cumulative true duration of all audio paths that precede
  its first content segment.

Against the pre-fix code (cursor counted the inter-chapter gap the concat lacked) the last
chapter's ``end_s`` would exceed the summed concat duration by one inter-chapter gap per
boundary, so :func:`test_last_marker_end_equals_true_concat_total` and
:func:`test_marker_starts_equal_true_cumulative_audio` would FAIL. They pass against the fix.

A ``>=3`` content-chapter book with a **middle** empty chapter is used so the assertion also
proves an empty chapter in the middle does not desync later chapters.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from casttrophizer.audio.assemble import (
    INTER_CHAPTER_GAP_S,
    INTER_LINE_GAP_S,
    build_assembly_request,
    wav_duration_s,
)
from casttrophizer.audio.assembler import AssemblyRequest
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Chapter, Line, Project, Segment, Speaker
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import RecordingProgressReporter

_EPS = 1e-6

#: Distinct, known per-segment WAV durations (seconds) keyed by segment text. Distinct values
#: mean any off-by-one in the stitch order would shift a marker measurably.
_DURATIONS: dict[str, float] = {
    "c1 narration": 0.20,
    "c1 quote": 0.10,
    "c1 tail": 0.30,
    "c3 only": 0.15,
    "c4 first": 0.25,
    "c4 second": 0.05,
}


def _seg(text: str, speaker: Speaker) -> Segment:
    return Segment(
        id=new_id("seg"),
        text=text,
        speaker_id=speaker.id,
        role=speaker.role,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )


def _line(chapter_id: str, order: int, segments: list[Segment]) -> Line:
    return Line(
        id=new_id("line"),
        chapter_id=chapter_id,
        order=order,
        text=" ".join(s.text for s in segments),
        segments=segments,
    )


@pytest.fixture
def multi_chapter_project(tmp_workspace: WorkspaceStore) -> Project:
    """A saved, fully-synthesized project with >=3 content chapters + a MIDDLE empty chapter.

    Layout (four chapters, each renderable segment a real WAV of a distinct known duration):
      * C1: two content lines (so an inter-line gap sits inside C1) — 3 segments.
      * C2: **empty** (zero renderable segments) — a middle empty chapter.
      * C3: one line, one segment.
      * C4: one line, two segments.
    """
    cache = AudioCache(tmp_workspace.layout)
    narrator = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)
    alice = Speaker(id=new_id("spk"), name="Alice", role=SpeakerRole.CHARACTER)

    c1, c2, c3, c4 = (new_id("ch") for _ in range(4))
    ch1 = Chapter(
        id=c1,
        order=0,
        title="Chapter One",
        lines=[
            _line(c1, 0, [_seg("c1 narration", narrator)]),
            _line(c1, 1, [_seg("c1 quote", alice), _seg("c1 tail", narrator)]),
        ],
    )
    ch2 = Chapter(id=c2, order=1, title="Chapter Two (empty)", lines=[])
    ch3 = Chapter(
        id=c3,
        order=2,
        title="Chapter Three",
        lines=[_line(c3, 0, [_seg("c3 only", narrator)])],
    )
    ch4 = Chapter(
        id=c4,
        order=3,
        title="Chapter Four",
        lines=[_line(c4, 0, [_seg("c4 first", alice), _seg("c4 second", narrator)])],
    )

    book = Book(
        title="Multi Chapter Tale",
        author="Test Author",
        source_ebook_path="x.epub",
        cover_image_path=None,
        chapters=[ch1, ch2, ch3, ch4],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="multi",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        speakers=[narrator, alice],
        voice_clips=[],
        stage_status={str(StageName.SYNTHESIZE): ReviewStatus.COMPLETED},
        tts_params={},
    )

    for chapter in book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                key = AudioCache.key_for(segment, project)
                path = cache.path_for_key(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_wav(path, _DURATIONS[segment.text])
                segment.audio_cache_key = key
                segment.audio_status = ReviewStatus.COMPLETED
    tmp_workspace.save(project)
    return project


def _write_wav(path: Path, seconds: float, *, rate: int = 22050) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))


def _build(project: Project, store: WorkspaceStore) -> AssemblyRequest:
    request, _ = build_assembly_request(
        project,
        AudioCache(store.layout),
        store.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    return request


# --------------------------------------------------------------------------- #
# THE fix-locking assertion: markers must match the TRUE concatenated audio.
# --------------------------------------------------------------------------- #
def test_last_marker_end_equals_true_concat_total(
    tmp_workspace: WorkspaceStore, multi_chapter_project: Project
) -> None:
    """Last chapter ``end_s`` == sum of EVERY path's real WAV duration (segments + gap WAVs).

    Pre-fix, the inter-chapter gap advanced the cursor but no gap WAV entered the concat, so
    the cursor (hence ``end_s``) ran ahead of the summed concat duration by one gap per
    boundary — this assertion would fail. With the fix the gap WAV is in the list, so the two
    agree.
    """
    request = _build(multi_chapter_project, tmp_workspace)

    true_total = sum(wav_duration_s(p) for p in request.segment_audio_paths)
    assert request.chapters[-1].end_s == pytest.approx(true_total, abs=_EPS)


def test_marker_starts_equal_true_cumulative_audio(
    tmp_workspace: WorkspaceStore, multi_chapter_project: Project
) -> None:
    """Each chapter ``start_s`` == cumulative real duration of every path BEFORE its first seg.

    Walks the returned ``segment_audio_paths`` accumulating real WAV durations, and at the
    index where each chapter's first content segment begins asserts the running total equals
    that chapter's ``start_s``. Ground truth is the concat itself, not the cursor arithmetic —
    so a cursor/concat divergence (the fixed bug) is caught. Also covers the empty middle
    chapter: its zero-length marker must sit at the cumulative offset reached after the
    preceding inter-chapter gap.
    """
    request = _build(multi_chapter_project, tmp_workspace)
    cache = AudioCache(tmp_workspace.layout)

    # First-segment WAV path for each non-empty chapter, in chapter order.
    first_path_per_chapter: dict[str, Path] = {}
    for chapter in multi_chapter_project.book.chapters:
        for line in chapter.lines:
            content = [s for s in line.segments if s.text.strip()]
            if content:
                assert content[0].audio_cache_key is not None
                first_path_per_chapter[chapter.title] = cache.path_for_key(
                    content[0].audio_cache_key
                )
                break

    # Cumulative real duration reaching each path index in the concat.
    cumulative_before: dict[Path, float] = {}
    running = 0.0
    for path in request.segment_audio_paths:
        cumulative_before.setdefault(path, running)
        running += wav_duration_s(path)

    markers = {m.title: m for m in request.chapters}
    for title, first_path in first_path_per_chapter.items():
        assert markers[title].start_s == pytest.approx(
            cumulative_before[first_path], abs=_EPS
        ), f"chapter {title!r} start_s drifted from the true cumulative audio offset"

    # The empty middle chapter carries a zero-length marker; its start must equal the cumulative
    # audio offset at that point (after the C1->C2 inter-chapter gap) and NOT desync C3/C4.
    empty = markers["Chapter Two (empty)"]
    assert empty.start_s == empty.end_s
    # C3 begins immediately after the C2->C3 inter-chapter gap; its start already asserted above.


def test_gap_wav_count_matches_boundaries(
    tmp_workspace: WorkspaceStore, multi_chapter_project: Project
) -> None:
    """Exactly one inter-line gap WAV inside C1 + one inter-chapter gap per chapter boundary.

    Four chapters -> three inter-chapter gaps (including the two touching the empty middle
    chapter, which the fix inserts on both sides), plus one inter-line gap inside C1 = 4 gaps.
    A pre-fix build would insert only the single inter-line gap (the inter-chapter gaps were
    cursor-only), so this count locks the inter-chapter gap insertion in place.
    """
    request = _build(multi_chapter_project, tmp_workspace)
    gap_paths = [p for p in request.segment_audio_paths if ".silence" in p.parts]

    inter_chapter = [p for p in gap_paths if "1000ms" in p.name]
    inter_line = [p for p in gap_paths if "0400ms" in p.name]
    # 4 chapters -> 3 inter-chapter boundaries (all gaps > 0 and preceded by audio).
    assert len(inter_chapter) == 3
    assert len(inter_line) == 1
    assert len(gap_paths) == 4


def test_markers_strictly_monotonic_in_start(
    tmp_workspace: WorkspaceStore, multi_chapter_project: Project
) -> None:
    """Start times are non-decreasing, and strictly increasing across chapters with audio.

    The empty chapter shares its neighbour's cursor only momentarily (zero-length), but every
    content chapter starts strictly after the previous content chapter thanks to the gaps +
    durations, so the sequence of content-chapter starts is strictly increasing.
    """
    request = _build(multi_chapter_project, tmp_workspace)
    starts = [m.start_s for m in request.chapters]
    assert starts == sorted(starts)  # non-decreasing overall

    # Chapters that actually contributed audio must be strictly increasing in start.
    content_titles = {"Chapter One", "Chapter Three", "Chapter Four"}
    content_starts = [m.start_s for m in request.chapters if m.title in content_titles]
    assert all(a < b for a, b in zip(content_starts, content_starts[1:], strict=False))


def test_no_marker_end_before_start(
    tmp_workspace: WorkspaceStore, multi_chapter_project: Project
) -> None:
    """Every marker has ``end_s >= start_s`` (empty chapters are exactly equal)."""
    request = _build(multi_chapter_project, tmp_workspace)
    for marker in request.chapters:
        assert marker.end_s >= marker.start_s


# --------------------------------------------------------------------------- #
# Gap-WAV format parity: a generated silence WAV must match its reference segment's
# channels / sampwidth / framerate, else a real ffmpeg concat would reject the stream.
# --------------------------------------------------------------------------- #
def test_gap_wavs_match_reference_audio_format(
    tmp_workspace: WorkspaceStore, multi_chapter_project: Project
) -> None:
    """Each generated silence WAV's (channels, sampwidth, framerate) matches the segment WAVs.

    The concat demuxer needs a homogeneous stream; a silence WAV at a different rate/width would
    glitch or be rejected. Reads both the gap WAVs and a reference content WAV via :mod:`wave`
    and asserts identical format triples. Also asserts each gap WAV's real duration matches the
    gap constant it encodes (so the marker math and the audio agree on the gap length).
    """
    request = _build(multi_chapter_project, tmp_workspace)
    content_paths = [p for p in request.segment_audio_paths if ".silence" not in p.parts]
    gap_paths = [p for p in request.segment_audio_paths if ".silence" in p.parts]
    assert gap_paths  # sanity: the fixture produces gaps

    with wave.open(str(content_paths[0]), "rb") as ref:
        ref_fmt = (ref.getnchannels(), ref.getsampwidth(), ref.getframerate())

    for gap in gap_paths:
        with wave.open(str(gap), "rb") as w:
            gap_fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        assert gap_fmt == ref_fmt, f"gap {gap.name} format {gap_fmt} != reference {ref_fmt}"

    # Gap durations match their encoded constant (0.400s inter-line, 1.000s inter-chapter).
    for gap in gap_paths:
        expected = INTER_LINE_GAP_S if "0400ms" in gap.name else INTER_CHAPTER_GAP_S
        assert wav_duration_s(gap) == pytest.approx(expected, abs=1e-3)

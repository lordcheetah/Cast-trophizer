"""Unit tests for the offline assemble orchestration (no stage, no ffmpeg).

Exercises the pure seam in :mod:`casttrophizer.audio.assemble` against the tiny silent-WAV
fixtures: the renderable predicate (parity with synthesize), ``wav_duration_s``, the
``unrendered_segments`` precheck, ``sanitize_filename``, ``escape_ffmetadata``, and
``build_assembly_request`` ordering / markers / cover / out_path / stop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer.audio.assemble import (
    INTER_CHAPTER_GAP_S,
    INTER_LINE_GAP_S,
    build_assembly_request,
    escape_ffmetadata,
    is_renderable,
    sanitize_filename,
    unrendered_segments,
    wav_duration_s,
)
from casttrophizer.audio.synthesize import RENDERED_STATUSES
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.models import Project, Segment
from casttrophizer.errors import AssemblyError
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import RecordingProgressReporter


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _seg(text: str) -> Segment:
    return Segment(
        id="seg-x",
        text=text,
        speaker_id=None,
        role=SpeakerRole.NARRATOR,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )


def _renderable(project: Project) -> list[Segment]:
    return [
        s for ch in project.book.chapters for ln in ch.lines for s in ln.segments if s.text.strip()
    ]


def _dur(cache: AudioCache, seg: Segment) -> float:
    assert seg.audio_cache_key is not None
    return wav_duration_s(cache.path_for_key(seg.audio_cache_key))


# --------------------------------------------------------------------------- #
# is_renderable — parity with synthesize's whitespace-skip rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [("hello", True), ("", False), ("   ", False), ("\n\t", False), (" x ", True)],
)
def test_is_renderable_matches_whitespace_rule(text: str, expected: bool) -> None:
    assert is_renderable(_seg(text)) is expected


def test_rendered_statuses_is_the_synthesize_set() -> None:
    # The precheck reuses synthesize's frozenset, not a local copy.
    assert RENDERED_STATUSES == frozenset({ReviewStatus.COMPLETED, ReviewStatus.APPROVED})


# --------------------------------------------------------------------------- #
# wav_duration_s
# --------------------------------------------------------------------------- #
def test_wav_duration_reads_fixture_wav(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    seg = next(s for s in _renderable(assemble_ready_project) if s.text == "The hall was silent.")
    # 0.20s at 22050 Hz -> 4410 frames -> exactly 0.20s.
    assert _dur(cache, seg) == pytest.approx(0.20, abs=1e-6)


def test_wav_duration_raises_on_bad_wav(tmp_path: Path) -> None:
    bad = tmp_path / "not.wav"
    bad.write_bytes(b"not a wav file")
    with pytest.raises(AssemblyError):
        wav_duration_s(bad)


# --------------------------------------------------------------------------- #
# unrendered_segments precheck
# --------------------------------------------------------------------------- #
def test_unrendered_empty_when_all_rendered(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    assert unrendered_segments(assemble_ready_project, cache) == []


def test_unrendered_flags_missing_key(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    target = _renderable(assemble_ready_project)[0]
    target.audio_cache_key = None
    missing = unrendered_segments(assemble_ready_project, cache)
    assert any(target.id in m for m in missing)


def test_unrendered_flags_non_rendered_status(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    target = _renderable(assemble_ready_project)[0]
    target.audio_status = ReviewStatus.FAILED
    missing = unrendered_segments(assemble_ready_project, cache)
    assert any(target.id in m for m in missing)


def test_unrendered_flags_missing_file(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    target = _renderable(assemble_ready_project)[0]
    assert target.audio_cache_key is not None
    cache.path_for_key(target.audio_cache_key).unlink()
    missing = unrendered_segments(assemble_ready_project, cache)
    assert any(target.id in m for m in missing)


def test_unrendered_skips_whitespace_only(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    # The whitespace-only segment has no key / PENDING but must not be flagged.
    ws = next(
        s
        for ch in assemble_ready_project.book.chapters
        for ln in ch.lines
        for s in ln.segments
        if not s.text.strip()
    )
    assert ws.audio_cache_key is None
    assert unrendered_segments(assemble_ready_project, cache) == []


# --------------------------------------------------------------------------- #
# sanitize_filename
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("A Sample Tale", "A Sample Tale"),
        ('bad/name:with*reserved?"<>|chars', "badnamewithreservedchars"),
        ("  spaced   out  ", "spaced out"),
        ("Rësumé 章", "Rësumé 章"),  # unicode letters preserved
        ("", "audiobook"),
        ("///", "audiobook"),
        ("\x00\x01control", "control"),
    ],
)
def test_sanitize_filename(title: str, expected: str) -> None:
    assert sanitize_filename(title) == expected


# --------------------------------------------------------------------------- #
# escape_ffmetadata
# --------------------------------------------------------------------------- #
def test_escape_ffmetadata_escapes_reserved() -> None:
    assert escape_ffmetadata("a=b;c#d\\e") == "a\\=b\\;c\\#d\\\\e"
    assert escape_ffmetadata("line1\nline2") == "line1\\\nline2"


def test_escape_ffmetadata_passes_unicode() -> None:
    assert escape_ffmetadata("Chapître Rësumé 章") == "Chapître Rësumé 章"


def test_escape_ffmetadata_backslash_first() -> None:
    # A literal backslash must be doubled without double-escaping the '=' escape.
    assert escape_ffmetadata("a\\=b") == "a\\\\\\=b"


# --------------------------------------------------------------------------- #
# build_assembly_request — ordering, markers, cover, out_path, stop
# --------------------------------------------------------------------------- #
def test_build_request_ordering_excludes_whitespace(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    request, count = build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    assert count == 3  # three chapters (incl. the empty one)

    expected_order = [
        cache.path_for_key(s.audio_cache_key)  # type: ignore[arg-type]
        for s in _renderable(assemble_ready_project)
    ]
    # No inter-segment / inter-line gap WAV is inserted (INTER_SEGMENT_GAP_S == 0.0 and the
    # inter-line gap only appears between two lines that BOTH have renderable segments).
    # Chapter One has two content lines, so an inter-line silence WAV sits between them.
    seg_paths = [p for p in request.segment_audio_paths if ".silence" not in p.parts]
    assert seg_paths == expected_order


def test_build_request_markers_timing(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    request, _ = build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    markers = request.chapters
    assert [m.title for m in markers] == ["Chapter One", "Chapter Two", "Chapter Three (empty)"]

    r = _renderable(assemble_ready_project)
    d = {s.text: _dur(cache, s) for s in r}

    # Chapter One: "The hall was silent." | inter-line gap | ("Hello," + "said Alice softly.")
    ch1_dur = d["The hall was silent."] + INTER_LINE_GAP_S + d['"Hello,"'] + d["said Alice softly."]
    assert markers[0].start_s == pytest.approx(0.0)
    assert markers[0].end_s == pytest.approx(ch1_dur)

    # Inter-chapter gap plays between chapter one and two.
    ch2_start = ch1_dur + INTER_CHAPTER_GAP_S
    ch2_dur = d['"We meet again,"'] + d["said Alice again."]
    assert markers[1].start_s == pytest.approx(ch2_start)
    assert markers[1].end_s == pytest.approx(ch2_start + ch2_dur)

    # Empty chapter: zero-length marker at the current cursor (after ch2 + inter-chapter gap).
    ch3_start = ch2_start + ch2_dur + INTER_CHAPTER_GAP_S
    assert markers[2].start_s == pytest.approx(ch3_start)
    assert markers[2].end_s == pytest.approx(ch3_start)  # zero length

    # Start times are monotonically non-decreasing.
    starts = [m.start_s for m in markers]
    assert starts == sorted(starts)


def test_build_request_cover_none_when_unset(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    request, _ = build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    assert request.cover_image_path is None


def test_build_request_cover_resolved_when_set(
    tmp_workspace: WorkspaceStore, assemble_with_cover_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    request, _ = build_assembly_request(
        assemble_with_cover_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    assert request.cover_image_path == Path(assemble_with_cover_project.book.cover_image_path)  # type: ignore[arg-type]


def test_build_request_cover_none_when_file_missing(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    assemble_ready_project.book.cover_image_path = str(tmp_workspace.layout.root / "nope.png")
    request, _ = build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    assert request.cover_image_path is None  # set-but-missing -> no cover


def test_build_request_out_path_sanitized(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    request, _ = build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    assert request.out_path == tmp_workspace.layout.output_dir / "A Sample Tale.m4b"


def test_build_request_empty_title_falls_back(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    assemble_ready_project.book.title = "///"
    request, _ = build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(),
        should_stop=lambda: False,
    )
    assert request is not None
    assert request.out_path == tmp_workspace.layout.output_dir / "audiobook.m4b"


def test_build_request_stop_returns_none(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    request, count = build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=RecordingProgressReporter(stop=True),
        should_stop=lambda: True,
    )
    assert request is None
    assert count == 0


def test_build_request_reports_progress_per_chapter(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    progress = RecordingProgressReporter()
    build_assembly_request(
        assemble_ready_project,
        cache,
        tmp_workspace.layout,
        progress=progress,
        should_stop=lambda: False,
    )
    assert progress.total == 3  # one per chapter
    assert sum(n for n, _ in progress.advances) == 3

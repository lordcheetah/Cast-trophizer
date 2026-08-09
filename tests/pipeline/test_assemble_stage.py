"""AssembleStage integration tests.

Run against the saved ``assemble_ready_project`` with a real ``WorkspaceStore`` and the
offline ``FakeM4BAssembler`` injected via ``AssembleStage(assembler=...)`` — no real ffmpeg /
mutagen ever run. Assertions read the persisted project back (proving it was saved) and inspect
the single ``AssemblyRequest`` the fake recorded (ordering / markers / cover / out_path).
Covers: the fail-fast precheck; ordering + resolved WAV paths; chapter markers + timing; cover
embed-if-set; sanitized output path; missing-ffmpeg / assembler-failure FAILED paths;
stop/resume; empty chapter; whole-book-empty; persistence + purity; is_complete / next_stage;
and that the stage needs no TTS/LLM provider.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.audio.assemble import (
    INTER_CHAPTER_GAP_S,
    INTER_LINE_GAP_S,
    wav_duration_s,
)
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project, Segment
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.assemble import AssembleStage
from casttrophizer.pipeline.stages.synthesize import SynthesizeStage
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeM4BAssembler, RecordingProgressReporter


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ctx(store: WorkspaceStore, progress: RecordingProgressReporter) -> StageContext:
    # No TTS / LLM provider — assemble touches neither.
    return StageContext(store=store, progress=progress, llm=None, tts=None)


def _renderable(project: Project) -> list[Segment]:
    return [
        s for ch in project.book.chapters for ln in ch.lines for s in ln.segments if s.text.strip()
    ]


# --------------------------------------------------------------------------- #
# precheck
# --------------------------------------------------------------------------- #
def test_precheck_fails_naming_unrendered(
    tmp_workspace: WorkspaceStore, assemble_unrendered_project: Project
) -> None:
    fake = FakeM4BAssembler()
    result = AssembleStage(assembler=fake).run(
        assemble_unrendered_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.status == ReviewStatus.FAILED
    assert "not rendered" in result.message
    assert "synthesize" in result.message
    assert fake.requests == []  # never handed to the assembler
    assert str(StageName.ASSEMBLE) not in assemble_unrendered_project.stage_status
    assert list(tmp_workspace.layout.output_dir.glob("*.m4b")) == []


def test_precheck_message_reports_count_and_no_ellipsis_at_boundary(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    """With every renderable segment unrendered, the message reports the full count.

    The fixture has 5 renderable segments (== ``_MAX_NAMED_UNRENDERED``), so all are named and
    no truncation ellipsis appears. Locks the count wording + the exactly-at-boundary case.
    """
    renderable = _renderable(assemble_ready_project)
    assert len(renderable) == 5
    for seg in renderable:
        seg.audio_cache_key = None
        seg.audio_status = ReviewStatus.PENDING
    tmp_workspace.save(assemble_ready_project)

    fake = FakeM4BAssembler()
    result = AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.status == ReviewStatus.FAILED
    assert result.message.startswith("5 segments not rendered")
    assert "…" not in result.message  # exactly 5 named -> no truncation ellipsis
    assert fake.requests == []


def test_precheck_message_truncates_with_ellipsis_beyond_five(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    """More than 5 unrendered segments -> names the first 5 and appends a ``…`` marker.

    Adds a sixth renderable-but-unrendered segment to a chapter so the count exceeds
    ``_MAX_NAMED_UNRENDERED``, then asserts the truncation branch fires.
    """
    for seg in _renderable(assemble_ready_project):
        seg.audio_cache_key = None
        seg.audio_status = ReviewStatus.PENDING
    # A sixth unrendered renderable segment (no key, PENDING) on the first line.
    first_line = assemble_ready_project.book.chapters[0].lines[0]
    first_line.segments.append(
        Segment(
            id="seg-extra",
            text="an extra unrendered line",
            speaker_id=None,
            role=first_line.segments[0].role,
            confidence=1.0,
            review_status=ReviewStatus.APPROVED,
            audio_status=ReviewStatus.PENDING,
        )
    )
    tmp_workspace.save(assemble_ready_project)

    fake = FakeM4BAssembler()
    result = AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.status == ReviewStatus.FAILED
    assert result.message.startswith("6 segments not rendered")
    assert result.message.rstrip().endswith("…")
    assert fake.requests == []


def test_precheck_passes_completes_and_persists(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    fake = FakeM4BAssembler()
    result = AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.stage == StageName.ASSEMBLE
    assert result.status == ReviewStatus.COMPLETED

    reloaded = WorkspaceStore(tmp_workspace.layout).load()
    assert reloaded.stage_status[str(StageName.ASSEMBLE)] == ReviewStatus.COMPLETED

    out = tmp_workspace.layout.output_dir / "A Sample Tale.m4b"
    assert out.is_file()
    assert len(fake.requests) == 1
    assert fake.requests[0].out_path == out


# --------------------------------------------------------------------------- #
# ordering + resolved WAV paths
# --------------------------------------------------------------------------- #
def test_segments_in_reading_order_with_resolved_paths(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    fake = FakeM4BAssembler()
    AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    cache = AudioCache(tmp_workspace.layout)
    request = fake.requests[0]

    expected = [
        cache.path_for_key(s.audio_cache_key)  # type: ignore[arg-type]
        for s in _renderable(assemble_ready_project)
    ]
    seg_paths = [p for p in request.segment_audio_paths if ".silence" not in p.parts]
    assert seg_paths == expected  # exact chapter -> line -> segment order, whitespace excluded


# --------------------------------------------------------------------------- #
# chapter markers + timing
# --------------------------------------------------------------------------- #
def test_chapter_markers_titles_and_timing(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    fake = FakeM4BAssembler()
    AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    cache = AudioCache(tmp_workspace.layout)
    markers = fake.requests[0].chapters
    assert [m.title for m in markers] == ["Chapter One", "Chapter Two", "Chapter Three (empty)"]

    def dur(text: str) -> float:
        seg = next(s for s in _renderable(assemble_ready_project) if s.text == text)
        return wav_duration_s(cache.path_for_key(seg.audio_cache_key))  # type: ignore[arg-type]

    ch1 = (
        dur("The hall was silent.") + INTER_LINE_GAP_S + dur('"Hello,"') + dur("said Alice softly.")
    )
    assert markers[0].start_s == 0.0
    assert abs(markers[0].end_s - ch1) < 1e-6

    ch2_start = ch1 + INTER_CHAPTER_GAP_S
    ch2 = dur('"We meet again,"') + dur("said Alice again.")
    assert abs(markers[1].start_s - ch2_start) < 1e-6
    assert abs(markers[1].end_s - (ch2_start + ch2)) < 1e-6

    # Empty chapter -> zero-length marker.
    ch3_start = ch2_start + ch2 + INTER_CHAPTER_GAP_S
    assert abs(markers[2].start_s - ch3_start) < 1e-6
    assert markers[2].start_s == markers[2].end_s

    starts = [m.start_s for m in markers]
    assert starts == sorted(starts)  # monotonically non-decreasing


# --------------------------------------------------------------------------- #
# cover embed-if-set
# --------------------------------------------------------------------------- #
def test_cover_embedded_when_set(
    tmp_workspace: WorkspaceStore, assemble_with_cover_project: Project
) -> None:
    fake = FakeM4BAssembler()
    AssembleStage(assembler=fake).run(
        assemble_with_cover_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    request = fake.requests[0]
    cover = Path(assemble_with_cover_project.book.cover_image_path)  # type: ignore[arg-type]
    assert request.cover_image_path == cover
    # The cover is a read-only input: it is NOT copied into the workspace.
    assert cover.parent != tmp_workspace.layout.root
    assert not (tmp_workspace.layout.root / cover.name).exists()


def test_no_cover_when_unset(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    fake = FakeM4BAssembler()
    AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert fake.requests[0].cover_image_path is None


# --------------------------------------------------------------------------- #
# output path
# --------------------------------------------------------------------------- #
def test_output_path_sanitized(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    fake = FakeM4BAssembler()
    AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert fake.requests[0].out_path == tmp_workspace.layout.output_dir / "A Sample Tale.m4b"


def test_output_path_hostile_title_sanitized(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    assemble_ready_project.book.title = 'bad/name:*?"<>|'
    tmp_workspace.save(assemble_ready_project)
    fake = FakeM4BAssembler()
    AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert fake.requests[0].out_path == tmp_workspace.layout.output_dir / "badname.m4b"


def test_output_path_empty_title_fallback(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    assemble_ready_project.book.title = "   "
    tmp_workspace.save(assemble_ready_project)
    fake = FakeM4BAssembler()
    AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert fake.requests[0].out_path == tmp_workspace.layout.output_dir / "audiobook.m4b"


# --------------------------------------------------------------------------- #
# failure paths
# --------------------------------------------------------------------------- #
def test_ffmpeg_missing_fails(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    fake = FakeM4BAssembler(available=False)
    result = AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.status == ReviewStatus.FAILED
    assert "ffmpeg not found" in result.message
    assert fake.requests == []  # assemble never called
    assert str(StageName.ASSEMBLE) not in assemble_ready_project.stage_status


def test_assembler_failure_fails_no_flag(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    fake = FakeM4BAssembler(fail=True)
    result = AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.status == ReviewStatus.FAILED
    assert "fake assembly failure" in result.message
    reloaded = tmp_workspace.load()
    assert str(StageName.ASSEMBLE) not in reloaded.stage_status
    # The fake raised before writing -> no half-written M4B.
    assert list(tmp_workspace.layout.output_dir.glob("*.m4b")) == []


# --------------------------------------------------------------------------- #
# stop / resume
# --------------------------------------------------------------------------- #
def test_stop_during_build_then_resume(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    stage = AssembleStage(assembler=FakeM4BAssembler())
    result = stage.run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(stop=True))
    )
    assert result.status == ReviewStatus.STOPPED
    reloaded = tmp_workspace.load()
    assert str(StageName.ASSEMBLE) not in reloaded.stage_status
    assert stage.is_complete(reloaded) is False
    assert list(tmp_workspace.layout.output_dir.glob("*.m4b")) == []

    # Resume with a fresh (non-stopping) reporter -> COMPLETED.
    fresh = FakeM4BAssembler()
    result2 = AssembleStage(assembler=fresh).run(
        reloaded, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result2.status == ReviewStatus.COMPLETED
    assert len(fresh.requests) == 1


# --------------------------------------------------------------------------- #
# whole-book empty -> FAILED
# --------------------------------------------------------------------------- #
def test_whole_book_no_renderable_audio_fails(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    # Blank out every renderable segment's text so nothing is renderable.
    for seg in _renderable(assemble_ready_project):
        seg.text = "   "
        seg.audio_cache_key = None
        seg.audio_status = ReviewStatus.PENDING
    tmp_workspace.save(assemble_ready_project)

    fake = FakeM4BAssembler()
    result = AssembleStage(assembler=fake).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.status == ReviewStatus.FAILED
    assert "no rendered audio" in result.message
    assert fake.requests == []


# --------------------------------------------------------------------------- #
# persistence & purity
# --------------------------------------------------------------------------- #
def test_persisted_reload_leaves_segments_untouched(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    before = {
        s.id: (s.text, s.speaker_id, s.audio_cache_key, s.audio_status, s.review_status)
        for s in _renderable(assemble_ready_project)
    }
    speakers_before = {sp.id: (sp.name, sp.voice_clip_id) for sp in assemble_ready_project.speakers}

    AssembleStage(assembler=FakeM4BAssembler()).run(
        assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    reloaded = tmp_workspace.load()
    after = {
        s.id: (s.text, s.speaker_id, s.audio_cache_key, s.audio_status, s.review_status)
        for s in _renderable(reloaded)
    }
    assert after == before  # assemble only writes stage_status[ASSEMBLE]
    assert {sp.id: (sp.name, sp.voice_clip_id) for sp in reloaded.speakers} == speakers_before
    assert reloaded.schema_version == CURRENT_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# is_complete / next_stage / re-run
# --------------------------------------------------------------------------- #
def test_is_complete_after_run(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    stage = AssembleStage(assembler=FakeM4BAssembler())
    assert stage.is_complete(assemble_ready_project) is False
    stage.run(assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert stage.is_complete(assemble_ready_project) is True


def test_pipeline_next_stage_none_after_assemble(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    pipeline = Pipeline([SynthesizeStage(), AssembleStage(assembler=FakeM4BAssembler())])
    # Synthesize is already COMPLETED in the fixture; the pipeline runs assemble.
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter()))
    reloaded = tmp_workspace.load()
    assert pipeline.next_stage(reloaded) is None


def test_direct_rerun_always_rebuilds(
    tmp_workspace: WorkspaceStore, assemble_ready_project: Project
) -> None:
    # A direct run() rebuilds even when already COMPLETED (Pipeline path stays idempotent).
    fake = FakeM4BAssembler()
    stage = AssembleStage(assembler=fake)
    stage.run(assemble_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))
    reloaded = tmp_workspace.load()
    assert reloaded.stage_status[str(StageName.ASSEMBLE)] == ReviewStatus.COMPLETED

    result2 = stage.run(reloaded, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert result2.status == ReviewStatus.COMPLETED
    assert len(fake.requests) == 2  # rebuilt a second time

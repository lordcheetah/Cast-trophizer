"""ParseStage integration tests.

The stage is exercised against a saved ``parse_ready_project`` and a real
``WorkspaceStore``; assertions read the **persisted** project back (proving it was saved,
not just mutated in memory). Covers completion + persistence, empty segments, resume /
idempotence, stop-mid-parse, read-only source, the failure path, and progress reporting.
All offline — no LLM/TTS/network (``StageContext.llm``/``tts`` stay ``None``).
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.ebook import parser_for
from casttrophizer.ebook.base import EbookParser, ParsedBook
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.parse import ParseStage
from casttrophizer.workspace.layout import WorkspaceLayout
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import RecordingProgressReporter


class CountingParser(EbookParser):
    """Wraps the real parser and counts ``parse`` calls (proves resume skips re-parsing)."""

    def __init__(self, inner: EbookParser) -> None:
        self._inner = inner
        self.parse_calls = 0

    def supports(self, path: Path) -> bool:
        return self._inner.supports(path)

    def parse(self, path: Path) -> ParsedBook:
        self.parse_calls += 1
        return self._inner.parse(path)


class StopAfterNProgress(RecordingProgressReporter):
    """Reports normally but trips ``should_stop`` only after ``n`` advances.

    Lets a test stop *mid*-parse (after the first chapter is appended) rather than before
    any work, exercising the partial-persist + resume path with non-empty partial state.
    """

    def __init__(self, n: int) -> None:
        super().__init__()
        self._n = n

    def should_stop(self) -> bool:
        return len(self.advances) >= self._n


def _ctx(store: WorkspaceStore, progress: RecordingProgressReporter) -> StageContext:
    return StageContext(store=store, progress=progress)


def _tree_snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    """Map every file under ``root`` to ``(mtime_ns, bytes)`` for change detection."""
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


def test_run_completes_and_persists(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    progress = RecordingProgressReporter()
    result = ParseStage().run(parse_ready_project, _ctx(tmp_workspace, progress))

    assert result.stage == StageName.PARSE
    assert result.status == ReviewStatus.COMPLETED

    reloaded = tmp_workspace.load()
    assert reloaded.stage_status[str(StageName.PARSE)] == ReviewStatus.COMPLETED
    assert reloaded.book.title == "A Sample Tale"
    assert reloaded.book.author == "Test Author"
    assert [c.title for c in reloaded.book.chapters] == ["Chapter One", "Chapter Two"]
    assert [c.order for c in reloaded.book.chapters] == [1, 2]


def test_lines_have_text_and_no_segments(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    ParseStage().run(parse_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))
    reloaded = tmp_workspace.load()
    for chapter in reloaded.book.chapters:
        for line in chapter.lines:
            assert line.text != ""
            # Segmentation is the attribute stage's job: parse leaves segments empty.
            assert line.segments == []


def test_is_complete_after_run(tmp_workspace: WorkspaceStore, parse_ready_project: Project) -> None:
    stage = ParseStage()
    assert stage.is_complete(parse_ready_project) is False
    stage.run(parse_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert stage.is_complete(parse_ready_project) is True


def test_resume_does_not_reparse_and_next_stage_advances(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    spy = CountingParser(parser_for(Path(parse_ready_project.book.source_ebook_path)))
    stage = ParseStage(parser=spy)
    pipeline = Pipeline([stage])

    # First run parses once and completes.
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter()))
    assert spy.parse_calls == 1

    # A complete stage is skipped on the next run -> parser is not called again.
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter()))
    assert spy.parse_calls == 1

    # next_stage advances past parse (here the only stage, so None).
    reloaded = tmp_workspace.load()
    assert pipeline.next_stage(reloaded) is None


def test_stop_mid_parse_then_resume_completes(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    stage = ParseStage()

    # Stop is requested before the first chapter is appended -> STOPPED, not COMPLETED.
    stopping = RecordingProgressReporter(stop=True)
    result = stage.run(parse_ready_project, _ctx(tmp_workspace, stopping))
    assert result.status == ReviewStatus.STOPPED

    persisted = tmp_workspace.load()
    assert persisted.stage_status.get(str(StageName.PARSE)) != ReviewStatus.COMPLETED
    assert stage.is_complete(persisted) is False

    # Resume with stop cleared -> completes.
    resuming = RecordingProgressReporter()
    result2 = stage.run(persisted, _ctx(tmp_workspace, resuming))
    assert result2.status == ReviewStatus.COMPLETED

    final = tmp_workspace.load()
    assert stage.is_complete(final) is True
    assert len(final.book.chapters) == 2


def test_source_epub_unchanged(tmp_workspace: WorkspaceStore, parse_ready_project: Project) -> None:
    src = Path(parse_ready_project.book.source_ebook_path)
    before_bytes = src.read_bytes()
    before_mtime = src.stat().st_mtime_ns

    ParseStage().run(parse_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))

    assert src.read_bytes() == before_bytes
    assert src.stat().st_mtime_ns == before_mtime
    # The read-only source path is preserved on the persisted book.
    assert tmp_workspace.load().book.source_ebook_path == str(src)


def test_failure_path_returns_failed_and_leaves_status_unset(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project, tmp_path: Path
) -> None:
    bogus = tmp_path / "broken.epub"
    bogus.write_text("not an epub", encoding="utf-8")
    parse_ready_project.book.source_ebook_path = str(bogus)
    tmp_workspace.save(parse_ready_project)

    result = ParseStage().run(parse_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert result.status == ReviewStatus.FAILED
    # Status left unset so a fixed input can re-run.
    assert str(StageName.PARSE) not in parse_ready_project.stage_status


def test_progress_reports_total_and_one_advance_per_chapter(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    progress = RecordingProgressReporter()
    ParseStage().run(parse_ready_project, _ctx(tmp_workspace, progress))
    assert progress.total == 2  # two chapters
    assert len(progress.advances) == 2  # one advance per chapter
    assert [n for n, _ in progress.advances] == [1, 1]


def test_completion_persists_across_a_fresh_store(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    """Completion survives a brand-new store instance (proves it is on disk, not in RAM)."""
    ParseStage().run(parse_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))

    # A fresh store reading the same workspace dir sees the completed parse.
    fresh = WorkspaceStore(WorkspaceLayout.for_dir(tmp_workspace.layout.root))
    reloaded = fresh.load()
    assert reloaded.stage_status[str(StageName.PARSE)] == ReviewStatus.COMPLETED
    assert ParseStage().is_complete(reloaded) is True
    # Every reloaded Line carries text and no segments (the §4 decision, on disk).
    for chapter in reloaded.book.chapters:
        for line in chapter.lines:
            assert line.text != ""
            assert line.segments == []


def test_stop_after_first_chapter_persists_partial_then_resume_completes(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    """Stopping *after* the first chapter persists 1 chapter, STOPPED, not COMPLETED."""
    stopping = StopAfterNProgress(n=1)
    result = ParseStage().run(parse_ready_project, _ctx(tmp_workspace, stopping))
    assert result.status == ReviewStatus.STOPPED

    persisted = tmp_workspace.load()
    # Partial state saved: exactly the first chapter, status NOT completed.
    assert len(persisted.book.chapters) == 1
    assert persisted.book.chapters[0].title == "Chapter One"
    assert persisted.stage_status.get(str(StageName.PARSE)) != ReviewStatus.COMPLETED

    # Resume with stop cleared re-parses from scratch and completes with all chapters.
    result2 = ParseStage().run(persisted, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert result2.status == ReviewStatus.COMPLETED
    final = tmp_workspace.load()
    assert [c.title for c in final.book.chapters] == ["Chapter One", "Chapter Two"]
    assert ParseStage().is_complete(final) is True


def test_parser_writes_nothing_outside_the_workspace(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    """The read-only source tree is untouched; only ``project.json`` changes in the workspace."""
    src = Path(parse_ready_project.book.source_ebook_path)
    inputs_root = src.parent  # tmp_path/"inputs" — the read-only input tree
    before_inputs = _tree_snapshot(inputs_root)

    ParseStage().run(parse_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))

    # No file in the input tree was added, removed, or modified by the parse.
    assert _tree_snapshot(inputs_root) == before_inputs

    # Inside the workspace, the only file the stage touches is project.json (no stray
    # cover/audio/output artifacts written by parse).
    workspace_files = {
        str(p.relative_to(tmp_workspace.layout.root))
        for p in tmp_workspace.layout.root.rglob("*")
        if p.is_file()
    }
    assert workspace_files == {"project.json"}

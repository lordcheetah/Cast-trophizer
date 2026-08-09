"""CorrectTextStage integration tests.

Exercised against the saved ``correct_ready_project`` and a real ``WorkspaceStore``;
assertions read the **persisted** project back (proving it was saved, not just mutated).
Correctors are injected (a ``FakeSpellchecker``-backed pair) so no real dictionary loads —
exactly as parse tests inject a ``CountingParser``. Covers auto-apply vs. surface, protection,
idempotent re-run, stop/resume, segments-untouched, the failure path, progress, and offline.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Chapter, Line, Project
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.correct import CorrectTextStage
from casttrophizer.text.base import Corrector
from casttrophizer.text.ocr import OcrHeuristicCorrector
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import CountingCorrector, FakeSpellchecker, RaisingCorrector
from tests.fakes.fake_progress import RecordingProgressReporter


def _fake_correctors() -> list[Corrector]:
    """OCR + spellcheck correctors over a fixed fake dictionary (no real dict load)."""
    fake = FakeSpellchecker(
        known={
            "he",
            "said",
            "hello",
            "the",
            "spoke",
            "drew",
            "her",
            "blade",
            "smiled",
            "quiet",
            "hall",
            "was",
            "empty",
        },
        corrections={"narrarator": "narrator"},
    )
    return [OcrHeuristicCorrector(is_word=fake.is_word), fake.as_corrector()]


def _ctx(store: WorkspaceStore, progress: RecordingProgressReporter) -> StageContext:
    return StageContext(store=store, progress=progress)


def _line_by_order(project: Project, order: int) -> Line:
    return next(line for line in project.book.chapters[0].lines if line.order == order)


class StopAfterNProgress(RecordingProgressReporter):
    """Trips ``should_stop`` only after ``n`` advances (stop mid-pass with partial state)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._n = n

    def should_stop(self) -> bool:
        return len(self.advances) >= self._n


# --------------------------------------------------------------------------- #
# completion + the four planted dispositions, all read back from disk
# --------------------------------------------------------------------------- #
def test_run_completes_and_persists(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    result = CorrectTextStage(_fake_correctors()).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.stage == StageName.CORRECT
    assert result.status == ReviewStatus.COMPLETED

    reloaded = tmp_workspace.load()
    assert reloaded.stage_status[str(StageName.CORRECT)] == ReviewStatus.COMPLETED


def test_whitespace_line_is_auto_fixed_and_recoverable(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    CorrectTextStage(_fake_correctors()).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    line = _line_by_order(tmp_workspace.load(), 0)
    assert line.text == "He said, hello"  # mutated
    auto = [s for s in line.suggestions if s.status == ReviewStatus.AUTO_APPLIED]
    assert len(auto) == 1
    assert auto[0].original == "He  said , hello"  # original recovers the text


def test_misspelling_is_surfaced_pending_text_unchanged(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    CorrectTextStage(_fake_correctors()).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    line = _line_by_order(tmp_workspace.load(), 1)
    assert line.text == "The narrarator spoke."  # unchanged
    pending = [s for s in line.suggestions if s.status == ReviewStatus.PENDING]
    assert len(pending) == 1
    assert pending[0].original == "narrarator"
    assert pending[0].suggested == "narrator"


def test_protected_name_yields_no_suggestion(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    CorrectTextStage(_fake_correctors()).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    reloaded = tmp_workspace.load()
    line = _line_by_order(reloaded, 2)  # "Aelin drew her blade."
    assert line.text == "Aelin drew her blade."
    assert line.suggestions == []


def test_clean_line_yields_no_suggestion(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    CorrectTextStage(_fake_correctors()).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    line = _line_by_order(tmp_workspace.load(), 4)  # "The quiet hall was empty."
    assert line.suggestions == []


def test_combined_auto_and_pending_on_one_line_through_stage(
    tmp_workspace: WorkspaceStore, sample_epub: Path
) -> None:
    # End-to-end: a single line with BOTH a whitespace defect and a misspelling. Text receives
    # only the whitespace fix; the misspelling stays a separate PENDING suggestion (not applied).
    ch_id = new_id("ch")
    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text="The  narrarator spoke.",  # double space (auto) + misspelling (pending)
        segments=[],
    )
    book = Book(
        title="t",
        author="a",
        source_ebook_path=str(sample_epub),
        chapters=[Chapter(id=ch_id, order=0, title="One", lines=[line])],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="combined",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        stage_status={str(StageName.PARSE): ReviewStatus.COMPLETED},
    )
    tmp_workspace.save(project)

    CorrectTextStage(_fake_correctors()).run(
        project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    reloaded = _line_by_order(tmp_workspace.load(), 0)
    assert reloaded.text == "The narrarator spoke."  # ONLY the whitespace fix landed
    auto = [s for s in reloaded.suggestions if s.status == ReviewStatus.AUTO_APPLIED]
    pending = [s for s in reloaded.suggestions if s.status == ReviewStatus.PENDING]
    assert len(auto) == 1 and auto[0].reason == "ocr-artifact"
    assert len(pending) == 1 and pending[0].reason == "spellcheck"
    assert pending[0].original == "narrarator" and pending[0].suggested == "narrator"


def test_segments_stay_empty_on_every_line(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    CorrectTextStage(_fake_correctors()).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    for line in tmp_workspace.load().book.chapters[0].lines:
        assert line.segments == []  # correct must not touch segments


# --------------------------------------------------------------------------- #
# idempotency
# --------------------------------------------------------------------------- #
def test_is_complete_after_run(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    stage = CorrectTextStage(_fake_correctors())
    assert stage.is_complete(correct_ready_project) is False
    stage.run(correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert stage.is_complete(correct_ready_project) is True


def test_rerun_is_a_noop(tmp_workspace: WorkspaceStore, correct_ready_project: Project) -> None:
    stage = CorrectTextStage(_fake_correctors())
    stage.run(correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))

    first = tmp_workspace.load()
    first_counts = {ln.order: len(ln.suggestions) for ln in first.book.chapters[0].lines}
    first_texts = {ln.order: ln.text for ln in first.book.chapters[0].lines}

    # Second run on the persisted (already-corrected) project changes nothing.
    stage.run(first, _ctx(tmp_workspace, RecordingProgressReporter()))
    second = tmp_workspace.load()

    assert {ln.order: len(ln.suggestions) for ln in second.book.chapters[0].lines} == first_counts
    assert {ln.order: ln.text for ln in second.book.chapters[0].lines} == first_texts


def test_rerun_does_not_reprocess_corrected_lines(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    # A counting OCR corrector proves processed lines are skipped on the second run.
    fake = FakeSpellchecker(known={"he", "said", "hello"}, corrections={})
    counter = CountingCorrector(OcrHeuristicCorrector(is_word=fake.is_word))
    stage = CorrectTextStage([counter])

    stage.run(correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))
    # Line 0 produced an AUTO suggestion -> it now carries a correction reason and is skipped;
    # lines with no suggestion are re-processed on a second pass (acceptable: no suggestion to
    # double). Count the calls that fall on the auto-fixed line specifically.
    after_first = counter.calls

    stage.run(tmp_workspace.load(), _ctx(tmp_workspace, RecordingProgressReporter()))
    # The whitespace line (which now has an AUTO suggestion) is not re-fed to the corrector.
    lines = tmp_workspace.load().book.chapters[0].lines
    assert sum(len(ln.suggestions) for ln in lines) >= 1  # stable suggestions
    assert counter.calls < after_first * 2  # fewer than a full re-pass


def test_rerun_adds_no_new_suggestions_for_pending_bearing_line(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    # The idempotency key is "carries a correction-reason suggestion (any status)". A line that
    # surfaced a PENDING spellcheck suggestion must be skipped on re-run — not re-surfaced.
    stage = CorrectTextStage(_fake_correctors())
    stage.run(correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter()))

    first = tmp_workspace.load()
    misspelled = _line_by_order(first, 1)  # "The narrarator spoke." -> 1 PENDING
    assert [s.status for s in misspelled.suggestions] == [ReviewStatus.PENDING]

    stage.run(first, _ctx(tmp_workspace, RecordingProgressReporter()))
    again = _line_by_order(tmp_workspace.load(), 1)
    # Exactly one suggestion still, same identity — not duplicated by the second pass.
    assert len(again.suggestions) == 1
    assert again.suggestions[0].id == misspelled.suggestions[0].id
    assert again.text == "The narrarator spoke."  # still unchanged


def _multi_chapter_project(store: WorkspaceStore, src: Path) -> Project:
    """A two-chapter parsed project, each chapter with one whitespace-fixable line."""

    def line(ch_id: str, text: str) -> Line:
        return Line(id=new_id("line"), chapter_id=ch_id, order=0, text=text, segments=[])

    c1, c2 = new_id("ch"), new_id("ch")
    book = Book(
        title="t",
        author="a",
        source_ebook_path=str(src),
        chapters=[
            Chapter(id=c1, order=0, title="One", lines=[line(c1, "He  said hello")]),
            Chapter(id=c2, order=1, title="Two", lines=[line(c2, "She  smiled wide")]),
        ],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="multi",
        workspace_dir=str(store.layout.root),
        book=book,
        stage_status={str(StageName.PARSE): ReviewStatus.COMPLETED},
    )
    store.save(project)
    return project


def test_multi_chapter_stop_after_one_chapter_then_resume(
    tmp_workspace: WorkspaceStore, sample_epub: Path
) -> None:
    # Genuine partial state: chapter One is processed, the stop trips before chapter Two, and
    # resume finishes chapter Two WITHOUT re-applying chapter One's auto fix.
    project = _multi_chapter_project(tmp_workspace, sample_epub)
    fake = FakeSpellchecker(known={"he", "said", "hello", "she", "smiled", "wide"})
    correctors: list[Corrector] = [OcrHeuristicCorrector(is_word=fake.is_word), fake.as_corrector()]
    stage = CorrectTextStage(correctors)

    result = stage.run(project, _ctx(tmp_workspace, StopAfterNProgress(n=1)))
    assert result.status == ReviewStatus.STOPPED

    after_stop = tmp_workspace.load()
    assert after_stop.stage_status.get(str(StageName.CORRECT)) != ReviewStatus.COMPLETED
    ch1_line = after_stop.book.chapters[0].lines[0]
    ch2_line = after_stop.book.chapters[1].lines[0]
    assert ch1_line.text == "He said hello"  # chapter One processed before the stop
    assert len(ch1_line.suggestions) == 1
    assert ch2_line.text == "She  smiled wide"  # chapter Two untouched
    assert ch2_line.suggestions == []

    result2 = stage.run(after_stop, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert result2.status == ReviewStatus.COMPLETED

    done = tmp_workspace.load()
    done_ch1 = done.book.chapters[0].lines[0]
    done_ch2 = done.book.chapters[1].lines[0]
    # Chapter One's auto fix is NOT double-applied (still exactly one AUTO suggestion).
    assert done_ch1.text == "He said hello"
    assert len([s for s in done_ch1.suggestions if s.status == ReviewStatus.AUTO_APPLIED]) == 1
    # Chapter Two is now corrected too.
    assert done_ch2.text == "She smiled wide"
    assert len([s for s in done_ch2.suggestions if s.status == ReviewStatus.AUTO_APPLIED]) == 1


# --------------------------------------------------------------------------- #
# stop / resume
# --------------------------------------------------------------------------- #
def test_stop_then_resume_completes_without_double_apply(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    stage = CorrectTextStage(_fake_correctors())

    # Stop requested before the (single) chapter is processed -> STOPPED, not COMPLETED.
    stopping = RecordingProgressReporter(stop=True)
    result = stage.run(correct_ready_project, _ctx(tmp_workspace, stopping))
    assert result.status == ReviewStatus.STOPPED

    persisted = tmp_workspace.load()
    assert persisted.stage_status.get(str(StageName.CORRECT)) != ReviewStatus.COMPLETED
    assert stage.is_complete(persisted) is False

    # Resume with stop cleared -> completes; whitespace line auto-fixed exactly once.
    result2 = stage.run(persisted, _ctx(tmp_workspace, RecordingProgressReporter()))
    assert result2.status == ReviewStatus.COMPLETED
    line = _line_by_order(tmp_workspace.load(), 0)
    assert line.text == "He said, hello"
    assert len([s for s in line.suggestions if s.status == ReviewStatus.AUTO_APPLIED]) == 1


# --------------------------------------------------------------------------- #
# pipeline / next_stage
# --------------------------------------------------------------------------- #
def test_next_stage_advances_past_correct(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    pipeline = Pipeline([CorrectTextStage(_fake_correctors())])
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter()))
    reloaded = tmp_workspace.load()
    # The only stage is correct, now complete -> next_stage is None.
    assert pipeline.next_stage(reloaded) is None


# --------------------------------------------------------------------------- #
# read-only source
# --------------------------------------------------------------------------- #
def test_source_epub_unchanged(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    src = Path(correct_ready_project.book.source_ebook_path)
    before_bytes = src.read_bytes()
    before_mtime = src.stat().st_mtime_ns

    CorrectTextStage(_fake_correctors()).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert src.read_bytes() == before_bytes
    assert src.stat().st_mtime_ns == before_mtime

    # Only project.json is written in the workspace.
    workspace_files = {
        str(p.relative_to(tmp_workspace.layout.root))
        for p in tmp_workspace.layout.root.rglob("*")
        if p.is_file()
    }
    assert workspace_files == {"project.json"}


# --------------------------------------------------------------------------- #
# failure path
# --------------------------------------------------------------------------- #
def test_failure_path_returns_failed_and_leaves_status_unset(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    result = CorrectTextStage([RaisingCorrector()]).run(
        correct_ready_project, _ctx(tmp_workspace, RecordingProgressReporter())
    )
    assert result.status == ReviewStatus.FAILED
    assert str(StageName.CORRECT) not in correct_ready_project.stage_status


# --------------------------------------------------------------------------- #
# progress + offline
# --------------------------------------------------------------------------- #
def test_progress_reports_total_and_one_advance_per_chapter(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    progress = RecordingProgressReporter()
    CorrectTextStage(_fake_correctors()).run(correct_ready_project, _ctx(tmp_workspace, progress))
    assert progress.total == 1  # one chapter
    assert len(progress.advances) == 1


def test_stage_runs_offline_without_providers(
    tmp_workspace: WorkspaceStore, correct_ready_project: Project
) -> None:
    ctx = StageContext(store=tmp_workspace, progress=RecordingProgressReporter())
    assert ctx.llm is None and ctx.tts is None
    result = CorrectTextStage(_fake_correctors()).run(correct_ready_project, ctx)
    assert result.status == ReviewStatus.COMPLETED

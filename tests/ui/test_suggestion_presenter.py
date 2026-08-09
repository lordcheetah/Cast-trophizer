"""Loop-free unit tests for :class:`SuggestionPresenter` — no Qt, no event loop, no thread.

A :class:`FakeSuggestionView` records what the presenter pushes; a **real, shared**
:class:`~casttrophizer.review.service.ReviewService` mutates an in-memory project loaded from
``tmp_workspace``. The dedicated build gives a suggestion whose ``original`` actually appears in a
segment (and twice in the line), so the accept path exercises real text->segment propagation.
Every mutating action must funnel through the service, tick ``on_reviewed``, and land on disk.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    TextSuggestion,
    VoiceClip,
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.review.gate import review_blockers
from casttrophizer.review.service import ReviewService
from casttrophizer.review.suggestion_view import SuggestionRow
from casttrophizer.ui.suggestion_presenter import SuggestionPresenter, SuggestionView
from casttrophizer.workspace.store import WorkspaceStore


# --------------------------------------------------------------------------- #
# fakes / helpers
# --------------------------------------------------------------------------- #
class FakeSuggestionView:
    """Records every call the presenter makes (structurally a :class:`SuggestionView`)."""

    def __init__(self) -> None:
        self.suggestions: list[list[SuggestionRow]] = []
        self.progress: list[tuple[int, int]] = []
        self.selected: list[int] = []
        self.errors: list[tuple[str, str]] = []

    def show_suggestions(self, rows: list[SuggestionRow]) -> None:
        self.suggestions.append(rows)

    def show_progress(self, pending: int, total: int) -> None:
        self.progress.append((pending, total))

    def select_suggestion(self, index: int) -> None:
        self.selected.append(index)

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))


class _Recorder:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def _seg(text: str, narrator: Speaker) -> Segment:
    return Segment(
        id=new_id("seg"),
        text=text,
        speaker_id=narrator.id,
        role=SpeakerRole.NARRATOR,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )


def _save_project(store: WorkspaceStore, clip: Path) -> None:
    """Save a project with two PENDING suggestions.

    ``line_typo``'s token ``mie`` appears in **both** its segments (and twice in the line), so
    accepting proves first-containing-segment / first-occurrence propagation. ``line_other``
    carries a second PENDING suggestion (for the pending-count decrement). ``stage_status[REVIEW]``
    starts COMPLETED so a re-segmenting line edit can be shown to re-open the gate.
    """
    narrator_clip = VoiceClip(id=new_id("voice"), source_path=str(clip), label="Narrator")
    narrator = Speaker(
        id=new_id("spk"),
        name="narrator",
        role=SpeakerRole.NARRATOR,
        voice_clip_id=narrator_clip.id,
    )
    ch_id = new_id("ch")

    line_typo = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text="The mie sat. The mie ran.",
        segments=[_seg("The mie sat.", narrator), _seg("The mie ran.", narrator)],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="mie",
                suggested="mouse",
                reason="spellcheck",
                confidence=0.7,
                status=ReviewStatus.PENDING,
            )
        ],
    )
    line_other = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=1,
        text="A clean sentence.",
        segments=[_seg("A clean sentence.", narrator)],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="clean",
                suggested="tidy",
                reason="spellcheck",
                confidence=0.6,
                status=ReviewStatus.PENDING,
            )
        ],
    )
    chapter = Chapter(id=ch_id, order=0, title="One", lines=[line_typo, line_other])
    book = Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter])
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Text Review",
        workspace_dir=str(store.layout.root),
        book=book,
        speakers=[narrator],
        voice_clips=[narrator_clip],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
            str(StageName.REVIEW): ReviewStatus.COMPLETED,
        },
    )
    store.save(project)


def _setup(
    store: WorkspaceStore, clip: Path
) -> tuple[SuggestionPresenter, FakeSuggestionView, _Recorder, ReviewService]:
    """Save the dedicated project, then build the view/recorder/service/presenter over it."""
    _save_project(store, clip)
    view = FakeSuggestionView()
    reviewed = _Recorder()
    service = ReviewService(store, store.load())
    presenter = SuggestionPresenter(view=view, on_reviewed=reviewed)
    presenter.attach(service)
    return presenter, view, reviewed, service


def _sug_id(project: Project, original: str) -> str:
    return next(
        s.id
        for ch in project.book.chapters
        for ln in ch.lines
        for s in ln.suggestions
        if s.original == original
    )


def _line(project: Project, order: int) -> Line:
    return next(ln for ch in project.book.chapters for ln in ch.lines if ln.order == order)


# --------------------------------------------------------------------------- #
# structural / open
# --------------------------------------------------------------------------- #
def test_structural_protocol_conformance() -> None:
    assert isinstance(FakeSuggestionView(), SuggestionView)


def test_open_renders_pending_only_by_default(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    presenter, view, _, _ = _setup(tmp_workspace, fake_voice_clips[0])

    presenter.open()

    # Default filter is pending-only: both PENDING suggestions show, 2 of 2 pending.
    assert {r.original for r in view.suggestions[-1]} == {"mie", "clean"}
    assert view.progress[-1] == (2, 2)


# --------------------------------------------------------------------------- #
# accept (propagation)
# --------------------------------------------------------------------------- #
def test_accept_applies_replace_and_propagates_to_matching_segment(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    presenter, view, reviewed, service = _setup(tmp_workspace, fake_voice_clips[0])
    presenter.open()
    sug_id = _sug_id(service.project, "mie")

    presenter.accept(sug_id)

    line = _line(service.project, 0)
    assert line.text == "The mouse sat. The mie ran."  # line-level: first occurrence only
    # First containing segment fixed + cache reset; the later segment untouched.
    assert line.segments[0].text == "The mouse sat."
    assert line.segments[0].audio_cache_key is None
    assert line.segments[0].audio_status == ReviewStatus.PENDING
    assert line.segments[1].text == "The mie ran."
    # Pending count decremented; on_reviewed fired; persisted.
    assert view.progress[-1] == (1, 2)
    assert reviewed.count == 1
    assert _line(tmp_workspace.load(), 0).text == "The mouse sat. The mie ran."


def test_reject_keeps_text_and_marks_rejected(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    presenter, view, reviewed, service = _setup(tmp_workspace, fake_voice_clips[0])
    presenter.open()
    sug_id = _sug_id(service.project, "mie")

    presenter.reject(sug_id)

    line = _line(service.project, 0)
    assert line.text == "The mie sat. The mie ran."  # unchanged
    assert line.suggestions[0].status == ReviewStatus.REJECTED
    assert view.progress[-1] == (1, 2)  # rejected no longer pending
    assert reviewed.count == 1


# --------------------------------------------------------------------------- #
# edit line (re-segment + re-open attribution)
# --------------------------------------------------------------------------- #
def test_edit_line_resegments_and_reopens_attribution(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    presenter, _, reviewed, service = _setup(tmp_workspace, fake_voice_clips[0])
    presenter.open()
    line = _line(service.project, 1)  # a narration-only line, no NEEDS_REVIEW yet
    before = len(review_blockers(service.project).needs_attribution)

    presenter.edit_line(line.id, '"A brand new quote," said Zed.')

    assert line.text == '"A brand new quote," said Zed.'
    # A new NEEDS_REVIEW quote segment re-opened attribution (criterion 1 grew) ...
    assert len(review_blockers(service.project).needs_attribution) > before
    # ... and the re-segmenting edit invalidated the COMPLETED review flag (in memory + on disk).
    assert str(StageName.REVIEW) not in service.project.stage_status
    assert str(StageName.REVIEW) not in tmp_workspace.load().stage_status
    assert reviewed.count == 1


def test_blank_line_edit_errors_without_commit(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    presenter, view, reviewed, service = _setup(tmp_workspace, fake_voice_clips[0])
    presenter.open()
    line = _line(service.project, 1)
    before = line.text

    presenter.edit_line(line.id, "   ")

    assert view.errors
    assert line.text == before  # nothing committed
    assert reviewed.count == 0


# --------------------------------------------------------------------------- #
# filter / missing ids
# --------------------------------------------------------------------------- #
def test_filter_toggles_between_pending_only_and_all(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    presenter, view, _, service = _setup(tmp_workspace, fake_voice_clips[0])
    presenter.open()
    presenter.reject(_sug_id(service.project, "mie"))  # one becomes REJECTED (resolved)

    presenter.set_filter(False)
    assert len(view.suggestions[-1]) == 2  # all suggestions (incl. the rejected one)

    presenter.set_filter(True)
    assert {r.original for r in view.suggestions[-1]} == {"clean"}  # only the remaining pending


def test_accept_unknown_id_errors_without_notify(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    presenter, view, reviewed, _ = _setup(tmp_workspace, fake_voice_clips[0])
    presenter.open()

    presenter.accept("sug_missing")

    assert view.errors
    assert reviewed.count == 0

"""Loop-free unit tests for :class:`AttributionPresenter` — no Qt, no event loop, no thread.

A :class:`FakeAttributionView` records what the presenter pushes; a **real**
:class:`~casttrophizer.review.service.ReviewService` mutates an in-memory project loaded from
``tmp_workspace`` (the ``review_ready_project`` fixture). Every mutating action must funnel
through that service, tick ``on_reviewed``, and land on disk — asserted below without a widget.
"""

from __future__ import annotations

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
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.review.attribution_view import SegmentRow, SpeakerOption
from casttrophizer.review.gate import review_blockers
from casttrophizer.review.service import ReviewService
from casttrophizer.ui.attribution_presenter import AttributionPresenter, AttributionView
from casttrophizer.workspace.store import WorkspaceStore


# --------------------------------------------------------------------------- #
# fakes / helpers
# --------------------------------------------------------------------------- #
class FakeAttributionView:
    """Records every call the presenter makes (structurally an :class:`AttributionView`)."""

    def __init__(self) -> None:
        self.segments: list[list[SegmentRow]] = []
        self.options: list[list[SpeakerOption]] = []
        self.progress: list[tuple[int, int]] = []
        self.selected: list[int] = []
        self.errors: list[tuple[str, str]] = []

    def show_segments(self, rows: list[SegmentRow]) -> None:
        self.segments.append(rows)

    def show_speaker_options(self, options: list[SpeakerOption]) -> None:
        self.options.append(options)

    def show_progress(self, needs_review: int, total: int) -> None:
        self.progress.append((needs_review, total))

    def select_segment(self, index: int) -> None:
        self.selected.append(index)

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))


class _Recorder:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def _seg_id(project: Project, text: str) -> str:
    return next(
        seg.id
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text == text
    )


def _find_segment(project: Project, text: str) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text == text
    )


def _speaker_id(project: Project, name: str) -> str:
    return next(sp.id for sp in project.speakers if sp.name == name)


def _presenter(
    store: WorkspaceStore,
) -> tuple[AttributionPresenter, FakeAttributionView, _Recorder]:
    view = FakeAttributionView()
    reviewed = _Recorder()
    presenter = AttributionPresenter(view=view, on_reviewed=reviewed)
    presenter.attach(ReviewService(store, store.load()))
    return presenter, view, reviewed


# --------------------------------------------------------------------------- #
# open / render
# --------------------------------------------------------------------------- #
def test_open_renders_filtered_rows_options_and_progress(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, _ = _presenter(tmp_workspace)

    presenter.open()

    # Default filter is needs-review-only: just Bob's flagged '"Hi,"' segment shows.
    assert [row.text for row in view.segments[-1]] == ['"Hi,"']
    # Progress reports the whole book: 1 of 4 segments still need review.
    assert view.progress[-1] == (1, 4)
    # Speaker options pushed (narrator first).
    assert [opt.display for opt in view.options[-1]] == ["narrator", "Alice", "Bob"]


def test_structural_protocol_conformance() -> None:
    assert isinstance(FakeAttributionView(), AttributionView)


# --------------------------------------------------------------------------- #
# approve
# --------------------------------------------------------------------------- #
def test_approve_marks_approved_decrements_count_and_notifies(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    seg_id = _seg_id(presenter._service.project, '"Hi,"')  # type: ignore[union-attr]

    presenter.approve(seg_id)

    project = presenter._service.project  # type: ignore[union-attr]
    assert _find_segment(project, '"Hi,"').review_status == ReviewStatus.APPROVED
    assert view.progress[-1] == (0, 4)  # needs-review count decremented
    assert reviewed.count == 1  # live shell refresh fired
    assert len(view.segments) >= 2  # rows re-pushed after the edit
    # Persistence: a fresh load reflects the approval (not just the in-memory snapshot).
    assert _find_segment(tmp_workspace.load(), '"Hi,"').review_status == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# reassign to existing
# --------------------------------------------------------------------------- #
def test_reassign_existing_character_sets_speaker_role_and_approves(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    project = presenter._service.project  # type: ignore[union-attr]
    seg_id = _seg_id(project, '"Hi,"')
    alice_id = _speaker_id(project, "Alice")

    presenter.reassign_existing(seg_id, alice_id)

    seg = _find_segment(project, '"Hi,"')
    assert seg.speaker_id == alice_id
    assert seg.role == SpeakerRole.CHARACTER
    assert seg.review_status == ReviewStatus.APPROVED
    assert reviewed.count == 1


def test_reassign_to_narrator_option_uses_narrator_id_and_role(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, _, _ = _presenter(tmp_workspace)
    presenter.open()
    project = presenter._service.project  # type: ignore[union-attr]
    seg_id = _seg_id(project, '"Hi,"')
    narrator_id = _speaker_id(project, "narrator")

    presenter.reassign_existing(seg_id, narrator_id)

    seg = _find_segment(project, '"Hi,"')
    assert seg.speaker_id == narrator_id  # the real narrator id, not None
    assert seg.role == SpeakerRole.NARRATOR
    assert seg.review_status == ReviewStatus.APPROVED


def test_reassign_existing_unknown_speaker_id_errors(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    seg_id = _seg_id(presenter._service.project, '"Hi,"')  # type: ignore[union-attr]

    presenter.reassign_existing(seg_id, "spk_does_not_exist")

    assert view.errors  # surfaced, no crash
    assert reviewed.count == 0  # a failed edit does not tick the shell


# --------------------------------------------------------------------------- #
# reassign to a new speaker (introduces the documented voice blocker)
# --------------------------------------------------------------------------- #
def test_reassign_new_creates_character_and_introduces_voice_blocker(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, _, reviewed = _presenter(tmp_workspace)
    presenter.open()
    project = presenter._service.project  # type: ignore[union-attr]
    seg_id = _seg_id(project, '"Hi,"')

    presenter.reassign_new(seg_id, "Carol")

    carol = next(sp for sp in project.speakers if sp.name == "Carol")
    assert carol.role == SpeakerRole.CHARACTER
    seg = _find_segment(project, '"Hi,"')
    assert seg.speaker_id == carol.id
    assert seg.review_status == ReviewStatus.APPROVED
    # The new voiceless character is now a downstream (criterion-3) blocker, as documented.
    assert "Carol" in review_blockers(project).unassigned_voices
    assert reviewed.count == 1


def test_reassign_new_blank_name_errors_without_service_call(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    project = presenter._service.project  # type: ignore[union-attr]
    seg_id = _seg_id(project, '"Hi,"')
    before = len(project.speakers)

    presenter.reassign_new(seg_id, "   ")

    assert view.errors
    assert len(project.speakers) == before  # no empty-named speaker created
    assert reviewed.count == 0


# --------------------------------------------------------------------------- #
# reject
# --------------------------------------------------------------------------- #
def test_reject_marks_rejected_and_is_not_a_blocker(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    seg_id = _seg_id(presenter._service.project, '"Hi,"')  # type: ignore[union-attr]

    presenter.reject(seg_id)

    project = presenter._service.project  # type: ignore[union-attr]
    seg = _find_segment(project, '"Hi,"')
    assert seg.review_status == ReviewStatus.REJECTED
    assert seg.id not in review_blockers(project).needs_attribution  # REJECTED does not block
    assert view.progress[-1] == (0, 4)
    assert reviewed.count == 1


# --------------------------------------------------------------------------- #
# narrator-fallback (speaker_id=None) flip
# --------------------------------------------------------------------------- #
def _fallback_store(tmp_path: Path) -> tuple[WorkspaceStore, str]:
    """A saved project whose single flagged segment is a ``speaker_id=None`` narrator fallback."""
    from casttrophizer.workspace.layout import WorkspaceLayout

    narrator = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)
    alice = Speaker(id=new_id("spk"), name="Alice", role=SpeakerRole.CHARACTER)
    ch_id = new_id("ch")
    seg_id = new_id("seg")
    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text='"Who goes there?"',
        segments=[
            Segment(
                id=seg_id,
                text='"Who goes there?"',
                speaker_id=None,
                role=SpeakerRole.NARRATOR,
                confidence=0.0,
                review_status=ReviewStatus.NEEDS_REVIEW,
            )
        ],
    )
    chapter = Chapter(id=ch_id, order=0, title="Prologue", lines=[line])
    book = Book(
        title="Fallbacks",
        author="Test",
        source_ebook_path="/nowhere.epub",
        cover_image_path=None,
        chapters=[chapter],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Fallback Project",
        workspace_dir=str(tmp_path),
        book=book,
        speakers=[narrator, alice],
    )
    layout = WorkspaceLayout.for_dir(tmp_path / "fallback")
    layout.ensure_dirs()
    store = WorkspaceStore(layout)
    store.save(project)
    return store, seg_id


def test_reassign_flips_narrator_fallback_to_attributed(tmp_path: Path) -> None:
    store, seg_id = _fallback_store(tmp_path)
    presenter, _, reviewed = _presenter(store)
    presenter.open()
    project = presenter._service.project  # type: ignore[union-attr]
    alice_id = _speaker_id(project, "Alice")

    presenter.reassign_existing(seg_id, alice_id)

    seg = next(s for s in project.book.chapters[0].lines[0].segments if s.id == seg_id)
    assert seg.speaker_id == alice_id  # no longer the None fallback
    assert seg.review_status == ReviewStatus.APPROVED
    assert reviewed.count == 1


def test_reassign_new_flips_narrator_fallback_to_attributed(tmp_path: Path) -> None:
    store, seg_id = _fallback_store(tmp_path)
    presenter, _, _ = _presenter(store)
    presenter.open()
    project = presenter._service.project  # type: ignore[union-attr]

    presenter.reassign_new(seg_id, "Carol")

    seg = next(s for s in project.book.chapters[0].lines[0].segments if s.id == seg_id)
    assert seg.speaker_id is not None
    assert seg.review_status == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# filter
# --------------------------------------------------------------------------- #
def test_filter_toggles_between_flagged_only_and_all(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, _ = _presenter(tmp_workspace)
    presenter.open()
    assert [row.text for row in view.segments[-1]] == ['"Hi,"']  # flagged-only default

    presenter.set_filter(False)
    assert len(view.segments[-1]) == 4  # all four segments

    presenter.set_filter(True)
    assert [row.text for row in view.segments[-1]] == ['"Hi,"']


# --------------------------------------------------------------------------- #
# keyboard navigation
# --------------------------------------------------------------------------- #
def test_next_and_prev_flagged_select_expected_indices(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, _ = _presenter(tmp_workspace)
    presenter.open()
    presenter.set_filter(False)  # show all four; flagged is '"Hi,"' at index 2

    presenter.next_flagged()
    assert view.selected[-1] == 2  # first flagged from the top

    presenter.next_flagged()
    assert view.selected[-1] == 2  # only one flagged -> wraps back to itself

    presenter.prev_flagged()
    assert view.selected[-1] == 2


def test_next_flagged_noop_when_nothing_flagged(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, _ = _presenter(tmp_workspace)
    presenter.open()
    seg_id = _seg_id(presenter._service.project, '"Hi,"')  # type: ignore[union-attr]
    presenter.approve(seg_id)  # clear the only flag
    view.selected.clear()

    presenter.next_flagged()

    assert view.selected == []  # nothing to select


# --------------------------------------------------------------------------- #
# re-selection after an edit (fast-triage ergonomics)
# --------------------------------------------------------------------------- #
def _two_flag_store(tmp_path: Path) -> tuple[WorkspaceStore, str, str]:
    """A saved project with two NEEDS_REVIEW character segments (for reselection tests)."""
    from casttrophizer.workspace.layout import WorkspaceLayout

    narrator = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)
    bob = Speaker(id=new_id("spk"), name="Bob", role=SpeakerRole.CHARACTER)
    carol = Speaker(id=new_id("spk"), name="Carol", role=SpeakerRole.CHARACTER)
    alice = Speaker(id=new_id("spk"), name="Alice", role=SpeakerRole.CHARACTER)
    ch_id = new_id("ch")
    first_id = new_id("seg")

    def _flagged(seg_id: str, text: str, speaker: Speaker) -> Segment:
        return Segment(
            id=seg_id,
            text=text,
            speaker_id=speaker.id,
            role=SpeakerRole.CHARACTER,
            confidence=0.3,
            review_status=ReviewStatus.NEEDS_REVIEW,
        )

    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text='"one" "two"',
        segments=[
            _flagged(first_id, '"one,"', bob),
            _flagged(new_id("seg"), '"two,"', carol),
        ],
    )
    chapter = Chapter(id=ch_id, order=0, title="Chapter One", lines=[line])
    book = Book(
        title="Two Flags",
        author="Test",
        source_ebook_path="/nowhere.epub",
        cover_image_path=None,
        chapters=[chapter],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Two Flag Project",
        workspace_dir=str(tmp_path),
        book=book,
        speakers=[narrator, bob, carol, alice],
    )
    layout = WorkspaceLayout.for_dir(tmp_path / "twoflag")
    layout.ensure_dirs()
    store = WorkspaceStore(layout)
    store.save(project)
    return store, first_id, '"two,"'


def test_reselect_lands_on_next_flag_after_approve_in_filtered_mode(tmp_path: Path) -> None:
    store, first_id, second_text = _two_flag_store(tmp_path)
    presenter, view, _ = _presenter(store)
    presenter.open()  # needs-review-only default: two flagged rows
    presenter.set_selected(0)  # the panel selected the first flag

    presenter.approve(first_id)

    # The approved row left the filtered list; the cursor lands on the remaining flag (index 0),
    # and the presenter's own selection agrees with the highlight it pushed at the view.
    assert view.selected[-1] == 0
    assert [row.text for row in view.segments[-1]] == [second_text]
    assert presenter._selected == 0  # presenter/view selection stay in sync


# --------------------------------------------------------------------------- #
# missing segment id
# --------------------------------------------------------------------------- #
def test_edit_unknown_segment_id_errors(
    tmp_workspace: WorkspaceStore, review_ready_project: Project
) -> None:
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()

    presenter.approve("seg_missing")

    assert view.errors
    assert reviewed.count == 0

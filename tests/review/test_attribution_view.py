"""Pure derivation tests for :mod:`casttrophizer.review.attribution_view`.

Offline and Qt-free — no store, no presenter, no providers. Builds on the
``review_ready_project`` fixture (mixed blocker state) plus a crafted project with a
``speaker_id=None`` narrator-fallback segment.
"""

from __future__ import annotations

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
from casttrophizer.review.attribution_view import (
    SegmentRow,
    needs_review_count,
    segment_rows,
    speaker_options,
)


def _row_by_text(rows: list[SegmentRow], text: str) -> SegmentRow:
    return next(row for row in rows if row.text == text)


# --------------------------------------------------------------------------- #
# segment_rows over the mixed-blocker fixture
# --------------------------------------------------------------------------- #
def test_segment_rows_flattens_in_order(review_ready_project: Project) -> None:
    rows = segment_rows(review_ready_project)

    # Four segments across the single chapter's two lines, in reading order.
    assert [row.text for row in rows] == ['"Hello,"', "said Alice.", '"Hi,"', "Bob replied."]
    assert all(row.chapter_title == "Chapter One" for row in rows)
    assert all(row.chapter_index == 0 for row in rows)
    assert [row.line_order for row in rows] == [0, 0, 1, 1]


def test_segment_rows_resolves_speaker_display_and_flags(review_ready_project: Project) -> None:
    rows = segment_rows(review_ready_project)

    hello = _row_by_text(rows, '"Hello,"')
    assert hello.speaker_display == "Alice"
    assert hello.role == SpeakerRole.CHARACTER
    assert hello.confidence == 0.95
    assert hello.review_status == ReviewStatus.APPROVED
    assert not hello.needs_review
    assert not hello.is_narrator_fallback

    hi = _row_by_text(rows, '"Hi,"')
    assert hi.speaker_display == "Bob"
    assert hi.review_status == ReviewStatus.NEEDS_REVIEW
    assert hi.needs_review  # the attribution blocker
    assert not hi.is_narrator_fallback

    said = _row_by_text(rows, "said Alice.")
    assert said.speaker_display == "narrator"
    assert said.role == SpeakerRole.NARRATOR


def test_needs_review_count_counts_flagged_rows(review_ready_project: Project) -> None:
    rows = segment_rows(review_ready_project)
    assert needs_review_count(rows) == 1


def test_speaker_options_lists_narrator_first_then_characters(
    review_ready_project: Project,
) -> None:
    options = speaker_options(review_ready_project)

    assert [opt.display for opt in options] == ["narrator", "Alice", "Bob"]
    assert options[0].role == SpeakerRole.NARRATOR
    assert all(opt.role == SpeakerRole.CHARACTER for opt in options[1:])
    # Every option carries a real id (the narrator by its reserved id, never None).
    assert all(opt.speaker_id for opt in options)


# --------------------------------------------------------------------------- #
# narrator-fallback (speaker_id=None) + unknown-id derivation
# --------------------------------------------------------------------------- #
def _fallback_project() -> Project:
    """A project with a narrator, a NEEDS_REVIEW ``speaker_id=None`` quote, and a dangling id."""
    narrator = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)
    ch_id = new_id("ch")
    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text='"Who goes there?"',
        segments=[
            Segment(
                id=new_id("seg"),
                text='"Who goes there?"',
                speaker_id=None,  # narrator fallback, still flagged
                role=SpeakerRole.NARRATOR,
                confidence=0.0,
                review_status=ReviewStatus.NEEDS_REVIEW,
            ),
            Segment(
                id=new_id("seg"),
                text="a voice called.",
                speaker_id="spk_missing",  # dangling id -> defensive "<unknown>"
                role=SpeakerRole.CHARACTER,
                confidence=0.5,
                review_status=ReviewStatus.NEEDS_REVIEW,
            ),
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
    return Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Fallback Project",
        workspace_dir="/nowhere",
        book=book,
        speakers=[narrator],
    )


def test_segment_rows_flags_narrator_fallback() -> None:
    rows = segment_rows(_fallback_project())

    fallback = _row_by_text(rows, '"Who goes there?"')
    assert fallback.is_narrator_fallback
    assert fallback.needs_review
    assert fallback.speaker_display == "narrator (auto)"
    assert fallback.confidence == 0.0


def test_segment_rows_renders_unknown_speaker_id_defensively() -> None:
    rows = segment_rows(_fallback_project())

    dangling = _row_by_text(rows, "a voice called.")
    assert dangling.speaker_display == "<unknown>"
    assert not dangling.is_narrator_fallback  # a real (if missing) id, not the None fallback

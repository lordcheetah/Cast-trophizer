"""Pure, Qt-free view-model derivation for the attribution-review UI.

Mirroring :mod:`casttrophizer.review.gate`, this module holds the **derivation** that turns a
persisted :class:`~casttrophizer.domain.models.Project` into the flat, render-ready rows the
attribution-review surface displays — so the shapes are unit-testable without any presenter or
Qt, and reusable by both the presenter and the widget.

Pure / offline: no Qt, no providers, no I/O. It only *reads* the project; every mutation goes
through :class:`~casttrophizer.review.service.ReviewService`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import SegmentId, SpeakerId
from casttrophizer.domain.models import Project, Segment, Speaker, find_narrator

__all__ = [
    "SegmentRow",
    "SpeakerOption",
    "segment_rows",
    "speaker_options",
    "needs_review_count",
]


@dataclass(frozen=True)
class SegmentRow:
    """One flattened, render-ready segment row for the attribution list.

    ``needs_review`` mirrors ``review_status == NEEDS_REVIEW`` (the gate's criterion-1
    predicate) and ``is_narrator_fallback`` flags the ``speaker_id is None`` quotes that render
    as the narrator until the user attributes them — the two states the list surfaces most
    prominently.
    """

    segment_id: SegmentId
    chapter_index: int
    chapter_title: str
    line_order: int
    text: str
    speaker_display: str
    role: SpeakerRole
    confidence: float
    review_status: ReviewStatus
    needs_review: bool
    is_narrator_fallback: bool


@dataclass(frozen=True)
class SpeakerOption:
    """One entry for the reassign-to-existing combo (narrator first, then characters)."""

    speaker_id: SpeakerId
    display: str
    role: SpeakerRole


def _speaker_display(
    segment: Segment, narrator: Speaker | None, by_id: dict[SpeakerId, Speaker]
) -> str:
    """Human label for a segment's current speaker.

    A ``speaker_id is None`` segment renders as the reserved narrator (see
    :func:`~casttrophizer.domain.models.find_narrator`) but is *not yet attributed*, so it is
    tagged ``"<name> (auto)"``; a set id resolves to that speaker's name; an unknown id degrades
    to ``"<unknown>"`` defensively (a can't-happen state that must never crash the list).
    """
    if segment.speaker_id is None:
        name = narrator.name if narrator is not None else "narrator"
        return f"{name} (auto)"
    speaker = by_id.get(segment.speaker_id)
    return speaker.name if speaker is not None else "<unknown>"


def segment_rows(project: Project) -> list[SegmentRow]:
    """Flatten ``chapters -> lines -> segments`` (in order) into render-ready rows.

    Pure/offline; resolves each row's ``speaker_display`` via :func:`_speaker_display` so the
    narrator-fallback and unknown-id cases share the one labelling rule.
    """
    narrator = find_narrator(project)
    by_id = {sp.id: sp for sp in project.speakers}
    rows: list[SegmentRow] = []
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                rows.append(
                    SegmentRow(
                        segment_id=segment.id,
                        chapter_index=chapter.order,
                        chapter_title=chapter.title,
                        line_order=line.order,
                        text=segment.text,
                        speaker_display=_speaker_display(segment, narrator, by_id),
                        role=segment.role,
                        confidence=segment.confidence,
                        review_status=segment.review_status,
                        needs_review=segment.review_status == ReviewStatus.NEEDS_REVIEW,
                        is_narrator_fallback=segment.speaker_id is None,
                    )
                )
    return rows


def speaker_options(project: Project) -> list[SpeakerOption]:
    """The reassign-combo options: the narrator first, then every character speaker.

    The narrator is listed by its real id (never ``None``) so selecting it reassigns
    explicitly with ``role=NARRATOR`` rather than relying on the ``speaker_id=None`` fallback.
    """
    narrator = find_narrator(project)
    options: list[SpeakerOption] = []
    if narrator is not None:
        options.append(SpeakerOption(narrator.id, narrator.name, narrator.role))
    for speaker in project.speakers:
        if speaker.role == SpeakerRole.NARRATOR:
            continue  # the reserved narrator is already listed first
        options.append(SpeakerOption(speaker.id, speaker.name, speaker.role))
    return options


def needs_review_count(rows: Iterable[SegmentRow]) -> int:
    """How many of ``rows`` still need attribution review (criterion-1 blockers)."""
    return sum(1 for row in rows if row.needs_review)

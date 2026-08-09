"""Pure, Qt-free view-model derivation for the text-suggestion review UI.

The text-review analogue of :mod:`casttrophizer.review.attribution_view`: it turns a persisted
:class:`~casttrophizer.domain.models.Project` into the flat, render-ready rows the
text-suggestion surface displays — so the shapes are unit-testable without any presenter or Qt,
and reusable by both the presenter and the widget.

Pure / offline: no Qt, no providers, no I/O. It only *reads* the project; every mutation goes
through :class:`~casttrophizer.review.service.ReviewService`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.ids import LineId, SuggestionId
from casttrophizer.domain.models import Project

__all__ = ["SuggestionRow", "suggestion_rows", "pending_count"]


@dataclass(frozen=True)
class SuggestionRow:
    """One flattened, render-ready text-suggestion row for the review list.

    ``is_pending`` mirrors ``status == PENDING`` (the gate's criterion-2 predicate — the only
    status that blocks; ``AUTO_APPLIED`` / ``APPROVED`` / ``REJECTED`` are resolved).
    ``line_text`` is the line's **current** text (so the row shows full context and prefills the
    free-text line editor), while ``original``/``suggested`` are the specific token the
    suggestion swaps.
    """

    suggestion_id: SuggestionId
    line_id: LineId
    chapter_index: int
    chapter_title: str
    line_order: int
    line_text: str
    original: str
    suggested: str
    reason: str
    confidence: float
    status: ReviewStatus
    is_pending: bool


def suggestion_rows(project: Project) -> list[SuggestionRow]:
    """Flatten ``chapters -> lines -> suggestions`` (in order) into render-ready rows.

    Pure/offline; ``line_text`` is the line's current text so the row carries full-line context
    for display and for prefilling the whole-line editor.
    """
    rows: list[SuggestionRow] = []
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for suggestion in line.suggestions:
                rows.append(
                    SuggestionRow(
                        suggestion_id=suggestion.id,
                        line_id=line.id,
                        chapter_index=chapter.order,
                        chapter_title=chapter.title,
                        line_order=line.order,
                        line_text=line.text,
                        original=suggestion.original,
                        suggested=suggestion.suggested,
                        reason=suggestion.reason,
                        confidence=suggestion.confidence,
                        status=suggestion.status,
                        is_pending=suggestion.status == ReviewStatus.PENDING,
                    )
                )
    return rows


def pending_count(rows: Iterable[SuggestionRow]) -> int:
    """How many of ``rows`` are still pending (criterion-2 blockers)."""
    return sum(1 for row in rows if row.is_pending)

"""Review gate: the pure, Qt-free predicate that decides 'ready to synthesize'.

Both :class:`~casttrophizer.pipeline.stages.review.ReviewStage` and the future review
UI ("what's still blocking?") share this seam, mirroring how ``attribution/policy.py``
holds the attribute stage's pure logic.

The project is **review-complete** iff all three hold over the persisted state:

1. No :class:`~casttrophizer.domain.models.Segment` has
   ``review_status == ReviewStatus.NEEDS_REVIEW`` (a bare ``REJECTED`` does NOT block).
2. No :class:`~casttrophizer.domain.models.TextSuggestion` has
   ``status == ReviewStatus.PENDING`` (``AUTO_APPLIED`` already counts as resolved).
3. ``unresolved_voices(project) == []`` — every referenced speaker (narrator included)
   has an assigned, existing voice clip. This **reuses the synthesize precheck verbatim**
   (`casttrophizer.audio.synthesize.unresolved_voices`) so review and synthesize assert
   the same predicate with no drift.

Pure / offline: no Qt, no providers, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.models import Project

__all__ = ["ReviewBlockers", "review_blockers", "is_review_complete", "describe_blockers"]


@dataclass(frozen=True)
class ReviewBlockers:
    """The lists the review UI surfaces; empty everywhere == ready to synthesize."""

    needs_attribution: list[str] = field(default_factory=list)  # segment ids still NEEDS_REVIEW
    pending_suggestions: list[str] = field(default_factory=list)  # suggestion ids still PENDING
    unassigned_voices: list[str] = field(default_factory=list)  # speaker display names w/o voice

    @property
    def is_empty(self) -> bool:
        """True iff nothing is blocking review (all three lists empty)."""
        return not (self.needs_attribution or self.pending_suggestions or self.unassigned_voices)


def review_blockers(project: Project) -> ReviewBlockers:
    """Compute everything still blocking review, from persisted state (pure, offline)."""
    needs_attr = [
        seg.id
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    ]
    pending = [
        sug.id
        for ch in project.book.chapters
        for ln in ch.lines
        for sug in ln.suggestions
        if sug.status == ReviewStatus.PENDING
    ]
    voices = unresolved_voices(project)  # reuse synthesize precondition verbatim
    return ReviewBlockers(
        needs_attribution=needs_attr,
        pending_suggestions=pending,
        unassigned_voices=voices,
    )


def is_review_complete(project: Project) -> bool:
    """True iff no attribution, suggestion, or voice blockers remain."""
    return review_blockers(project).is_empty


def describe_blockers(blockers: ReviewBlockers) -> str:
    """Render a one-line human summary of what's still blocking review.

    e.g. ``"3 attributions, 1 text suggestion, voices needed for: narrator, Bob"``. An
    empty :class:`ReviewBlockers` renders ``"review complete"``.
    """
    if blockers.is_empty:
        return "review complete"

    parts: list[str] = []
    n_attr = len(blockers.needs_attribution)
    if n_attr:
        parts.append(f"{n_attr} attribution{'s' if n_attr != 1 else ''}")
    n_sug = len(blockers.pending_suggestions)
    if n_sug:
        parts.append(f"{n_sug} text suggestion{'s' if n_sug != 1 else ''}")
    if blockers.unassigned_voices:
        parts.append(f"voices needed for: {', '.join(blockers.unassigned_voices)}")
    return ", ".join(parts)

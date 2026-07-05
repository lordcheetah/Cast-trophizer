"""Gate-predicate unit tests: ``review_blockers`` / ``is_review_complete`` / ``describe_blockers``.

Pure and offline — no store, no Qt, no providers. Builds on the ``review_ready_project``
fixture (mixed blocker state) and the already-clean ``sample_project``.
"""

from __future__ import annotations

from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Project, Segment, Speaker, TextSuggestion, find_narrator
from casttrophizer.review import actions
from casttrophizer.review.gate import (
    describe_blockers,
    is_review_complete,
    review_blockers,
)


def _segment(project: Project, seg_id: str) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.id == seg_id
    )


def _line_of_suggestion(project: Project, sug_id: str):
    return next(
        ln
        for ch in project.book.chapters
        for ln in ch.lines
        if any(s.id == sug_id for s in ln.suggestions)
    )


def _speaker(project: Project, name: str) -> Speaker:
    return next(sp for sp in project.speakers if sp.name == name)


def _suggestion(project: Project, sug_id: str) -> TextSuggestion:
    return next(
        s
        for ch in project.book.chapters
        for ln in ch.lines
        for s in ln.suggestions
        if s.id == sug_id
    )


def test_review_blockers_reports_all_three(review_ready_project: Project) -> None:
    blockers = review_blockers(review_ready_project)

    assert len(blockers.needs_attribution) == 1  # the NEEDS_REVIEW Bob segment
    assert len(blockers.pending_suggestions) == 1  # the PENDING suggestion
    assert blockers.unassigned_voices == ["Bob"]  # Bob has no voice
    assert not blockers.is_empty


def test_resolved_items_do_not_block(review_ready_project: Project) -> None:
    """An AUTO_APPLIED suggestion and APPROVED segments are not reported as blockers."""
    blockers = review_blockers(review_ready_project)

    # Only the single PENDING suggestion is reported, not the AUTO_APPLIED one.
    pending = blockers.pending_suggestions
    for sug_id in pending:
        assert _suggestion(review_ready_project, sug_id).status.value == "pending"
    # Only the single NEEDS_REVIEW segment, not the APPROVED ones.
    for seg_id in blockers.needs_attribution:
        assert _segment(review_ready_project, seg_id).review_status.value == "needs_review"


def test_is_review_complete_false_until_all_cleared(review_ready_project: Project) -> None:
    project = review_ready_project
    assert is_review_complete(project) is False

    # 1. resolve the attribution blocker.
    blockers = review_blockers(project)
    actions.approve_attribution(_segment(project, blockers.needs_attribution[0]))
    assert is_review_complete(project) is False  # suggestion + voice still block

    # 2. resolve the pending suggestion.
    sug_id = review_blockers(project).pending_suggestions[0]
    actions.reject_suggestion(_line_of_suggestion(project, sug_id), sug_id)
    assert is_review_complete(project) is False  # voice still blocks

    # 3. assign Bob a voice (reuse an existing clip).
    bob = _speaker(project, "Bob")
    actions.assign_voice(bob, project.voice_clips[0])

    assert is_review_complete(project) is True
    final = review_blockers(project)
    assert final.is_empty
    # criterion-3 list agrees bit-for-bit with the synthesize precheck.
    assert final.unassigned_voices == unresolved_voices(project) == []


def test_unassigned_voices_matches_unresolved_voices(review_ready_project: Project) -> None:
    blockers = review_blockers(review_ready_project)
    assert blockers.unassigned_voices == unresolved_voices(review_ready_project)


def test_clean_project_completes_after_resolving_one_segment(sample_project: Project) -> None:
    """``sample_project`` has exactly one NEEDS_REVIEW segment and voices assigned."""
    assert is_review_complete(sample_project) is False
    blockers = review_blockers(sample_project)
    assert len(blockers.needs_attribution) == 1
    assert blockers.unassigned_voices == []

    actions.approve_attribution(_segment(sample_project, blockers.needs_attribution[0]))
    assert is_review_complete(sample_project) is True


def test_describe_blockers_summary(review_ready_project: Project) -> None:
    text = describe_blockers(review_blockers(review_ready_project))
    assert "1 attribution" in text
    assert "1 text suggestion" in text
    assert "voices needed for: Bob" in text


def test_describe_blockers_when_empty(sample_project: Project) -> None:
    blockers = review_blockers(sample_project)
    actions.approve_attribution(_segment(sample_project, blockers.needs_attribution[0]))
    assert describe_blockers(review_blockers(sample_project)) == "review complete"


def test_unvoiced_narrator_with_none_segment_names_narrator(review_ready_project: Project) -> None:
    """A renderable ``speaker_id=None`` segment + unvoiced narrator surfaces the narrator by name.

    Proves the gate string is fixed: ``describe_blockers`` reports ``voices needed for:
    narrator`` (never the old ``<unattributed>`` sentinel), so the run-gate hint and
    ``assign-voice --rest`` can voice it.
    """
    project = review_ready_project
    narrator = find_narrator(project)
    assert narrator is not None
    narrator.voice_clip_id = None  # narrator now unvoiced
    project.book.chapters[0].lines[0].segments.append(
        Segment(
            id=new_id("seg"),
            text="an orphan quote",
            speaker_id=None,
            role=SpeakerRole.NARRATOR,
            confidence=0.0,
            review_status=ReviewStatus.NEEDS_REVIEW,
        )
    )
    text = describe_blockers(review_blockers(project))
    assert "voices needed for:" in text
    assert "narrator" in text
    assert "<unattributed>" not in text

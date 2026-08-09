"""Action-layer gap tests: reassign reuse, narrator-by-None override, exact-count guarantees.

Complements ``test_actions.py`` with the cases the priority list calls out that the existing
suite does not isolate: ``reassign_segment_to_new_speaker`` *reusing* an existing speaker
(exactly one new CHARACTER ever created), ``set_segment_speaker(speaker_id=None)`` storing None
without raising (it renders as the narrator at synth), and reject_attribution leaving
speaker/role intact.
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.models import Project, Segment, Speaker
from casttrophizer.review import actions


def _needs_review_segment(project: Project) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == ReviewStatus.NEEDS_REVIEW
    )


def _speaker(project: Project, name: str) -> Speaker:
    return next(sp for sp in project.speakers if sp.name == name)


def test_reassign_creates_exactly_one_speaker_then_reuses(review_ready_project: Project) -> None:
    project = review_ready_project
    n_before = len(project.speakers)
    n_char_before = sum(1 for sp in project.speakers if sp.role == SpeakerRole.CHARACTER)

    seg = _needs_review_segment(project)
    first = actions.reassign_segment_to_new_speaker(seg, project, "Frank")
    assert len(project.speakers) == n_before + 1
    assert first.role == SpeakerRole.CHARACTER
    n_char_after = sum(1 for sp in project.speakers if sp.role == SpeakerRole.CHARACTER)
    assert n_char_after == n_char_before + 1

    # A second reassignment of a different segment to the same (case-folded) name REUSES it.
    other = project.book.chapters[0].lines[0].segments[0]
    second = actions.reassign_segment_to_new_speaker(other, project, "frank")
    assert second is first
    assert len(project.speakers) == n_before + 1  # no duplicate Speaker appended
    assert other.speaker_id == first.id


def test_set_segment_speaker_none_stores_none(review_ready_project: Project) -> None:
    """``speaker_id=None`` is allowed (no raise) and stores None (renders as narrator at synth)."""
    project = review_ready_project
    seg = _needs_review_segment(project)
    actions.set_segment_speaker(seg, project, speaker_id=None, role=SpeakerRole.NARRATOR)
    assert seg.speaker_id is None  # stored as None; resolves to the reserved narrator at render
    assert seg.review_status == ReviewStatus.APPROVED  # approve=True default still applies


def test_reject_attribution_preserves_speaker_and_role(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _needs_review_segment(project)
    speaker_before, role_before = seg.speaker_id, seg.role
    actions.reject_attribution(seg)
    assert seg.review_status == ReviewStatus.REJECTED
    assert (seg.speaker_id, seg.role) == (speaker_before, role_before)


def test_create_character_does_not_touch_segments(review_ready_project: Project) -> None:
    """``create_character`` only registers a Speaker; it repoints nothing."""
    project = review_ready_project
    segs_before = [
        (s.id, s.speaker_id) for ch in project.book.chapters for ln in ch.lines for s in ln.segments
    ]
    actions.create_character(project, "Grace")
    segs_after = [
        (s.id, s.speaker_id) for ch in project.book.chapters for ln in ch.lines for s in ln.segments
    ]
    assert segs_before == segs_after


# --------------------------------------------------------------------------- #
# per-segment audio review actions (pure)
# --------------------------------------------------------------------------- #
def _audio_segment(status: ReviewStatus) -> Segment:
    return Segment(
        id="seg-1",
        text="hi",
        speaker_id=None,
        role=SpeakerRole.NARRATOR,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
        audio_cache_key="k",
        audio_status=status,
    )


def test_approve_audio_sets_approved() -> None:
    seg = _audio_segment(ReviewStatus.COMPLETED)
    actions.approve_audio(seg)
    assert seg.audio_status == ReviewStatus.APPROVED
    assert seg.audio_cache_key == "k"  # approval never touches the render/key


def test_reroll_audio_sets_seed_clears_key_and_pends() -> None:
    seg = _audio_segment(ReviewStatus.COMPLETED)
    actions.reroll_audio(seg, seed=4242)
    assert seg.audio_seed == 4242
    assert seg.audio_cache_key is None
    assert seg.audio_status == ReviewStatus.PENDING

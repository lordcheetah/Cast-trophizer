"""Headless review package: the gate predicate + pure review actions + persistence service.

Qt-free and provider-free (the heavy SDKs/PySide6 never load here). This is the operation
layer the future PySide6 review UI is a thin caller over: the gate (`review_blockers` /
`is_review_complete`) answers "what's still blocking?", the actions mutate the project, and
:class:`ReviewService` persists each mutation.
"""

from __future__ import annotations

from casttrophizer.review.actions import (
    accept_suggestion,
    approve_attribution,
    assign_voice,
    assign_voice_by_ids,
    create_character,
    edit_line_text,
    reassign_segment_to_new_speaker,
    register_voice_clip,
    reject_attribution,
    reject_suggestion,
    set_segment_speaker,
    unassign_voice,
)
from casttrophizer.review.attribution_view import (
    SegmentRow,
    SpeakerOption,
    needs_review_count,
    segment_rows,
    speaker_options,
)
from casttrophizer.review.gate import (
    ReviewBlockers,
    describe_blockers,
    is_review_complete,
    review_blockers,
)
from casttrophizer.review.service import ReviewService

__all__ = [
    "ReviewBlockers",
    "review_blockers",
    "is_review_complete",
    "describe_blockers",
    "accept_suggestion",
    "reject_suggestion",
    "edit_line_text",
    "approve_attribution",
    "reject_attribution",
    "set_segment_speaker",
    "create_character",
    "reassign_segment_to_new_speaker",
    "register_voice_clip",
    "assign_voice",
    "assign_voice_by_ids",
    "unassign_voice",
    "ReviewService",
    "SegmentRow",
    "SpeakerOption",
    "segment_rows",
    "speaker_options",
    "needs_review_count",
]

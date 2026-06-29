"""Enumerations for the domain model.

All enums are :class:`enum.StrEnum` so their values serialize directly as strings in
``project.json`` and compare equal to plain string literals — keeping serialization
hand-written and dependency-free.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SpeakerRole", "StageName", "ReviewStatus"]


class SpeakerRole(StrEnum):
    """Whether a segment/speaker is the narrator or a named character."""

    NARRATOR = "narrator"
    CHARACTER = "character"


class StageName(StrEnum):
    """The ordered pipeline stages. Drives ``stage_status`` keys and resume logic."""

    PARSE = "parse"
    CORRECT = "correct"
    ATTRIBUTE = "attribute"
    REVIEW = "review"
    SYNTHESIZE = "synthesize"
    ASSEMBLE = "assemble"


class ReviewStatus(StrEnum):
    """Status shared by suggestions, segments, lines, and stage results.

    Not every value is meaningful in every context: e.g. stage results use
    ``COMPLETED`` / ``NEEDS_REVIEW`` / ``STOPPED`` / ``FAILED``, while text suggestions
    use ``AUTO_APPLIED`` / ``PENDING`` / ``APPROVED`` / ``REJECTED``.
    """

    PENDING = "pending"
    AUTO_APPLIED = "auto_applied"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"

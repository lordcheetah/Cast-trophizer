"""Enumerations for the domain model.

All enums are :class:`enum.StrEnum` so their values serialize directly as strings in
``project.json`` and compare equal to plain string literals — keeping serialization
hand-written and dependency-free.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SpeakerRole", "VoiceCategory", "StageName", "ReviewStatus"]


class SpeakerRole(StrEnum):
    """Whether a segment/speaker is the narrator or a named character."""

    NARRATOR = "narrator"
    CHARACTER = "character"


class VoiceCategory(StrEnum):
    """A speaker's voice bucket, used to assign category-appropriate default clips.

    A per-speaker property stamped by the post-attribution classification pass. ``UNKNOWN``
    is the fallback for the narrator, unnamed/ambiguous or non-human speakers, and any
    speaker classification could not confidently bucket. The finite set doubles as the
    ``castrun assign-voice --rest`` flag surface (``--man/--woman/--boy/--girl/--default``);
    extending the taxonomy later (e.g. fantasy/scifi variants) is a schema bump that adds
    members here plus new flags — isolated to this module and the CLI.
    """

    MAN = "man"
    WOMAN = "woman"
    BOY = "boy"
    GIRL = "girl"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> VoiceCategory:
        """Map an arbitrary/LLM-provided string to a member, defaulting to ``UNKNOWN``.

        Used at the provider->domain boundary so a malformed or unexpected category string
        never raises — an unrecognized value simply degrades to ``UNKNOWN`` (covered by
        ``--default`` under ``--rest``).
        """
        try:
            return cls(str(value).strip().casefold())
        except ValueError:
            return cls.UNKNOWN


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

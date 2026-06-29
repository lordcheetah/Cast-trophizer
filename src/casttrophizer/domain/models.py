"""Pure domain data shapes.

These are plain ``@dataclass`` value objects with **no I/O**, no provider imports, and
no Qt. Serialization lives in :mod:`casttrophizer.domain.serialization`, keeping the
shapes themselves trivially constructible and comparable (dataclass ``__eq__`` powers
the round-trip equality tests).

Per the project decisions, the **TTS render unit is the Segment**: ``audio_cache_key``
and ``audio_status`` live on :class:`Segment`, not :class:`Line`. A ``Line`` is the
renderable text unit (a paragraph or quote run); a ``Segment`` is the attributable
sub-unit (narration vs. an inline quote), and each segment is synthesized with its own
voice and stitched in order during assembly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import (
    ChapterId,
    LineId,
    ProjectId,
    SegmentId,
    SpeakerId,
    SuggestionId,
    VoiceClipId,
)

__all__ = [
    "TextSuggestion",
    "Segment",
    "Line",
    "Chapter",
    "Speaker",
    "VoiceClip",
    "Book",
    "Project",
]


@dataclass
class TextSuggestion:
    """A proposed text correction surfaced for user review.

    High-confidence corrections are applied automatically (``status`` =
    ``AUTO_APPLIED``); everything else is ``PENDING`` until the user accepts/rejects.
    """

    id: SuggestionId
    original: str
    suggested: str
    reason: str  # e.g. "ocr-artifact" | "spellcheck"
    confidence: float
    status: ReviewStatus


@dataclass
class Segment:
    """An attributable, separately-rendered unit within a line.

    The audio cache key + status live here because TTS renders per-segment.
    """

    id: SegmentId
    text: str
    speaker_id: SpeakerId | None  # resolves to Speaker.id; None == narrator
    role: SpeakerRole
    confidence: float  # attribution confidence; low => flagged in review
    review_status: ReviewStatus
    audio_cache_key: str | None = None  # -> AudioCache; None until synthesized
    audio_status: ReviewStatus = ReviewStatus.PENDING  # per-segment review


@dataclass
class Line:
    """One renderable text unit (a paragraph or quote run) made of ordered segments."""

    id: LineId
    chapter_id: ChapterId
    order: int
    text: str
    segments: list[Segment] = field(default_factory=list)
    suggestions: list[TextSuggestion] = field(default_factory=list)


@dataclass
class Chapter:
    """A chapter: an ordered collection of lines plus a display title."""

    id: ChapterId
    order: int
    title: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class Speaker:
    """A voice in the cast — the narrator or a named character.

    ``voice_clip_id`` is the user-assigned mapping to a :class:`VoiceClip`; ``None``
    until the user assigns one.
    """

    id: SpeakerId
    name: str  # "narrator" reserved; characters by display name
    role: SpeakerRole
    voice_clip_id: VoiceClipId | None = None


@dataclass
class VoiceClip:
    """A user-provided reference clip, referenced by absolute path (read-only input)."""

    id: VoiceClipId
    source_path: str  # READ-ONLY input path (never copied/mutated)
    label: str


@dataclass
class Book:
    """Parsed book metadata and chapter tree. Source paths are read-only inputs."""

    title: str
    author: str
    source_ebook_path: str  # READ-ONLY input
    cover_image_path: str | None = None
    chapters: list[Chapter] = field(default_factory=list)


@dataclass
class Project:
    """The top-level persisted project state.

    ``stage_status`` maps :class:`~casttrophizer.domain.enums.StageName` values to a
    :class:`~casttrophizer.domain.enums.ReviewStatus`; it drives pipeline resume.
    ``schema_version`` is bumped when the on-disk shape changes, triggering a migration
    hook in :mod:`casttrophizer.domain.serialization`.
    """

    schema_version: int
    id: ProjectId
    name: str
    workspace_dir: str
    book: Book
    speakers: list[Speaker] = field(default_factory=list)
    voice_clips: list[VoiceClip] = field(default_factory=list)
    stage_status: dict[str, ReviewStatus] = field(default_factory=dict)
    tts_params: dict[str, object] = field(default_factory=dict)

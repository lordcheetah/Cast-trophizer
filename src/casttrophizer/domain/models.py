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

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, VoiceCategory
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
    "find_narrator",
    "resolve_segment_speaker_id",
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
    # resolves to Speaker.id; None resolves to the reserved narrator at render
    # (see :func:`find_narrator`), so an unattributed quote is voiced by the narrator.
    speaker_id: SpeakerId | None
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
    until the user assigns one. ``category`` is the voice bucket stamped by the
    post-attribution classification pass (``UNKNOWN`` for the narrator, or when
    classification is unavailable/uncertain); it drives ``assign-voice --rest`` default-clip
    selection but never gates synthesis.
    """

    id: SpeakerId
    name: str  # "narrator" reserved; characters by display name
    role: SpeakerRole
    voice_clip_id: VoiceClipId | None = None
    category: VoiceCategory = VoiceCategory.UNKNOWN


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


def find_narrator(project: Project) -> Speaker | None:
    """Return the project's reserved narrator (first ``NARRATOR``-role speaker), or None.

    A pure, side-effect-free query — the shared lookup rule for "who is the narrator?".
    Identity is by ``role == SpeakerRole.NARRATOR`` (not by name), matching what
    :func:`casttrophizer.attribution.policy.ensure_narrator` creates and reuses; that
    function delegates here so the rule cannot drift. Lives in the domain layer so both
    ``audio/`` (voice resolution) and ``attribution/`` can call it without a cross-package
    dependency. Returns ``None`` only when no narrator has been reserved yet (pre-attribution,
    or a narrator-less project) — the synthesize layer treats that defensively.
    """
    for speaker in project.speakers:
        if speaker.role == SpeakerRole.NARRATOR:
            return speaker
    return None


def resolve_segment_speaker_id(segment: Segment, narrator: Speaker | None) -> SpeakerId | None:
    """Resolve a segment's *effective* speaker id — the one rule voice resolution shares.

    Returns ``segment.speaker_id`` when set; otherwise the reserved ``narrator``'s id (a
    ``speaker_id=None`` segment is voiced by the narrator at render), or ``None`` when no
    narrator exists (defensive can't-happen case). Every voice-resolution path — the
    precheck (:func:`~casttrophizer.audio.synthesize.unresolved_voices`), the render loop
    (:func:`~casttrophizer.audio.synthesize.synthesize_chapter`), and the cache key
    (:meth:`~casttrophizer.workspace.audio_cache.AudioCache.key_for`) — goes through here so
    the resolved voice cannot diverge between them. ``narrator`` is passed in (resolved once
    by the caller via :func:`find_narrator`) to avoid re-scanning per segment.
    """
    if segment.speaker_id is not None:
        return segment.speaker_id
    return narrator.id if narrator is not None else None

"""Pure, Qt-free review-action operations the UI (and :class:`ReviewService`) call.

Each function **mutates the in-memory** :class:`~casttrophizer.domain.models.Project`
(or one of its sub-objects) and performs **no I/O** — persistence is the caller's job
(:class:`casttrophizer.review.service.ReviewService` owns ``store.save``). This keeps the
operations trivially unit-testable without a store.

Speaker creation/lookup reuses ``attribution.policy.resolve_speaker``/``ensure_narrator``
so the one case-insensitive matching rule is shared (no second implementation).

**Known limitation (text-edit propagation DEFERRED):** ``accept_suggestion`` and
``edit_line_text`` mutate ``line.text`` *only*; ``line.segments`` (what the synthesize
stage actually speaks) are left untouched — the line is NOT re-segmented. Whether/how a
text edit propagates to the spanning segment is a later UI-phase design decision. A user
"fixing" a typo in review may therefore not change spoken audio yet.

**Cache consequence (NOTE only — not built here):** the attribution/voice actions change
inputs that feed :meth:`~casttrophizer.workspace.audio_cache.AudioCache.key_for` (a
segment's resolved ``voice_clip_id``). ``set_segment_speaker`` / ``reassign_*`` /
``assign_voice`` / ``unassign_voice`` therefore change that key, so the synthesize stage
re-renders only the affected segments on its next run. No review action touches the cache
or deletes WAVs — the key recomputation in ``synthesize_chapter`` is the entire
invalidation mechanism.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.attribution.policy import ensure_narrator, resolve_speaker
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Line,
    Project,
    Segment,
    Speaker,
    TextSuggestion,
    VoiceClip,
)

__all__ = [
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
]


# --------------------------------------------------------------------------- #
# text suggestions (§B.1)
# --------------------------------------------------------------------------- #
def _find_suggestion(line: Line, suggestion_id: str) -> TextSuggestion:
    for suggestion in line.suggestions:
        if suggestion.id == suggestion_id:
            return suggestion
    raise ValueError(f"no suggestion {suggestion_id!r} on line {line.id!r}")


def accept_suggestion(line: Line, suggestion_id: str) -> None:
    """Apply a suggestion by replacing its ``original`` token with ``suggested`` in the line.

    A surfaced (PENDING) suggestion stores the specific TOKEN being corrected in
    ``original``/``suggested`` (e.g. ``"narrarator"`` -> ``"narrator"``), NOT the whole line —
    so we replace the first occurrence of ``original`` within ``line.text`` rather than
    overwriting the whole line (which would drop the rest of the sentence). A whole-line
    suggestion (``original`` == the full line) still works: replacing it swaps the line.

    Mutates ``line.text`` only — ``line.segments`` are NOT re-derived (see module docstring:
    text-edit propagation is deferred). Raises ``ValueError`` if the suggestion id is not on
    the line.
    """
    suggestion = _find_suggestion(line, suggestion_id)
    line.text = line.text.replace(suggestion.original, suggestion.suggested, 1)
    suggestion.status = ReviewStatus.APPROVED


def reject_suggestion(line: Line, suggestion_id: str) -> None:
    """Reject a suggestion: ``line.text`` unchanged, status -> REJECTED.

    Raises ``ValueError`` if the suggestion id is not on the line.
    """
    suggestion = _find_suggestion(line, suggestion_id)
    suggestion.status = ReviewStatus.REJECTED


def edit_line_text(line: Line, new_text: str) -> None:
    """Set ``line.text`` to a user-typed value (free-form correction).

    Does NOT touch ``line.segments`` — editing a line's text after attribution does not
    auto-resegment (see module docstring). The text shown in review is ``line.text``;
    ``segment.text`` is what the synthesize stage renders.
    """
    line.text = new_text


# --------------------------------------------------------------------------- #
# attribution (§B.2)
# --------------------------------------------------------------------------- #
def approve_attribution(segment: Segment) -> None:
    """Confirm the proposed speaker: ``review_status`` -> APPROVED (speaker/role unchanged)."""
    segment.review_status = ReviewStatus.APPROVED


def reject_attribution(segment: Segment) -> None:
    """Reject the proposal without choosing a replacement: ``review_status`` -> REJECTED.

    A REJECTED segment is NOT a gate blocker (only NEEDS_REVIEW is); ``speaker_id``/``role``
    are left as-is. The intended UI flow is reject -> reassign so the segment gets a valid
    speaker again; if the rejected segment ends up with a voice-less speaker, the voice
    precheck (gate criterion 3) catches it.
    """
    segment.review_status = ReviewStatus.REJECTED


def set_segment_speaker(
    segment: Segment,
    project: Project,
    *,
    speaker_id: str | None,
    role: SpeakerRole,
    approve: bool = True,
) -> None:
    """Override the attribution: point ``segment`` at ``speaker_id`` + ``role``.

    ``speaker_id`` of ``None`` stores ``segment.speaker_id = None``, which **renders as the
    reserved narrator** at synthesis (see
    :func:`~casttrophizer.domain.models.find_narrator`); to attribute a specific character,
    pass that speaker's id. A non-None id must already exist in ``project.speakers`` (raises
    ``ValueError`` otherwise). By default the human's choice marks ``review_status`` APPROVED;
    pass ``approve=False`` to leave the status untouched (note: this neither approves nor flags —
    a still-``NEEDS_REVIEW`` segment keeps blocking the review gate).

    Cache consequence: changing the resolved speaker changes the segment's
    :meth:`AudioCache.key_for`, so the synthesize stage re-renders this segment.
    """
    if speaker_id is not None and not any(sp.id == speaker_id for sp in project.speakers):
        raise ValueError(f"no speaker {speaker_id!r} in project")
    segment.speaker_id = speaker_id
    segment.role = role
    if approve:
        segment.review_status = ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# reassign to a different / new speaker (§B.3)
# --------------------------------------------------------------------------- #
def create_character(project: Project, name: str) -> Speaker:
    """Register (or reuse) a CHARACTER speaker for ``name``.

    Delegates to ``attribution.policy.resolve_speaker`` so a case-insensitive existing
    match is reused (no duplicate) and an unknown name appends a new CHARACTER speaker —
    the one matching rule the attribute stage uses.
    """
    narrator = ensure_narrator(project)
    return resolve_speaker(project, narrator, name)


def reassign_segment_to_new_speaker(segment: Segment, project: Project, name: str) -> Speaker:
    """``create_character(name)`` then point ``segment`` at it (CHARACTER, APPROVED).

    Convenience for the UI "assign to new character" path. Returns the existing or
    newly-created :class:`Speaker`.
    """
    speaker = create_character(project, name)
    set_segment_speaker(
        segment,
        project,
        speaker_id=speaker.id,
        role=SpeakerRole.CHARACTER,
        approve=True,
    )
    return speaker


# --------------------------------------------------------------------------- #
# voice-clip registration & assignment (§B.4)
# --------------------------------------------------------------------------- #
def register_voice_clip(project: Project, source_path: str | Path, label: str) -> VoiceClip:
    """Register a user-picked clip as a READ-ONLY input and append it to ``project``.

    The file is referenced by absolute path and **never copied or mutated** (CLAUDE.md:
    source clips are read-only inputs). Raises ``ValueError`` immediately if the path does
    not exist, so a typo surfaces at assignment time rather than as a later synthesize
    precheck failure. Does not validate that the file is playable audio (the UI may
    pre-validate).
    """
    path = Path(source_path)
    if not path.is_file():
        raise ValueError(f"voice clip source path does not exist: {path}")
    # Store an ABSOLUTE path: a relative one would resolve against whatever CWD the app (or a
    # resumed run / the future UI) happens to have later, and the clip would read as missing.
    clip = VoiceClip(id=new_id("voice"), source_path=str(path.resolve()), label=label)
    project.voice_clips.append(clip)
    return clip


def assign_voice(speaker: Speaker, voice_clip: VoiceClip) -> None:
    """Map ``speaker`` (narrator OR character — identical handling) to ``voice_clip``.

    Cache consequence: changing a speaker's ``voice_clip_id`` changes
    :meth:`AudioCache.key_for` for all of that speaker's segments, so the synthesize stage
    re-renders them on its next run.
    """
    speaker.voice_clip_id = voice_clip.id


def assign_voice_by_ids(project: Project, *, speaker_id: str, voice_clip_id: str) -> None:
    """Lookup-and-assign convenience; raises ``ValueError`` if either id is absent."""
    speaker = next((sp for sp in project.speakers if sp.id == speaker_id), None)
    if speaker is None:
        raise ValueError(f"no speaker {speaker_id!r} in project")
    clip = next((vc for vc in project.voice_clips if vc.id == voice_clip_id), None)
    if clip is None:
        raise ValueError(f"no voice clip {voice_clip_id!r} in project")
    assign_voice(speaker, clip)


def unassign_voice(speaker: Speaker) -> None:
    """Clear ``speaker.voice_clip_id`` (-> None).

    Re-introduces a voice blocker (gate criterion 3); the service must invalidate the
    review-complete flag (see :class:`ReviewService`).
    """
    speaker.voice_clip_id = None

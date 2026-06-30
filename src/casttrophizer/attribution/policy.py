"""Attribution policy: confidence thresholding, speaker registry, narrator reservation.

Pure, offline, deterministic, Qt-free. This is the one tested place for the
confidence threshold (mirrors how ``text/base.py`` centralizes its constants) and for
the candidate -> Segment status mapping and speaker resolution rules.

Decisions (CONFIRMED): threshold 0.75 (>= -> APPROVED, < -> NEEDS_REVIEW); a reserved
narrator Speaker exists and narrator segments point at it; each distinct returned
``speaker_name`` is its own ``CHARACTER`` Speaker (aliases deferred; case-insensitive
reuse only).
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Project, Speaker

__all__ = [
    "ATTRIBUTION_CONFIDENCE_THRESHOLD",
    "review_status_for",
    "ensure_narrator",
    "resolve_speaker",
]

#: Quote attributions at or above this confidence start APPROVED; below start
#: NEEDS_REVIEW. A single tunable v1 threshold — change it here, not at call sites.
ATTRIBUTION_CONFIDENCE_THRESHOLD = 0.75

#: Reserved narrator display name (case-folded for lookup).
_NARRATOR_NAME = "narrator"


def review_status_for(
    confidence: float, threshold: float = ATTRIBUTION_CONFIDENCE_THRESHOLD
) -> ReviewStatus:
    """Map an attribution confidence to its initial review status.

    ``confidence >= threshold`` -> ``APPROVED`` (high-confidence proposal, still
    user-overridable in review); below -> ``NEEDS_REVIEW`` (surfaced to the user).
    """
    return ReviewStatus.APPROVED if confidence >= threshold else ReviewStatus.NEEDS_REVIEW


def ensure_narrator(project: Project) -> Speaker:
    """Return the project's reserved narrator Speaker, creating one if absent.

    Finds the first ``NARRATOR``-role speaker; if none exists, appends a fresh
    ``Speaker(name="narrator", role=NARRATOR)`` to ``project.speakers``. Narrator
    segments point at this speaker so the synthesize stage has a place to hang the
    narrator voice clip.
    """
    for speaker in project.speakers:
        if speaker.role == SpeakerRole.NARRATOR:
            return speaker
    narrator = Speaker(id=new_id("spk"), name=_NARRATOR_NAME, role=SpeakerRole.NARRATOR)
    project.speakers.append(narrator)
    return narrator


def resolve_speaker(project: Project, narrator: Speaker, speaker_name: str | None) -> Speaker:
    """Resolve a returned ``speaker_name`` to a Speaker, creating a CHARACTER if new.

    ``None`` resolves to ``narrator``. An existing speaker whose name matches
    case-insensitively is reused (so "Alice" said twice yields one Speaker). An unknown
    name creates a new ``Speaker(role=CHARACTER, voice_clip_id=None)`` appended to
    ``project.speakers``. Aliases ("Darcy" vs "Mr. Darcy") are NOT merged in v1 — each
    distinct name is its own Speaker; merging is a later user-driven action.
    """
    if speaker_name is None:
        return narrator

    folded = speaker_name.casefold()
    for speaker in project.speakers:
        if speaker.name.casefold() == folded:
            return speaker

    character = Speaker(id=new_id("spk"), name=speaker_name, role=SpeakerRole.CHARACTER)
    project.speakers.append(character)
    return character

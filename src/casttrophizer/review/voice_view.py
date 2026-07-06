"""Pure, Qt-free view-model derivation for the voice-assignment review UI.

The voice-assignment analogue of :mod:`casttrophizer.review.attribution_view`: it turns a
persisted :class:`~casttrophizer.domain.models.Project` into the flat, render-ready rows the
voice-assignment surface displays — so the shapes are unit-testable without any presenter or
Qt, and reusable by both the presenter and the widget.

The load-bearing anti-drift rule: ``needs_voice`` comes **only** from
:func:`~casttrophizer.audio.synthesize.unresolved_speakers` and ``is_referenced`` **only** from
:func:`~casttrophizer.audio.synthesize.referenced_speaker_ids`. Neither the "has a usable
voice" check nor the "is referenced" walk is re-implemented here — this module just resolves
each speaker's clip label/path for display and marks membership in those two predicate sets.

Pure / offline: no Qt, no providers, no I/O beyond a single ``Path.is_file`` existence probe
per assigned clip (to surface the ``clip_file_missing`` row state); every mutation goes through
:class:`~casttrophizer.review.service.ReviewService`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from casttrophizer.audio.synthesize import referenced_speaker_ids, unresolved_speakers
from casttrophizer.domain.enums import SpeakerRole, VoiceCategory
from casttrophizer.domain.ids import SpeakerId
from casttrophizer.domain.models import Project, VoiceClip

__all__ = [
    "SpeakerVoiceRow",
    "CategoryOption",
    "speaker_voice_rows",
    "category_options",
    "needs_voice_count",
]


@dataclass(frozen=True)
class SpeakerVoiceRow:
    """One flattened, render-ready speaker row for the voice-assignment list.

    ``needs_voice`` mirrors membership in
    :func:`~casttrophizer.audio.synthesize.unresolved_speakers` (the gate's criterion-3
    predicate) and ``is_referenced`` membership in
    :func:`~casttrophizer.audio.synthesize.referenced_speaker_ids` — the two flags are read
    straight from the synthesize helpers so they cannot drift from the render precheck.
    ``voice_clip_path`` (the assigned clip's ``source_path``) is what the panel hands to
    ``play_clip`` for audition; ``clip_file_missing`` flags a set ``voice_clip_id`` whose file
    is absent (one reason a referenced speaker still ``needs_voice``).
    """

    speaker_id: SpeakerId
    name: str
    role: SpeakerRole
    category: VoiceCategory
    voice_clip_label: str | None
    voice_clip_path: str | None
    is_referenced: bool
    needs_voice: bool
    clip_file_missing: bool


@dataclass(frozen=True)
class CategoryOption:
    """One entry for a per-speaker category combo (one per :class:`VoiceCategory` member)."""

    value: VoiceCategory
    display: str


def speaker_voice_rows(project: Project) -> list[SpeakerVoiceRow]:
    """Flatten ``project.speakers`` into render-ready rows (one row per speaker, in order).

    ``needs_voice`` = membership in :func:`unresolved_speakers`; ``is_referenced`` = membership
    in :func:`referenced_speaker_ids` (both id-sets computed once by calling those helpers, never
    re-derived). The clip fields resolve ``voice_clip_id -> project.voice_clips``;
    ``clip_file_missing`` is True iff a clip id is set but its ``source_path`` file is absent.
    """
    voices: dict[str, VoiceClip] = {clip.id: clip for clip in project.voice_clips}
    referenced = set(referenced_speaker_ids(project))
    needs = {speaker.id for speaker in unresolved_speakers(project)}

    rows: list[SpeakerVoiceRow] = []
    for speaker in project.speakers:
        clip = voices.get(speaker.voice_clip_id) if speaker.voice_clip_id is not None else None
        clip_missing = clip is not None and not Path(clip.source_path).is_file()
        rows.append(
            SpeakerVoiceRow(
                speaker_id=speaker.id,
                name=speaker.name,
                role=speaker.role,
                category=speaker.category,
                voice_clip_label=clip.label if clip is not None else None,
                voice_clip_path=clip.source_path if clip is not None else None,
                is_referenced=speaker.id in referenced,
                needs_voice=speaker.id in needs,
                clip_file_missing=clip_missing,
            )
        )
    return rows


def category_options() -> list[CategoryOption]:
    """Every :class:`VoiceCategory` member as a combo option (value + its display string)."""
    return [CategoryOption(value=category, display=category.value) for category in VoiceCategory]


def needs_voice_count(rows: Iterable[SpeakerVoiceRow]) -> int:
    """How many of ``rows`` still need a voice (criterion-3 blockers).

    Equal to ``len(unresolved_speakers(project))`` for rows derived from the same project — the
    drift check the tests assert.
    """
    return sum(1 for row in rows if row.needs_voice)

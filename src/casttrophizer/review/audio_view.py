"""Pure, Qt-free view-model derivation for the per-segment audio-review UI.

The audio-review analogue of :mod:`casttrophizer.review.attribution_view`: it turns a persisted
:class:`~casttrophizer.domain.models.Project` (plus the workspace
:class:`~casttrophizer.workspace.audio_cache.AudioCache`) into the flat, render-ready rows the
per-segment audio-review surface displays — so the shapes are unit-testable without any presenter,
Qt, or audio device, and reusable by both the presenter and the widget.

The load-bearing anti-drift rule: ``is_rendered`` comes **only** from membership in
:data:`~casttrophizer.audio.synthesize.RENDERED_STATUSES` (the synth skip-gate's own predicate),
and ``speaker_display`` reuses :func:`~casttrophizer.review.attribution_view._speaker_display` so
the narrator-fallback label matches the attribution panel exactly. Playability is decided by the
one probe the panel needs — ``audio_cache_key`` is set **and** the WAV exists on disk
(:meth:`AudioCache.has`) — so a segment dirtied by a slice-4 edit (key cleared) or a missing file
reads as non-playable automatically.

Pure / offline: no Qt, no providers, no I/O beyond ``AudioCache``'s single ``Path.is_file``
existence probe per rendered segment; every mutation goes through
:class:`~casttrophizer.review.service.ReviewService`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from casttrophizer.audio.synthesize import RENDERED_STATUSES
from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.ids import SegmentId
from casttrophizer.domain.models import Project, find_narrator
from casttrophizer.review.attribution_view import _speaker_display
from casttrophizer.workspace.audio_cache import AudioCache

__all__ = [
    "AudioSegmentRow",
    "audio_segment_rows",
    "approved_count",
    "rendered_count",
]


@dataclass(frozen=True)
class AudioSegmentRow:
    """One flattened, render-ready segment row for the audio-review list.

    ``is_rendered`` mirrors membership in
    :data:`~casttrophizer.audio.synthesize.RENDERED_STATUSES` (COMPLETED or APPROVED — the two
    statuses the synth skip-gate treats as "already rendered & current"); ``is_approved`` /
    ``is_failed`` mirror the two terminal review states the list surfaces. ``is_playable`` is the
    panel's audition gate: the segment has a stored ``audio_cache_key`` **and** its WAV exists on
    disk, in which case ``wav_path`` is that file's path (else ``None``). A whitespace-only segment
    is never rendered/playable (nothing audible), so it is forced non-playable here.
    """

    segment_id: SegmentId
    chapter_index: int
    chapter_title: str
    line_order: int
    text: str
    speaker_display: str
    audio_status: ReviewStatus
    is_approved: bool
    is_failed: bool
    is_rendered: bool
    is_playable: bool
    wav_path: str | None


def audio_segment_rows(project: Project, cache: AudioCache) -> list[AudioSegmentRow]:
    """Flatten ``chapters -> lines -> segments`` (in order) into render-ready audio rows.

    Pure/offline: resolves each row's ``speaker_display`` via the shared
    :func:`~casttrophizer.review.attribution_view._speaker_display` rule (so the narrator-fallback
    and unknown-id labels match the attribution panel) and decides ``is_playable`` from
    ``segment.audio_cache_key`` + a single :meth:`AudioCache.has` probe. A whitespace-only segment
    contributes a non-playable, non-rendered row (nothing audible was ever synthesized for it).
    """
    narrator = find_narrator(project)
    by_id = {sp.id: sp for sp in project.speakers}
    rows: list[AudioSegmentRow] = []
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                is_whitespace = not segment.text.strip()
                key = segment.audio_cache_key
                is_playable = not is_whitespace and key is not None and cache.has(key)
                wav_path = str(cache.path_for_key(key)) if is_playable and key is not None else None
                rows.append(
                    AudioSegmentRow(
                        segment_id=segment.id,
                        chapter_index=chapter.order,
                        chapter_title=chapter.title,
                        line_order=line.order,
                        text=segment.text,
                        speaker_display=_speaker_display(segment, narrator, by_id),
                        audio_status=segment.audio_status,
                        is_approved=segment.audio_status == ReviewStatus.APPROVED,
                        is_failed=segment.audio_status == ReviewStatus.FAILED,
                        is_rendered=(
                            not is_whitespace and segment.audio_status in RENDERED_STATUSES
                        ),
                        is_playable=is_playable,
                        wav_path=wav_path,
                    )
                )
    return rows


def approved_count(rows: Iterable[AudioSegmentRow]) -> int:
    """How many of ``rows`` the user has approved (audio_status == APPROVED)."""
    return sum(1 for row in rows if row.is_approved)


def rendered_count(rows: Iterable[AudioSegmentRow]) -> int:
    """How many of ``rows`` are rendered (COMPLETED or APPROVED) — the audition denominator."""
    return sum(1 for row in rows if row.is_rendered)

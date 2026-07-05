"""Per-chapter synthesis orchestration: resolve voice -> cache-skip -> render -> stamp.

Keeps :class:`~casttrophizer.pipeline.stages.synthesize.SynthesizeStage` thin (mirrors how
``attribution/attribute.py`` holds the attribute stage's policy). Pure-ish on the in-memory
``Project`` — the stage owns persistence. Fully offline/unit-testable against a fake
``TTSProvider``; the real model is only ever reached via ``tts.synthesize``.

Two seams the stage drives:

``unresolved_voices(project)``
    The §2b fail-fast precheck. Returns the distinct **display names** of speakers that are
    referenced by some segment but have no usable voice clip (``voice_clip_id`` is None, the
    referenced :class:`VoiceClip` is missing, or its ``source_path`` file does not exist).
    The stage returns FAILED naming these before any TTS call, so a misconfigured project
    fails instantly instead of half-rendering.

``synthesize_chapter(...)``
    For each segment in a chapter: skip whitespace-only text; compute the cache key from live
    state via :meth:`AudioCache.key_for`; SKIP (no TTS) when the stored key matches AND the
    WAV exists on disk AND the segment is in a rendered status; otherwise render via
    ``tts.synthesize`` and stamp ``audio_cache_key``/``audio_status=COMPLETED``. A single
    segment's :class:`TTSProviderError` is flag-and-continue (that segment goes ``FAILED``,
    the rest keep rendering). Returns ``(rendered_any, failed_any)`` so the stage can apply
    the all-failed guard. ``should_stop`` is polled every :data:`STOP_POLL_INTERVAL`
    segments via an injected callback so stop stays responsive on a huge chapter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from casttrophizer.audio.loudness import LoudnessSettings, normalize_wav_file
from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.models import Chapter, Project, Speaker, VoiceClip
from casttrophizer.errors import TTSProviderError
from casttrophizer.providers.base import SynthesisRequest, TTSProvider
from casttrophizer.workspace.audio_cache import AudioCache

logger = logging.getLogger(__name__)

__all__ = [
    "STOP_POLL_INTERVAL",
    "RENDERED_STATUSES",
    "unresolved_voices",
    "unresolved_speakers",
    "synthesize_chapter",
]

#: How often (in segments) ``synthesize_chapter`` polls the stop callback, with a partial
#: save, so stop stays responsive even inside a single multi-thousand-segment chapter.
STOP_POLL_INTERVAL = 16

#: Audio-review statuses that count as "already rendered & current" for the skip gate. A
#: future per-line review stage promotes COMPLETED -> APPROVED; both are skippable. PENDING
#: and FAILED are "must render".
RENDERED_STATUSES: frozenset[ReviewStatus] = frozenset(
    {ReviewStatus.COMPLETED, ReviewStatus.APPROVED}
)


def _voice_index(project: Project) -> dict[str, VoiceClip]:
    """Build a ``{voice_clip_id: VoiceClip}`` index once per run."""
    return {clip.id: clip for clip in project.voice_clips}


def _speaker_index(project: Project) -> dict[str, Speaker]:
    """Build a ``{speaker_id: Speaker}`` index once per run."""
    return {speaker.id: speaker for speaker in project.speakers}


def _referenced_speaker_ids(project: Project) -> list[str]:
    """Distinct speaker ids referenced by any non-whitespace segment, in first-seen order."""
    seen: dict[str, None] = {}
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                if not segment.text.strip():
                    continue  # whitespace-only segments are never rendered (§7)
                if segment.speaker_id is not None and segment.speaker_id not in seen:
                    seen[segment.speaker_id] = None
    return list(seen)


def _resolved_clip_path(
    speaker: Speaker | None,
    voices: dict[str, VoiceClip],
) -> Path | None:
    """Resolve ``speaker -> voice_clip_id -> VoiceClip.source_path`` to an existing file.

    Returns the clip path only when the speaker has an assigned clip that exists in
    ``project.voice_clips`` and whose ``source_path`` file is present on disk; otherwise
    ``None`` (the precheck / per-segment failure path handles the gap).
    """
    if speaker is None or speaker.voice_clip_id is None:
        return None
    clip = voices.get(speaker.voice_clip_id)
    if clip is None:
        return None
    path = Path(clip.source_path)
    if not path.is_file():
        return None
    return path


def unresolved_voices(project: Project) -> list[str]:
    """Return display names of referenced speakers lacking a usable voice clip (§2b precheck).

    A speaker is unresolved iff it is referenced by some renderable segment and any of:
    ``voice_clip_id`` is None, the referenced :class:`VoiceClip` is absent from
    ``project.voice_clips``, or its ``source_path`` file is missing on disk. Names are
    returned in first-referenced order, de-duplicated. An empty list means every referenced
    speaker can be synthesized. A referenced segment with ``speaker_id`` of ``None`` is also
    treated as unresolved (reported as ``"<unattributed>"``) since no voice can be resolved.
    """
    speakers = _speaker_index(project)
    voices = _voice_index(project)
    missing: dict[str, None] = {}  # ordered set of display names

    referenced_ids = _referenced_speaker_ids(project)
    for speaker_id in referenced_ids:
        speaker = speakers.get(speaker_id)
        if _resolved_clip_path(speaker, voices) is None:
            name = speaker.name if speaker is not None else speaker_id
            missing[name] = None

    # A renderable segment with no speaker at all cannot resolve a voice either.
    if _has_unattributed_renderable_segment(project):
        missing.setdefault("<unattributed>", None)

    return list(missing)


def unresolved_speakers(project: Project) -> list[Speaker]:
    """Return the referenced, currently-unvoiced :class:`Speaker` objects (§2b, object form).

    The Speaker-object analogue of :func:`unresolved_voices`: a speaker is included iff it is
    referenced by some renderable segment (so ``castrun assign-voice --rest`` targets exactly
    the speakers the review gate flags) and lacks a usable voice clip (per
    :func:`_resolved_clip_path`). Shares ``_referenced_speaker_ids`` + ``_resolved_clip_path``
    with :func:`unresolved_voices` so the two predicates cannot drift. First-referenced order,
    de-duplicated.

    Unlike :func:`unresolved_voices`, this **excludes** the ``<unattributed>`` sentinel (a
    renderable segment with ``speaker_id`` ``None`` has no Speaker to voice — ``--rest`` cannot
    fix it) and any referenced id that resolves to no Speaker object.
    """
    speakers = _speaker_index(project)
    voices = _voice_index(project)
    out: list[Speaker] = []
    seen: set[str] = set()
    for speaker_id in _referenced_speaker_ids(project):
        speaker = speakers.get(speaker_id)
        if speaker is None or speaker.id in seen:
            continue
        if _resolved_clip_path(speaker, voices) is None:
            out.append(speaker)
            seen.add(speaker.id)
    return out


def _has_unattributed_renderable_segment(project: Project) -> bool:
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                if segment.speaker_id is None and segment.text.strip():
                    return True
    return False


def synthesize_chapter(
    chapter: Chapter,
    project: Project,
    tts: TTSProvider,
    cache: AudioCache,
    *,
    speakers: dict[str, Speaker],
    voices: dict[str, VoiceClip],
    advance: Callable[[str | None], None],
    should_stop: Callable[[], bool],
    save: Callable[[], None],
) -> tuple[bool, bool, bool]:
    """Render (or skip) every segment in ``chapter`` in place.

    For each segment: whitespace-only text is skipped (advance, no TTS); otherwise the cache
    key is recomputed from live state and the segment is skipped when the stored key matches,
    the WAV exists, and the status is rendered (§3a). Renderable segments call
    ``tts.synthesize``; success stamps ``audio_cache_key``/``audio_status=COMPLETED``, a
    :class:`TTSProviderError` flags that segment ``FAILED`` and continues (§5).

    Polls ``should_stop`` every :data:`STOP_POLL_INTERVAL` segments, calling ``save`` for a
    partial persist before bailing. ``advance`` is called once per segment (rendered, skipped,
    or whitespace) so progress reflects total work.

    Returns ``(rendered_any, failed_any, stopped)``. ``stopped`` is True iff the loop bailed
    on a stop request (the stage then returns STOPPED). The provider escaping with a
    :class:`TTSProviderError` is caught per segment here and never propagates.
    """
    rendered_any = False
    failed_any = False
    processed = 0

    # Per-segment loudness normalization (confirmed decision): applied to each just-rendered
    # WAV. ``None`` => the project has no loudness block (older project) -> skip entirely.
    loudness = LoudnessSettings.from_params(project.tts_params)

    for line in chapter.lines:
        for segment in line.segments:
            if processed and processed % STOP_POLL_INTERVAL == 0 and should_stop():
                save()
                return rendered_any, failed_any, True
            processed += 1

            if not segment.text.strip():
                advance(None)  # nothing audible to render; leave audio_status untouched
                continue

            key = cache.key_for(segment, project)
            if (
                segment.audio_cache_key == key
                and segment.audio_status in RENDERED_STATUSES
                and cache.has(key)
            ):
                advance(None)  # cache hit: current key + existing file + rendered status
                continue

            speaker = speakers.get(segment.speaker_id) if segment.speaker_id else None
            clip_path = _resolved_clip_path(speaker, voices)
            if clip_path is None:
                # Voice vanished after the precheck (race): flag-and-continue.
                segment.audio_status = ReviewStatus.FAILED
                failed_any = True
                advance(line.text[:40] or None)
                continue

            request = SynthesisRequest(
                text=segment.text,
                voice_clip_path=clip_path,
                params=dict(project.tts_params),
            )
            try:
                tts.synthesize(request, cache.path_for_key(key))
            except TTSProviderError:
                # Clear any stale key so a FAILED segment never points at an old WAV the
                # assemble stage might stitch in. The skip gate already excludes FAILED, so
                # the segment re-renders next run regardless; this just removes the trap.
                segment.audio_cache_key = None
                segment.audio_status = ReviewStatus.FAILED
                failed_any = True
            else:
                # Normalize the just-rendered WAV to a consistent loudness before stamping the
                # segment COMPLETED. A SKIPPED (cache-hit) segment is never re-normalized — its
                # settings are in the cache key, so a settings change already forces a re-render.
                # Normalization must never fail an otherwise-good render (log-and-continue: the
                # segment stays COMPLETED with un-normalized audio).
                if loudness is not None and loudness.enabled:
                    try:
                        normalize_wav_file(cache.path_for_key(key), loudness)
                    except Exception:  # noqa: BLE001 - defensive; a normalize failure is non-fatal
                        logger.warning(
                            "loudness normalization failed for segment %s; keeping raw audio",
                            segment.id,
                            exc_info=True,
                        )
                segment.audio_cache_key = key
                segment.audio_status = ReviewStatus.COMPLETED
                rendered_any = True
            advance(line.text[:40] or None)

    return rendered_any, failed_any, False

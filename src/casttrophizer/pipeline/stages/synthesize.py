"""SynthesizeStage: per-segment TTS, cached.

Drives ``ctx.tts`` (the first stage that does, exactly as ``attribute`` drives ``ctx.llm``)
to render each attributed :class:`~casttrophizer.domain.models.Segment` to its own cached
WAV in the workspace :class:`~casttrophizer.workspace.audio_cache.AudioCache`, keyed by
``hash(segment_text + voice_clip_id + tts_params)``. Segments whose key already has audio on
disk (and a rendered status) are SKIPPED, so a restart re-scans and resumes rather than
re-rendering — the "full render is expensive" principle.

Policy (per the confirmed plan decisions):

* **Fail-fast voice precheck.** Before rendering anything, scan every segment; if any
  referenced speaker has no usable voice clip, return FAILED naming them, ``stage_status``
  left unset so it re-runs once voices are assigned. Nothing partial is rendered.
* **Idempotent cache skip.** A segment is skipped iff its recomputed key matches the stored
  ``audio_cache_key`` AND the WAV exists AND ``audio_status`` is rendered. Any text/voice/
  param edit changes the key and forces regeneration.
* **Flag-and-continue.** A per-segment TTS failure marks that segment ``FAILED`` and keeps
  going; the stage still returns COMPLETED — unless *every* attempted segment failed, then
  FAILED. ``ctx.tts is None`` / unavailable -> FAILED, status unset.

The orchestration (precheck + per-chapter render/skip loop) lives in
:mod:`casttrophizer.audio.synthesize` so this stage stays thin. Qt-free; no provider
construction (``ctx.tts`` is injected, mirroring ``SegmentAttributeStage``).
"""

from __future__ import annotations

from casttrophizer.audio.synthesize import (
    STOP_POLL_INTERVAL,
    synthesize_chapter,
    unresolved_voices,
)
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project, Speaker, VoiceClip
from casttrophizer.errors import TTSProviderError
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult
from casttrophizer.workspace.audio_cache import AudioCache

__all__ = ["SynthesizeStage", "STOP_POLL_INTERVAL"]


class SynthesizeStage(Stage):
    """Renders per-segment audio via TTS, skipping already-cached segments."""

    name = StageName.SYNTHESIZE

    def is_complete(self, project: Project) -> bool:
        """True once synthesize recorded COMPLETED in ``stage_status`` (purely status-driven)."""
        return project.stage_status.get(str(StageName.SYNTHESIZE)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Render every renderable segment, skipping cached ones, persist, record COMPLETED.

        None-guards/availability-guards ``ctx.tts`` (FAILED, status unset). Runs the voice
        precheck first (FAILED naming unresolved speakers, status unset). Iterates chapters as
        the coarse stop checkpoint with a per-chapter persist, and additionally polls
        ``should_stop`` every ``STOP_POLL_INTERVAL`` segments inside a chapter with a partial
        save. Per-segment TTS failures are flagged and skipped; an all-failed run returns
        FAILED.
        """
        if ctx.tts is None:
            return StageResult(self.name, ReviewStatus.FAILED, "no TTS provider configured")
        if not ctx.tts.is_available():
            return StageResult(
                self.name, ReviewStatus.FAILED, f"TTS provider {ctx.tts.name} unavailable"
            )

        missing = unresolved_voices(project)
        if missing:
            return StageResult(
                self.name,
                ReviewStatus.FAILED,
                f"assign voices first: {', '.join(missing)}",
            )

        cache = AudioCache(ctx.store.layout)
        speakers: dict[str, Speaker] = {sp.id: sp for sp in project.speakers}
        voices: dict[str, VoiceClip] = {vc.id: vc for vc in project.voice_clips}

        total = sum(len(ln.segments) for ch in project.book.chapters for ln in ch.lines)
        ctx.progress.set_total(total)

        any_ok = False
        any_fail = False
        try:
            for chapter in project.book.chapters:
                if ctx.progress.should_stop():
                    ctx.store.save(project)  # persist rendered-so-far; status stays unset
                    return StageResult(self.name, ReviewStatus.STOPPED, "stopped during synthesize")

                rendered, failed, stopped = synthesize_chapter(
                    chapter,
                    project,
                    ctx.tts,
                    cache,
                    speakers=speakers,
                    voices=voices,
                    advance=lambda message: ctx.progress.advance(1, message=message),
                    should_stop=ctx.progress.should_stop,
                    save=lambda: ctx.store.save(project),
                )
                any_ok = any_ok or rendered
                any_fail = any_fail or failed
                ctx.store.save(project)  # chapter-granular persist for crash/resume safety
                if stopped:
                    return StageResult(self.name, ReviewStatus.STOPPED, "stopped during synthesize")
        except TTSProviderError as exc:
            ctx.store.save(project)  # keep partial progress; re-runnable once provider is back
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        if any_fail and not any_ok:
            ctx.store.save(project)
            return StageResult(self.name, ReviewStatus.FAILED, "all segments failed to synthesize")

        project.stage_status[str(StageName.SYNTHESIZE)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, "segments synthesized")

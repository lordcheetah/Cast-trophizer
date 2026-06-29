"""SynthesizeStage: per-segment TTS, cached.

Uses ``ctx.tts`` to render each segment, writing audio into the
:class:`~casttrophizer.workspace.audio_cache.AudioCache` keyed by
``hash(segment_text + voice_clip_id + tts_params)``. Segments whose key already has
audio are skipped, so a restart re-scans and resumes rather than re-rendering. Feature
bodies are stubs in this skeleton.
"""

from __future__ import annotations

from casttrophizer.domain.enums import StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["SynthesizeStage"]


class SynthesizeStage(Stage):
    """Renders per-segment audio via TTS, skipping already-cached segments."""

    name = StageName.SYNTHESIZE

    def is_complete(self, project: Project) -> bool:
        raise NotImplementedError("SynthesizeStage.is_complete is not yet implemented")

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        raise NotImplementedError("SynthesizeStage.run is not yet implemented")

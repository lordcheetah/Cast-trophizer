"""SegmentAttributeStage: speaker tagging via the LLM.

Splits lines into attributable segments and uses ``ctx.llm`` to set each segment's
``speaker_id`` and ``confidence``. The LLM proposes; low-confidence attributions are
flagged for review and never silently committed. Feature bodies are stubs in this
skeleton.
"""

from __future__ import annotations

from casttrophizer.domain.enums import StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["SegmentAttributeStage"]


class SegmentAttributeStage(Stage):
    """Tags segments with their speaker via the configured LLM provider."""

    name = StageName.ATTRIBUTE

    def is_complete(self, project: Project) -> bool:
        raise NotImplementedError("SegmentAttributeStage.is_complete is not yet implemented")

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        raise NotImplementedError("SegmentAttributeStage.run is not yet implemented")

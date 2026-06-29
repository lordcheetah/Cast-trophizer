"""AssembleStage: stitch per-segment audio into the final M4B.

Uses :class:`~casttrophizer.audio.assembler.M4BAssembler` (which shells out to ffmpeg
and tags via mutagen) to produce the chaptered M4B with cover art. Feature bodies are
stubs in this skeleton.
"""

from __future__ import annotations

from casttrophizer.domain.enums import StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["AssembleStage"]


class AssembleStage(Stage):
    """Assembles the final M4B from cached per-segment audio."""

    name = StageName.ASSEMBLE

    def is_complete(self, project: Project) -> bool:
        raise NotImplementedError("AssembleStage.is_complete is not yet implemented")

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        raise NotImplementedError("AssembleStage.run is not yet implemented")

"""CorrectTextStage: automated text-correction pass.

Populates ``Line.suggestions`` and auto-applies only high-confidence fixes; everything
else is surfaced for the user to accept/reject. Feature bodies are stubs in this
skeleton.
"""

from __future__ import annotations

from casttrophizer.domain.enums import StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["CorrectTextStage"]


class CorrectTextStage(Stage):
    """Proposes text corrections; auto-applies only high-confidence ones."""

    name = StageName.CORRECT

    def is_complete(self, project: Project) -> bool:
        raise NotImplementedError("CorrectTextStage.is_complete is not yet implemented")

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        raise NotImplementedError("CorrectTextStage.run is not yet implemented")

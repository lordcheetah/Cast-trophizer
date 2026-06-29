"""ReviewStage: human review gate.

A mostly-no-op compute stage that returns ``NEEDS_REVIEW`` until the UI marks the
attribution/text as reviewed. Nothing irreversible happens here. Feature bodies are
stubs in this skeleton.
"""

from __future__ import annotations

from casttrophizer.domain.enums import StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["ReviewStage"]


class ReviewStage(Stage):
    """Gates the pipeline on human review of attribution and text edits."""

    name = StageName.REVIEW

    def is_complete(self, project: Project) -> bool:
        raise NotImplementedError("ReviewStage.is_complete is not yet implemented")

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        raise NotImplementedError("ReviewStage.run is not yet implemented")

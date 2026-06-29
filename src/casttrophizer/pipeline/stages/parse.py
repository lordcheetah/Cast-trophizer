"""ParseStage: ebook -> chapters/lines.

Uses an :class:`~casttrophizer.ebook.base.EbookParser` to fill the project's chapters
and lines. Feature bodies are stubs in this skeleton.
"""

from __future__ import annotations

from casttrophizer.domain.enums import StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["ParseStage"]


class ParseStage(Stage):
    """Parses the source ebook into chapters and lines."""

    name = StageName.PARSE

    def is_complete(self, project: Project) -> bool:
        raise NotImplementedError("ParseStage.is_complete is not yet implemented")

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        raise NotImplementedError("ParseStage.run is not yet implemented")

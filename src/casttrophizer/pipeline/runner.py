"""The ordered, resumable pipeline runner.

``Pipeline`` runs a list of stages in order. It loads the project from the store once,
skips stages whose ``is_complete`` returns ``True`` (resume), and stops early when a
stage returns a non-COMPLETED status (STOPPED / NEEDS_REVIEW / FAILED) or after an
optional ``until`` stage. State is persisted by the stages themselves, so a later call
resumes where the previous one left off.

This module is Qt-free and provider-free; providers reach stages via ``StageContext``.
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["Pipeline"]

#: Statuses that halt the run when returned by a stage.
_HALTING = frozenset({ReviewStatus.STOPPED, ReviewStatus.NEEDS_REVIEW, ReviewStatus.FAILED})


class Pipeline:
    """Runs an ordered list of resumable stages over a single project."""

    def __init__(self, stages: list[Stage]) -> None:
        self._stages = list(stages)

    @property
    def stages(self) -> list[Stage]:
        """The ordered stages this pipeline runs."""
        return list(self._stages)

    def next_stage(self, project: Project) -> Stage | None:
        """Return the first stage whose ``is_complete`` is ``False`` (drives 'Resume').

        ``None`` means every stage is complete.
        """
        for stage in self._stages:
            if not stage.is_complete(project):
                return stage
        return None

    def run(self, ctx: StageContext, *, until: StageName | None = None) -> StageResult:
        """Load the project and run each not-yet-complete stage in order.

        Stops early on a halting status (STOPPED / NEEDS_REVIEW / FAILED), after the
        ``until`` stage, or when all stages are complete. Returns the last stage's
        result; if there was nothing to do, returns a COMPLETED result for the final
        stage in the pipeline.
        """
        project = ctx.store.load()
        last: StageResult | None = None

        for stage in self._stages:
            if stage.is_complete(project):
                continue

            result = stage.run(project, ctx)
            last = result

            if result.status in _HALTING:
                return result

            if until is not None and stage.name == until:
                return result

        if last is not None:
            return last

        # Nothing ran (everything already complete). Report completion of the final stage.
        final = self._stages[-1].name if self._stages else StageName.ASSEMBLE
        return StageResult(stage=final, status=ReviewStatus.COMPLETED)

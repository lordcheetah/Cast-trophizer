"""ReviewStage: the human review gate between ``attribute`` and ``synthesize``.

A pure compute/gate stage — the cheapest in the pipeline. It needs neither ``ctx.llm`` nor
``ctx.tts`` and mutates no book content (nothing irreversible is automatic). It computes
:func:`~casttrophizer.review.gate.review_blockers` and either halts the runner
(``NEEDS_REVIEW``, while any attribution / text suggestion / voice assignment is unresolved)
or records the gate passed (``COMPLETED``).

The interactive *work* of resolving blockers happens in the (deferred) PySide6 review UI via
:class:`~casttrophizer.review.service.ReviewService`; this stage is only the gate. It is
idempotent and resumable for free: re-running with blockers still present returns
``NEEDS_REVIEW`` again; re-running once clear returns ``COMPLETED``.

``is_complete`` is purely ``stage_status``-driven like every other stage; ``run`` is the
single writer of ``stage_status[REVIEW] = COMPLETED`` and only writes it when the gate is
clear. Blocker-introducing review actions pop that flag (see ``ReviewService``) so a later
edit re-opens the gate; the synthesize voice precheck is the final backstop.
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult
from casttrophizer.review.gate import describe_blockers, review_blockers

__all__ = ["ReviewStage"]


class ReviewStage(Stage):
    """Gates the pipeline on human review of attribution, text edits, and voice assignment."""

    name = StageName.REVIEW

    def is_complete(self, project: Project) -> bool:
        """True once review recorded COMPLETED in ``stage_status`` (purely status-driven)."""
        return project.stage_status.get(str(StageName.REVIEW)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Halt (``NEEDS_REVIEW``) while blockers remain; record ``COMPLETED`` once clear.

        Performs no mutation of book content and no provider call. While any blocker remains
        it persists nothing new and returns ``NEEDS_REVIEW`` (the runner halts and the UI
        works through the blockers). Once clear, it sets ``stage_status[REVIEW]=COMPLETED``,
        persists, and returns ``COMPLETED``.
        """
        blockers = review_blockers(project)
        if not blockers.is_empty:
            return StageResult(self.name, ReviewStatus.NEEDS_REVIEW, describe_blockers(blockers))

        project.stage_status[str(StageName.REVIEW)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, "review complete")

"""CorrectTextStage: automated, offline, deterministic text-correction pass.

Runs after ``parse``: over every ``Line.text`` it auto-applies only high-confidence
typographic fixes (whitespace normalization) and surfaces everything lexical (spellcheck and
OCR character-confusion / de-hyphenation) as ``PENDING`` :class:`TextSuggestion`s for later
review. No LLM, no network, no Qt. ``Line.segments`` is left untouched (segmentation is the
attribute stage's job).

Resume/idempotency: a line that already carries a correction-reason suggestion has been
processed and is skipped, so a re-run (or resume after STOP) never double-applies or
re-surfaces. The stage returns ``COMPLETED`` even with pending suggestions left — the review
stage/UI handles those; correction does not gate the pipeline on a human.
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Line, Project
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult
from casttrophizer.text import (
    Corrector,
    FixCandidate,
    apply_fixes,
    build_protected_set,
    default_correctors,
    is_correction_suggestion,
)

__all__ = ["CorrectTextStage"]


class CorrectTextStage(Stage):
    """Proposes text corrections; auto-applies only high-confidence (whitespace) ones."""

    name = StageName.CORRECT

    def __init__(self, correctors: list[Corrector] | None = None) -> None:
        """``correctors`` overrides the default (dictionary-backed) set for test injection."""
        self._correctors = correctors

    def is_complete(self, project: Project) -> bool:
        """True once correct recorded COMPLETED in ``stage_status`` (purely status-driven)."""
        return project.stage_status.get(str(StageName.CORRECT)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Correct every line's text, persist, and record completion.

        Builds the protected allowlist and correctors once, iterates chapters (the stop
        checkpoint), and per line skips any already carrying a correction suggestion
        (idempotent). On a corrector/dictionary failure returns FAILED with ``stage_status``
        left unset so a fixed environment can re-run.
        """
        ctx.progress.message("Correcting text")

        try:
            protected = build_protected_set(project)
            correctors = self._correctors or default_correctors(protected)
        except Exception as exc:  # dictionary load / corrector init failure
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        chapters = project.book.chapters
        ctx.progress.set_total(len(chapters))

        try:
            for chapter in chapters:
                if ctx.progress.should_stop():
                    ctx.store.save(project)  # persist partial state; status stays unset
                    return StageResult(self.name, ReviewStatus.STOPPED, "stopped during correct")

                for line in chapter.lines:
                    if self._already_corrected(line):
                        continue  # idempotency: never re-process a corrected line
                    candidates: list[FixCandidate] = []
                    for corrector in correctors:
                        candidates += corrector.line_fixes(line.text, protected=protected)
                    apply_fixes(line, candidates)

                ctx.progress.advance(1, message=chapter.title)
        except Exception as exc:  # a corrector raising mid-pass — re-runnable
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        project.stage_status[str(StageName.CORRECT)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, "text corrected")

    @staticmethod
    def _already_corrected(line: Line) -> bool:
        """True if ``line`` already carries a correction suggestion (the idempotency key)."""
        return any(is_correction_suggestion(s) for s in line.suggestions)

"""SegmentAttributeStage: speaker tagging via the LLM.

Runs after ``correct``. For each chapter it segments the chapter's *unattributed* lines
offline (deterministic ``QuoteSegmenter``) into narration/quote ``Segment``s, then uses
``ctx.llm`` to attribute the quote segments to speakers. The LLM proposes; low-confidence
attributions are flagged ``NEEDS_REVIEW`` and never silently committed. Discovered character
names are registered in ``project.speakers``. Narration is the narrator by construction and
is never sent to the LLM.

On the pass that *finishes* attribution — after the chapter loop, immediately before
stamping ``stage_status[ATTRIBUTE]=COMPLETED`` — a one-shot classification pass buckets every
discovered CHARACTER speaker into a ``VoiceCategory`` via one ``ctx.llm.classify_speakers``
call (the narrator stays ``UNKNOWN``). That pass is soft: any provider error leaves categories
``unknown`` and never fails the (already-successful) attribution. It runs only on the finishing
pass, so a resumed / already-COMPLETED project is never reclassified.

Resume/idempotency: the idempotency key is ``Line.segments`` — a line that already has
segments is skipped (mirrors ``correct``'s skip-on-existing rule), so a re-run creates no
duplicate segments and makes no extra LLM calls. The stop checkpoint is per chapter, with a
partial persist, so a STOP resumes from the first chapter with unattributed lines.

Provider policy: if ``ctx.llm`` is None or unavailable, return FAILED with ``stage_status``
left unset (re-runnable). A malformed batch (handled inside the orchestration) is soft-
flagged ``NEEDS_REVIEW`` rather than failing the stage; only a genuinely unreachable
provider mid-run returns FAILED. The stage returns COMPLETED even with many segments left
``NEEDS_REVIEW`` — the dedicated review stage/UI resolves them. Qt-free; no provider
construction (``ctx.llm`` is injected).
"""

from __future__ import annotations

from casttrophizer.attribution import (
    ATTRIBUTION_CONFIDENCE_THRESHOLD,
    Segmenter,
    attribute_chapter,
    classify_speakers,
    default_segmenter,
    ensure_narrator,
)
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.errors import LLMProviderError
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["SegmentAttributeStage"]


class SegmentAttributeStage(Stage):
    """Tags segments with their speaker via the configured LLM provider."""

    name = StageName.ATTRIBUTE

    def __init__(
        self,
        segmenter: Segmenter | None = None,
        threshold: float = ATTRIBUTION_CONFIDENCE_THRESHOLD,
    ) -> None:
        """``segmenter`` overrides the default splitter (test injection); the LLM always
        comes from ``ctx.llm``. ``threshold`` tunes the APPROVED/NEEDS_REVIEW gate."""
        self._segmenter = segmenter or default_segmenter()
        self._threshold = threshold

    def is_complete(self, project: Project) -> bool:
        """True once attribute recorded COMPLETED in ``stage_status`` (purely status-driven)."""
        return project.stage_status.get(str(StageName.ATTRIBUTE)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Attribute every chapter's unattributed lines, persist per chapter, record COMPLETED.

        None-guards ``ctx.llm`` (FAILED, status unset). Iterates chapters as the stop
        checkpoint; per chapter, skips already-attributed lines and attributes the rest. On
        a genuinely unreachable provider mid-run, persists partial progress and returns
        FAILED (re-runnable). Malformed batches are soft-flagged inside the orchestration.
        """
        if ctx.llm is None:
            return StageResult(self.name, ReviewStatus.FAILED, "no LLM provider configured")
        if not ctx.llm.is_available():
            return StageResult(
                self.name, ReviewStatus.FAILED, f"LLM provider {ctx.llm.name} unavailable"
            )

        narrator = ensure_narrator(project)
        chapters = project.book.chapters
        ctx.progress.set_total(len(chapters))

        try:
            for chapter in chapters:
                if ctx.progress.should_stop():
                    ctx.store.save(project)  # persist completed chapters; status stays unset
                    return StageResult(self.name, ReviewStatus.STOPPED, "stopped during attribute")

                todo = [line for line in chapter.lines if not line.segments]
                if todo:
                    attribute_chapter(
                        chapter,
                        todo,
                        narrator,
                        project,
                        ctx.llm,
                        self._segmenter,
                        self._threshold,
                    )
                    ctx.store.save(project)  # chapter-granular persist for crash/resume safety
                ctx.progress.advance(1, message=chapter.title)
        except LLMProviderError as exc:
            ctx.store.save(project)  # keep partial progress; re-runnable once provider is back
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        # Finishing pass: one-shot classify discovered CHARACTER speakers into voice
        # categories, right before stamping COMPLETED. Soft — the orchestration swallows any
        # provider error and leaves categories `unknown`, so classification never fails an
        # otherwise-successful attribution. Runs only on this finishing pass, so a resumed
        # (already-COMPLETED) project is not reclassified.
        classify_speakers(project, ctx.llm)

        project.stage_status[str(StageName.ATTRIBUTE)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, "speakers attributed")

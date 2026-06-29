"""ParseStage: ebook -> chapters/lines.

Uses an :class:`~casttrophizer.ebook.base.EbookParser` to fill the project's chapters
and lines from its read-only source EPUB. Each :class:`~casttrophizer.domain.models.Line`
is created with **no segments** — segmentation/attribution belongs to the attribute
stage. The stage persists via ``ctx.store`` and records completion in
``project.stage_status`` so the pipeline can resume.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Chapter, Line, Project
from casttrophizer.ebook import EbookParser, parser_for
from casttrophizer.errors import EbookParseError
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult

__all__ = ["ParseStage"]


class ParseStage(Stage):
    """Parses the source ebook into chapters and lines."""

    name = StageName.PARSE

    def __init__(self, parser: EbookParser | None = None) -> None:
        """``parser`` overrides registry selection (used for test injection)."""
        self._parser = parser

    def is_complete(self, project: Project) -> bool:
        """True once parse recorded COMPLETED in ``stage_status`` (purely status-driven)."""
        return project.stage_status.get(str(StageName.PARSE)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Parse the source EPUB into ``project.book`` and persist it.

        Reports progress per chapter, polls ``should_stop`` between chapters, and on a
        parse error returns FAILED with ``stage_status`` left unset so a fixed input can
        re-run.
        """
        src = Path(project.book.source_ebook_path)
        parser = self._parser or parser_for(src)
        ctx.progress.message(f"Parsing {src.name}")

        try:
            parsed = parser.parse(src)
        except EbookParseError as exc:
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        ctx.progress.set_total(len(parsed.chapters))

        chapters: list[Chapter] = []
        for pch in parsed.chapters:
            if ctx.progress.should_stop():
                # Persist partial state and stop; status stays not-COMPLETED so the next
                # run re-parses (parse is cheap/deterministic) and completes.
                project.book.chapters = chapters
                ctx.store.save(project)
                return StageResult(self.name, ReviewStatus.STOPPED, "stopped during parse")

            chapter_id = new_id("ch")
            lines = [
                Line(
                    id=new_id("line"),
                    chapter_id=chapter_id,
                    order=i,
                    text=text,
                    segments=[],  # segmentation is the attribute stage's job
                )
                for i, text in enumerate(pch.lines)
            ]
            chapters.append(Chapter(id=chapter_id, order=pch.order, title=pch.title, lines=lines))
            ctx.progress.advance(1, message=pch.title)

        # Update metadata in place, preserving the read-only source path.
        project.book.title = parsed.title or project.book.title
        project.book.author = parsed.author or project.book.author
        project.book.chapters = chapters
        if parsed.cover_image_path is not None:
            project.book.cover_image_path = str(parsed.cover_image_path)

        project.stage_status[str(StageName.PARSE)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, f"{len(chapters)} chapters")

"""AssembleStage: stitch per-segment audio into the final M4B.

The final pipeline stage. It reads the per-segment WAVs the synthesize stage cached, computes
per-chapter timing offline, and hands a fully-computed
:class:`~casttrophizer.audio.assembler.AssemblyRequest` to
:class:`~casttrophizer.audio.assembler.M4BAssembler` (which shells out to ffmpeg and tags via
mutagen). It is the first stage that touches an external binary (``ffmpeg``, not a pip dep).

Policy (per the confirmed plan decisions):

* **ffmpeg guard.** If ffmpeg is not on PATH (``assembler.is_available()`` False) -> FAILED
  with a clear "install ffmpeg" message, ``stage_status`` left unset.
* **Fail-fast precheck.** Before producing anything, scan every renderable segment (same
  whitespace-skip rule as synthesize); if any lacks a COMPLETED/APPROVED ``audio_cache_key``
  whose WAV exists on disk -> FAILED naming how many/which are unrendered ("run synthesize
  first"), status unset, nothing produced.
* **Always rebuild on a direct ``run``** (the ``Pipeline`` path stays idempotent via the
  status-driven ``is_complete``). The M4B is one monolithic artifact — no per-segment caching.

The ordering/durations/markers/gap-WAV/request assembly lives in
:mod:`casttrophizer.audio.assemble` so this stage stays thin. Qt-free; the assembler is
injected via ``__init__(assembler=None)`` (test seam), defaulting to a real ``M4BAssembler``.
It needs neither ``ctx.tts`` nor ``ctx.llm``.
"""

from __future__ import annotations

from casttrophizer.audio.assemble import build_assembly_request, unrendered_segments
from casttrophizer.audio.assembler import M4BAssembler
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.errors import AssemblyError
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult
from casttrophizer.workspace.audio_cache import AudioCache

__all__ = ["AssembleStage"]

#: How many unrendered-segment identifiers to name in the FAILED message before truncating.
_MAX_NAMED_UNRENDERED = 5


class AssembleStage(Stage):
    """Assembles the final M4B from cached per-segment audio."""

    name = StageName.ASSEMBLE

    def __init__(self, assembler: M4BAssembler | None = None) -> None:
        """``assembler`` defaults to a real :class:`M4BAssembler`; tests inject a fake."""
        self._assembler = assembler or M4BAssembler()

    def is_complete(self, project: Project) -> bool:
        """True once assemble recorded COMPLETED in ``stage_status`` (purely status-driven)."""
        return project.stage_status.get(str(StageName.ASSEMBLE)) == ReviewStatus.COMPLETED

    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Build + encode the M4B, persist, record COMPLETED. Always rebuilds on a direct call.

        Guards ffmpeg availability, runs the fail-fast unrendered-segment precheck, builds the
        request offline (polling ``should_stop`` per chapter), then hands it to the assembler.
        FAILED on a missing ffmpeg / unrendered segments / no renderable audio / an
        :class:`AssemblyError`; STOPPED if a stop was requested during the offline build.
        """
        if not self._assembler.is_available():
            return StageResult(
                self.name,
                ReviewStatus.FAILED,
                "ffmpeg not found on PATH — install ffmpeg to assemble the M4B",
            )

        cache = AudioCache(ctx.store.layout)

        unrendered = unrendered_segments(project, cache)
        if unrendered:
            named = ", ".join(unrendered[:_MAX_NAMED_UNRENDERED])
            more = " …" if len(unrendered) > _MAX_NAMED_UNRENDERED else ""
            return StageResult(
                self.name,
                ReviewStatus.FAILED,
                f"{len(unrendered)} segments not rendered — run synthesize first: {named}{more}",
            )

        request, _chapter_count = build_assembly_request(
            project,
            cache,
            ctx.store.layout,
            progress=ctx.progress,
            should_stop=ctx.progress.should_stop,
        )
        if request is None:
            return StageResult(self.name, ReviewStatus.STOPPED, "stopped during assemble")
        if not request.segment_audio_paths:
            return StageResult(self.name, ReviewStatus.FAILED, "no rendered audio to assemble")

        ctx.progress.message("encoding M4B")
        try:
            out = self._assembler.assemble(request)
        except AssemblyError as exc:
            return StageResult(self.name, ReviewStatus.FAILED, str(exc))

        project.stage_status[str(StageName.ASSEMBLE)] = ReviewStatus.COMPLETED
        ctx.store.save(project)
        return StageResult(self.name, ReviewStatus.COMPLETED, f"assembled {out.name}")

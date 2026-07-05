"""Per-stage status rows — the read model both front-ends render.

Moved out of ``cli.cmd_status`` so the CLI status printout and the UI stage-status list
derive from one place. Pure and Qt-free: reads ``project.stage_status`` and the pipeline's
resume pointer (:meth:`Pipeline.next_stage`) with no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.runner import Pipeline

__all__ = ["StageRow", "next_stage_name", "stage_status_rows"]


@dataclass(frozen=True)
class StageRow:
    """One row of the stage-status view: the stage, its persisted status, and 'is next?'."""

    name: StageName
    status: ReviewStatus | None  # None => the stage has not recorded a status yet
    is_next: bool  # True for the single stage the next run would execute


def next_stage_name(project: Project, pipeline: Pipeline) -> StageName | None:
    """Name of the next not-yet-complete stage, or ``None`` when every stage is complete."""
    nxt = pipeline.next_stage(project)
    return nxt.name if nxt else None


def stage_status_rows(project: Project, pipeline: Pipeline) -> list[StageRow]:
    """Build one :class:`StageRow` per stage in pipeline order, flagging the next stage."""
    nxt = next_stage_name(project, pipeline)
    return [
        StageRow(
            name=stage,
            status=project.stage_status.get(str(stage)),
            is_next=stage == nxt,
        )
        for stage in StageName
    ]

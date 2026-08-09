"""Pipeline stage stubs and the canonical stage ordering.

``STAGE_ORDER`` is the source-of-truth sequence of stage classes; ``default_stages``
instantiates them in order for :class:`~casttrophizer.pipeline.runner.Pipeline`.
"""

from __future__ import annotations

from casttrophizer.pipeline.stage import Stage
from casttrophizer.pipeline.stages.assemble import AssembleStage
from casttrophizer.pipeline.stages.attribute import SegmentAttributeStage
from casttrophizer.pipeline.stages.correct import CorrectTextStage
from casttrophizer.pipeline.stages.parse import ParseStage
from casttrophizer.pipeline.stages.review import ReviewStage
from casttrophizer.pipeline.stages.synthesize import SynthesizeStage

__all__ = [
    "ParseStage",
    "CorrectTextStage",
    "SegmentAttributeStage",
    "ReviewStage",
    "SynthesizeStage",
    "AssembleStage",
    "STAGE_ORDER",
    "default_stages",
]

#: Canonical ordering of pipeline stages (parse -> ... -> assemble).
STAGE_ORDER: tuple[type[Stage], ...] = (
    ParseStage,
    CorrectTextStage,
    SegmentAttributeStage,
    ReviewStage,
    SynthesizeStage,
    AssembleStage,
)


def default_stages() -> list[Stage]:
    """Instantiate the stages in :data:`STAGE_ORDER`, ready for a ``Pipeline``."""
    return [cls() for cls in STAGE_ORDER]

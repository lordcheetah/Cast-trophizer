"""Resumable pipeline framework.

Qt-free by design: the stage framework, runner, and progress protocol have no Qt
imports, so parsing/attribution/synthesis can run off the Qt main thread (inside
``ui/workers.py``) while progress reaches the UI via a Qt adapter of
:class:`ProgressReporter`.
"""

from __future__ import annotations

from casttrophizer.pipeline.progress import NullProgressReporter, ProgressReporter
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import Stage, StageContext, StageResult
from casttrophizer.pipeline.stages import STAGE_ORDER, default_stages

__all__ = [
    "NullProgressReporter",
    "ProgressReporter",
    "Pipeline",
    "Stage",
    "StageContext",
    "StageResult",
    "STAGE_ORDER",
    "default_stages",
]

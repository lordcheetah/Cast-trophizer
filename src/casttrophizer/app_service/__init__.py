"""Qt-free, CLI-free application-service layer above pipeline/review/workspace.

Sits between the presentation front-ends (the ``castrun`` CLI and the PySide6 UI) and the
headless pipeline/review/workspace packages. It owns the orchestration both front-ends share
— project creation/loading, pipeline + provider construction, the run pass, and turning a
:class:`~casttrophizer.pipeline.stage.StageResult` into a structured
:class:`~casttrophizer.app_service.outcome.RunOutcome` and per-stage status rows — so the CLI
and UI never duplicate it and the UI never imports ``cli``.

Nothing here imports PySide6, so ``tests/test_qt_isolation.py`` covers the whole package.
"""

from __future__ import annotations

from casttrophizer.app_service.deps import (
    AppServiceDeps,
    default_llm_factory,
    default_tts_factory,
    tts_extra_available,
)
from casttrophizer.app_service.outcome import (
    RunOutcome,
    RunOutcomeKind,
    interpret_result,
)
from casttrophizer.app_service.pipeline_service import (
    build_pipeline,
    build_providers,
    run_pipeline,
)
from casttrophizer.app_service.projects import (
    ProjectExistsError,
    create_project,
    open_project,
    seed_tts_params,
)
from casttrophizer.app_service.status import (
    StageRow,
    next_stage_name,
    stage_status_rows,
)

__all__ = [
    "AppServiceDeps",
    "default_llm_factory",
    "default_tts_factory",
    "tts_extra_available",
    "RunOutcome",
    "RunOutcomeKind",
    "interpret_result",
    "build_pipeline",
    "build_providers",
    "run_pipeline",
    "ProjectExistsError",
    "create_project",
    "open_project",
    "seed_tts_params",
    "StageRow",
    "next_stage_name",
    "stage_status_rows",
]

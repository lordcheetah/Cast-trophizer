"""Pipeline stage framework: the ``Stage`` ABC, ``StageContext``, and ``StageResult``.

Stages are pure, resumable units that read/write **only** through the workspace store
and report progress through an injected reporter. They never construct providers or
import Qt — everything a stage needs is handed to it via :class:`StageContext`.

Resume model: each stage records its own completion in ``project.stage_status`` and
``is_complete`` reads that back, so :class:`~casttrophizer.pipeline.runner.Pipeline`
can skip finished stages. A stage MUST poll ``ctx.progress.should_stop()`` at safe
checkpoints and return a ``STOPPED`` result (with partial state already persisted) when
asked to stop, and MUST be idempotent so re-running after a stop continues rather than
restarts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.progress import ProgressReporter
from casttrophizer.providers.base import LLMProvider, TTSProvider
from casttrophizer.workspace.store import WorkspaceStore

if TYPE_CHECKING:
    from casttrophizer.config import AppConfig

__all__ = ["StageContext", "StageResult", "Stage"]


@dataclass
class StageContext:
    """Everything a stage needs, injected so stages stay provider- and Qt-agnostic."""

    store: WorkspaceStore
    progress: ProgressReporter
    llm: LLMProvider | None = None  # None when the stage doesn't need an LLM
    tts: TTSProvider | None = None  # None when the stage doesn't need TTS
    config: AppConfig | None = None


@dataclass
class StageResult:
    """The outcome of running a stage.

    ``status`` is one of COMPLETED / NEEDS_REVIEW / STOPPED / FAILED.
    """

    stage: StageName
    status: ReviewStatus
    message: str = ""


class Stage(ABC):
    """One resumable pipeline stage. Subclasses set :attr:`name`."""

    name: StageName

    @abstractmethod
    def is_complete(self, project: Project) -> bool:
        """True if persisted state shows this stage already finished — enables resume."""

    @abstractmethod
    def run(self, project: Project, ctx: StageContext) -> StageResult:
        """Do the work, mutating ``project`` and persisting it via ``ctx.store``.

        Must poll ``ctx.progress.should_stop()`` at safe checkpoints and return a STOPPED
        result (partial state already saved) when asked to stop. Must be idempotent /
        resumable: re-running after a stop continues, it does not restart.
        """

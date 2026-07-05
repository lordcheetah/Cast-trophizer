"""Build the pipeline + providers and run one pass — the shared run orchestration.

Moved out of ``cli.py`` (``_build_pipeline`` / ``_build_llm`` / ``_build_tts`` /
``_run_once``) so both front-ends drive the pipeline through one code path. This module is
Qt-free and CLI-free: preflight failures are raised as
:class:`~casttrophizer.errors.PreconditionError` / :class:`~casttrophizer.errors.ConfigError`
(never ``CliError`` or a Qt dialog), and each front-end maps them to its own presentation.

Provider construction is cheap — it imports no heavy SDK (``torch`` / ``anthropic`` load
lazily inside the provider methods) — so both providers are always built and injected. The
one fail-fast kept here is the **LLM key preflight**, and only while the attribute stage will
actually run this pass. The stages' own guards handle a genuinely unavailable TTS/ffmpeg
(SynthesizeStage / AssembleStage -> FAILED), which the front-end reports.
"""

from __future__ import annotations

from dataclasses import replace

from casttrophizer.app_service.deps import AppServiceDeps
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import StageName
from casttrophizer.errors import PreconditionError
from casttrophizer.pipeline.progress import ProgressReporter
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext, StageResult
from casttrophizer.pipeline.stages import (
    AssembleStage,
    CorrectTextStage,
    ParseStage,
    ReviewStage,
    SegmentAttributeStage,
    SynthesizeStage,
)
from casttrophizer.providers import LLMProvider, TTSProvider
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["build_pipeline", "build_providers", "run_pipeline"]


def build_pipeline(deps: AppServiceDeps) -> Pipeline:
    """Instantiate the six stages in order; AssembleStage gets its assembler via ``__init__``."""
    return Pipeline(
        [
            ParseStage(),
            CorrectTextStage(),
            SegmentAttributeStage(),
            ReviewStage(),
            SynthesizeStage(),
            AssembleStage(assembler=deps.assembler),
        ]
    )


def build_providers(
    deps: AppServiceDeps,
    config: AppConfig,
    *,
    provider: str | None,
    preflight: bool,
) -> tuple[LLMProvider, TTSProvider]:
    """Build both providers, optionally front-running the attribute stage's LLM-key guard.

    ``provider`` overrides ``config.llm_provider`` for the LLM (used by ``--provider``); the
    TTS provider is always built from the unmodified config. When ``preflight`` is set (the
    attribute stage *will* run this pass), an unavailable LLM raises
    :class:`~casttrophizer.errors.PreconditionError` with a clear "set your key" message
    before any stage runs. An unknown provider name surfaces as
    :class:`~casttrophizer.errors.ConfigError` from the factory. Never raises a CLI/Qt error.
    """
    cfg = replace(config, llm_provider=provider) if provider else config
    llm = deps.llm_factory(cfg)  # ConfigError (unknown provider) propagates to the caller
    if preflight and not llm.is_available():
        raise PreconditionError(
            "attribution needs an LLM: set ANTHROPIC_API_KEY "
            "(or use `--provider lmstudio` with LM Studio running)"
        )
    tts = deps.tts_factory(config)  # ConfigError (unknown provider) propagates to the caller
    return llm, tts


def run_pipeline(
    store: WorkspaceStore,
    pipeline: Pipeline,
    deps: AppServiceDeps,
    config: AppConfig,
    progress: ProgressReporter,
    *,
    provider: str | None = None,
    until: StageName | None = None,
) -> StageResult:
    """Decide the LLM preflight, build both providers, inject them, and run one pass.

    ``Pipeline.run`` advances through several stages in one call, so deciding *statically*
    which provider to build risks reaching a stage whose provider was never built — hence both
    are always built and injected. The LLM key preflight only fires while the attribute stage
    will actually run this pass: attribution is still pending AND ``--until`` does not stop
    before it. A completed attribution (or a ``--until parse``/``correct`` run) needs no key.
    """
    project = store.load()
    by_name = {stage.name: stage for stage in pipeline.stages}
    attribute_done = by_name[StageName.ATTRIBUTE].is_complete(project)

    order = [stage.name for stage in pipeline.stages]
    attribute_reachable = until is None or order.index(until) >= order.index(StageName.ATTRIBUTE)

    llm, tts = build_providers(
        deps,
        config,
        provider=provider,
        preflight=not attribute_done and attribute_reachable,
    )
    ctx = StageContext(store=store, progress=progress, llm=llm, tts=tts, config=config)
    return pipeline.run(ctx, until=until)

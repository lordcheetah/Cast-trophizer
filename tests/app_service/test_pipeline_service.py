"""``build_pipeline`` / ``build_providers`` / ``run_pipeline`` — construction + preflight."""

from __future__ import annotations

import pytest

from casttrophizer.app_service.deps import AppServiceDeps
from casttrophizer.app_service.pipeline_service import (
    build_pipeline,
    build_providers,
    run_pipeline,
)
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.errors import ConfigError, PreconditionError
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import (
    FakeLLMProvider,
    FakeM4BAssembler,
    FakeTTSProvider,
    RecordingProgressReporter,
)


def _deps(
    *,
    llm: FakeLLMProvider | None = None,
    tts: FakeTTSProvider | None = None,
    llm_raises: Exception | None = None,
) -> AppServiceDeps:
    def llm_factory(_config: AppConfig) -> FakeLLMProvider:
        if llm_raises is not None:
            raise llm_raises
        return llm or FakeLLMProvider()

    return AppServiceDeps(
        llm_factory=llm_factory,
        tts_factory=lambda _config: tts or FakeTTSProvider(),
        assembler=FakeM4BAssembler(),
        config=AppConfig(),
    )


def test_build_pipeline_has_six_stages_in_order() -> None:
    pipeline = build_pipeline(AppServiceDeps())
    assert [stage.name for stage in pipeline.stages] == list(StageName)


def test_build_providers_preflight_unavailable_llm_raises_precondition() -> None:
    deps = _deps(llm=FakeLLMProvider(available=False))
    with pytest.raises(PreconditionError, match="ANTHROPIC_API_KEY"):
        build_providers(deps, AppConfig(), provider=None, preflight=True)


def test_build_providers_skips_preflight_when_not_requested() -> None:
    deps = _deps(llm=FakeLLMProvider(available=False))
    llm, tts = build_providers(deps, AppConfig(), provider=None, preflight=False)
    assert not llm.is_available()  # unavailable, but no preflight -> no raise
    assert tts is not None


def test_build_providers_unknown_provider_raises_config_error() -> None:
    deps = _deps(llm_raises=ConfigError("unknown LLM provider: 'nope'"))
    with pytest.raises(ConfigError, match="unknown LLM provider"):
        build_providers(deps, AppConfig(), provider="nope", preflight=False)


def test_run_pipeline_advances_and_halts_at_review(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    """A full run over an attributed project halts at the review gate (voices unassigned)."""
    deps = _deps()
    pipeline = build_pipeline(deps)
    result = run_pipeline(
        tmp_workspace,
        pipeline,
        deps,
        AppConfig(),
        RecordingProgressReporter(),
    )
    assert result.status == ReviewStatus.NEEDS_REVIEW
    reloaded = tmp_workspace.load()
    assert reloaded.stage_status[str(StageName.ATTRIBUTE)] == ReviewStatus.COMPLETED


def test_run_pipeline_until_parse_needs_no_llm_key(
    tmp_workspace: WorkspaceStore, parse_ready_project: Project
) -> None:
    """`until=parse` must not preflight the LLM key (attribute never runs this pass)."""
    deps = _deps(llm=FakeLLMProvider(available=False))  # no key configured
    pipeline = build_pipeline(deps)
    result = run_pipeline(
        tmp_workspace,
        pipeline,
        deps,
        AppConfig(),
        RecordingProgressReporter(),
        until=StageName.PARSE,
    )
    # parse ran and the pass stopped before attribute — no PreconditionError, no stage FAILED.
    assert result.stage == StageName.PARSE
    assert result.status == ReviewStatus.COMPLETED

"""Provider factory tests: selection by config, ABC conformance, lazy concrete imports."""

from __future__ import annotations

import sys

import pytest

from casttrophizer.config import AppConfig
from casttrophizer.errors import ConfigError
from casttrophizer.providers import (
    LLMProvider,
    TTSProvider,
    build_llm_provider,
    build_tts_provider,
)


def test_build_llm_selects_claude() -> None:
    cfg = AppConfig(llm_provider="claude")
    provider = build_llm_provider(cfg)
    assert isinstance(provider, LLMProvider)
    assert provider.name == "claude"


def test_build_llm_selects_lmstudio() -> None:
    cfg = AppConfig(llm_provider="lmstudio")
    provider = build_llm_provider(cfg)
    assert isinstance(provider, LLMProvider)
    assert provider.name == "lmstudio"


def test_build_llm_provider_override_beats_config() -> None:
    cfg = AppConfig(llm_provider="claude")
    provider = build_llm_provider(cfg, provider="lmstudio")
    assert provider.name == "lmstudio"


def test_build_tts_selects_chatterbox() -> None:
    cfg = AppConfig(tts_provider="chatterbox")
    provider = build_tts_provider(cfg)
    assert isinstance(provider, TTSProvider)
    assert provider.name == "chatterbox"


def test_unknown_llm_provider_raises() -> None:
    with pytest.raises(ConfigError):
        build_llm_provider(AppConfig(llm_provider="nope"))


def test_unknown_tts_provider_raises() -> None:
    with pytest.raises(ConfigError):
        build_tts_provider(AppConfig(tts_provider="nope"))


def test_importing_providers_package_does_not_import_heavy_sdks() -> None:
    # Importing the factory package must not pull in anthropic/openai/torch/chatterbox.
    import casttrophizer.providers  # noqa: F401

    assert "anthropic" not in sys.modules
    assert "openai" not in sys.modules
    assert "torch" not in sys.modules
    assert "chatterbox" not in sys.modules


def test_importing_chatterbox_provider_module_does_not_import_torch() -> None:
    # The provider module must be import-cheap: torch loads only when synthesize runs.
    import casttrophizer.providers.tts.chatterbox  # noqa: F401

    assert "torch" not in sys.modules
    assert "chatterbox" not in sys.modules

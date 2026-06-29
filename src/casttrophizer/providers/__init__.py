"""Provider package: ABCs/DTOs plus the factory that builds concrete providers.

This ``__init__`` re-exports the abstractions from :mod:`casttrophizer.providers.base`
and exposes the two factory functions. **Only the factory imports concrete provider
modules**, and it does so lazily *inside* the function bodies — so importing
``casttrophizer.providers`` never pulls in ``anthropic``, ``openai``, ``torch``, or
``chatterbox``, and UI/pipeline code can depend on the ABCs alone.
"""

from __future__ import annotations

from casttrophizer.config import AppConfig
from casttrophizer.errors import ConfigError
from casttrophizer.providers.base import (
    AttributionCandidate,
    LLMMessage,
    LLMProvider,
    SynthesisRequest,
    SynthesisResult,
    TTSProvider,
)

__all__ = [
    "AttributionCandidate",
    "LLMMessage",
    "LLMProvider",
    "SynthesisRequest",
    "SynthesisResult",
    "TTSProvider",
    "build_llm_provider",
    "build_tts_provider",
]


def build_llm_provider(config: AppConfig, *, provider: str | None = None) -> LLMProvider:
    """Build the configured LLM provider.

    ``provider`` overrides ``config.llm_provider`` (used to construct the backup). The
    concrete provider modules are imported lazily here so this package stays import-cheap
    and the chosen SDK is only loaded when actually needed.
    """
    name = (provider or config.llm_provider).lower()
    if name == "claude":
        from casttrophizer.providers.llm.claude import ClaudeProvider

        return ClaudeProvider(model=config.claude_model, api_key=config.claude_api_key)
    if name == "lmstudio":
        from casttrophizer.providers.llm.lmstudio import LMStudioProvider

        return LMStudioProvider(model=config.lmstudio_model, base_url=config.lmstudio_base_url)
    raise ConfigError(f"unknown LLM provider: {name!r}")


def build_tts_provider(config: AppConfig, *, provider: str | None = None) -> TTSProvider:
    """Build the configured TTS provider (only Chatterbox in v1)."""
    name = (provider or config.tts_provider).lower()
    if name == "chatterbox":
        from casttrophizer.providers.tts.chatterbox import ChatterboxProvider

        return ChatterboxProvider()
    raise ConfigError(f"unknown TTS provider: {name!r}")

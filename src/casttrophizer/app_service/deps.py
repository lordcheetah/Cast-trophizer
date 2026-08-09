"""Dependency-injection seam for the application-service layer.

:class:`AppServiceDeps` is the Qt-free, CLI-free bundle every front-end (the ``castrun``
CLI and the PySide6 UI) hands to the shared orchestration functions so tests can inject
offline fakes for the LLM / TTS providers and the M4B assembler. Provider *factories* are
injected (not concrete providers) so construction stays lazy: the default factories call
:func:`~casttrophizer.providers.build_llm_provider` /
:func:`~casttrophizer.providers.build_tts_provider`, which import the heavy SDK only inside
the chosen provider's methods.

This module imports no Qt and no concrete provider SDK, so importing it is cheap.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from casttrophizer.config import AppConfig
from casttrophizer.providers import (
    LLMProvider,
    TTSProvider,
    build_llm_provider,
    build_tts_provider,
)

if TYPE_CHECKING:
    from casttrophizer.audio.assembler import M4BAssembler

__all__ = [
    "AppServiceDeps",
    "default_llm_factory",
    "default_tts_factory",
    "tts_extra_available",
]


def tts_extra_available() -> bool:
    """True iff the ``tts`` extra (Chatterbox) is importable — a cheap graceful-degradation probe.

    Uses :func:`importlib.util.find_spec` so it never actually imports Chatterbox/torch (the heavy
    model is loaded lazily inside the provider on the first ``synthesize``). The audio-review UI
    calls this to decide whether the in-session **regenerate-now** render is possible: when the
    extra is absent (CI, or a user without torch) regenerate is disabled while play/approve stay
    available. A ``find_spec`` that raises (a broken partial install) is treated as unavailable.
    """
    try:
        return importlib.util.find_spec("chatterbox") is not None
    except (ImportError, ValueError):
        return False


def default_llm_factory(config: AppConfig) -> LLMProvider:
    """Build the configured LLM provider (SDK imported lazily inside the factory)."""
    return build_llm_provider(config)


def default_tts_factory(config: AppConfig) -> TTSProvider:
    """Build the configured TTS provider (chatterbox imported lazily inside the factory)."""
    return build_tts_provider(config)


@dataclass
class AppServiceDeps:
    """Injected providers/assembler/config shared by every front-end orchestration call.

    Attributes:
        llm_factory: Builds the LLM provider from an :class:`AppConfig`.
        tts_factory: Builds the TTS provider from an :class:`AppConfig`.
        assembler: Optional :class:`M4BAssembler` injected into ``AssembleStage``; ``None``
            uses the real (ffmpeg-backed) assembler.
        config: The runtime config, or ``None`` to resolve from the environment lazily.
    """

    llm_factory: Callable[[AppConfig], LLMProvider] = default_llm_factory
    tts_factory: Callable[[AppConfig], TTSProvider] = default_tts_factory
    assembler: M4BAssembler | None = None
    config: AppConfig | None = None

    def resolved_config(self) -> AppConfig:
        """Return :attr:`config`, falling back to :meth:`AppConfig.from_env` when unset."""
        return self.config or AppConfig.from_env()

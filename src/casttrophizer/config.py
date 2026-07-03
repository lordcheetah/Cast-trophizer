"""Application configuration.

``AppConfig`` is a plain dataclass capturing provider selection, model ids, and
workspace defaults. It is intentionally Qt-free and provider-free: the provider
*factory* (``providers/__init__.py``) consumes it, but constructing a config never
imports a concrete provider. Values may be supplied directly or resolved from the
environment via :meth:`AppConfig.from_env`.

The Claude model id is overridable via env/config and is **not** hardcoded from
memory at the call site — the default below is the current Claude model id.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "LLMProviderName",
    "TTSProviderName",
    "DEFAULT_CLAUDE_MODEL",
    "DEFAULT_LMSTUDIO_MODEL",
    "DEFAULT_LMSTUDIO_BASE_URL",
    "AppConfig",
]

# Provider-selection literals. Kept as plain ``str`` constants (not enums) so config
# round-trips trivially through JSON/env without import-time coupling.
LLMProviderName = str
TTSProviderName = str

#: Current Claude model id (consult the ``claude-api`` skill before changing).
#: Overridable via the ``CASTTROPHIZER_CLAUDE_MODEL`` env var or the constructor.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"

#: LM Studio is local and model-name agnostic; this is a placeholder default.
DEFAULT_LMSTUDIO_MODEL = "local-model"

#: Default LM Studio OpenAI-compatible endpoint.
DEFAULT_LMSTUDIO_BASE_URL = "http://localhost:1234/v1"


@dataclass
class AppConfig:
    """User/runtime configuration for providers and workspace defaults.

    Attributes:
        llm_provider: Which LLM provider to build (``"claude"`` or ``"lmstudio"``).
        llm_backup_provider: Provider to fall back to when the primary is unavailable.
        tts_provider: Which TTS provider to build (only ``"chatterbox"`` for v1).
        claude_model: Claude model id (env-overridable; never hardcode at call sites).
        claude_api_key: Anthropic API key; ``None`` => read from the environment.
        lmstudio_model: Model name to request from LM Studio.
        lmstudio_base_url: LM Studio OpenAI-compatible base URL.
        workspaces_root: Default parent directory for new project workspaces.
        tts_params: Global synthesis defaults merged into per-segment requests.
        voice_defaults: Reusable per-category default voice-clip paths for
            ``assign-voice --rest``. Keys are ``VoiceCategory`` values (``"man"`` / ``"woman"``
            / ``"boy"`` / ``"girl"``) plus ``"default"`` (the ``unknown``/uncovered fallback);
            values are absolute clip paths. Populated from ``CASTTROPHIZER_VOICE_*`` env vars so
            a user sets category clips once instead of retyping them each run. Explicit
            ``--man/--woman/...`` flags override these; an absent flag falls back to the mapping.
    """

    llm_provider: LLMProviderName = "claude"
    llm_backup_provider: LLMProviderName = "lmstudio"
    tts_provider: TTSProviderName = "chatterbox"

    claude_model: str = DEFAULT_CLAUDE_MODEL
    claude_api_key: str | None = None

    lmstudio_model: str = DEFAULT_LMSTUDIO_MODEL
    lmstudio_base_url: str = DEFAULT_LMSTUDIO_BASE_URL

    workspaces_root: Path = field(
        default_factory=lambda: Path.home() / ".casttrophizer" / "workspaces"
    )

    tts_params: dict[str, object] = field(default_factory=dict)

    voice_defaults: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppConfig:
        """Build a config from environment variables, falling back to defaults.

        Recognized variables:
            ``CASTTROPHIZER_LLM_PROVIDER``, ``CASTTROPHIZER_LLM_BACKUP``,
            ``CASTTROPHIZER_TTS_PROVIDER``, ``CASTTROPHIZER_CLAUDE_MODEL``,
            ``ANTHROPIC_API_KEY``, ``CASTTROPHIZER_LMSTUDIO_MODEL``,
            ``CASTTROPHIZER_LMSTUDIO_BASE_URL``, ``CASTTROPHIZER_WORKSPACES_ROOT``,
            ``CASTTROPHIZER_VOICE_MAN`` / ``_WOMAN`` / ``_BOY`` / ``_GIRL`` / ``_DEFAULT``
            (reusable ``assign-voice --rest`` default clips -> :attr:`voice_defaults`).
        """
        src = os.environ if env is None else env
        cfg = cls()
        cfg.llm_provider = src.get("CASTTROPHIZER_LLM_PROVIDER", cfg.llm_provider)
        cfg.llm_backup_provider = src.get("CASTTROPHIZER_LLM_BACKUP", cfg.llm_backup_provider)
        cfg.tts_provider = src.get("CASTTROPHIZER_TTS_PROVIDER", cfg.tts_provider)
        cfg.claude_model = src.get("CASTTROPHIZER_CLAUDE_MODEL", cfg.claude_model)
        cfg.claude_api_key = src.get("ANTHROPIC_API_KEY", cfg.claude_api_key)
        cfg.lmstudio_model = src.get("CASTTROPHIZER_LMSTUDIO_MODEL", cfg.lmstudio_model)
        cfg.lmstudio_base_url = src.get("CASTTROPHIZER_LMSTUDIO_BASE_URL", cfg.lmstudio_base_url)
        if (root := src.get("CASTTROPHIZER_WORKSPACES_ROOT")) is not None:
            cfg.workspaces_root = Path(root)
        cfg.voice_defaults = _voice_defaults_from_env(src)
        return cfg


#: Maps a ``voice_defaults`` key to its ``CASTTROPHIZER_VOICE_*`` env var. Keys are
#: ``VoiceCategory`` values plus ``"default"`` (kept as plain strings so ``config`` stays
#: free of any domain import).
_VOICE_DEFAULT_ENV = {
    "man": "CASTTROPHIZER_VOICE_MAN",
    "woman": "CASTTROPHIZER_VOICE_WOMAN",
    "boy": "CASTTROPHIZER_VOICE_BOY",
    "girl": "CASTTROPHIZER_VOICE_GIRL",
    "default": "CASTTROPHIZER_VOICE_DEFAULT",
}


def _voice_defaults_from_env(src: Mapping[str, str]) -> dict[str, str]:
    """Collect the set ``CASTTROPHIZER_VOICE_*`` vars into a category->path mapping."""
    return {
        key: src[var] for key, var in _VOICE_DEFAULT_ENV.items() if src.get(var) not in (None, "")
    }

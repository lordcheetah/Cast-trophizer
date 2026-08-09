"""Provider factory tests: selection by config, ABC conformance, lazy concrete imports."""

from __future__ import annotations

import subprocess
import sys
import textwrap

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


def _assert_import_loads_no_sdks(module: str, forbidden: tuple[str, ...]) -> None:
    """Import ``module`` in a FRESH interpreter and assert no ``forbidden`` SDK loads.

    A clean subprocess (mirroring ``tests/test_lazy_imports.py``) is required: ``anthropic``
    is installed and another test in the same session may legitimately exercise a provider
    method that imports it (e.g. ``attribute_speakers`` references ``anthropic.AnthropicError``).
    An in-process ``not in sys.modules`` check would then fail purely on test ordering — a
    false positive about *this* module's import graph.
    """
    code = textwrap.dedent(f"""
        import sys, importlib
        importlib.import_module({module!r})
        leaked = [m for m in {forbidden!r} if m in sys.modules]
        if leaked:
            sys.stderr.write(f"{module} leaked: " + ", ".join(leaked) + "\\n")
            raise SystemExit(1)
        raise SystemExit(0)
        """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"{module} must import no heavy SDK.\nstderr:\n{proc.stderr}"


def test_importing_providers_package_does_not_import_heavy_sdks() -> None:
    # Importing the factory package must not pull in anthropic/openai/torch/chatterbox.
    _assert_import_loads_no_sdks(
        "casttrophizer.providers", ("anthropic", "openai", "torch", "chatterbox")
    )


def test_importing_chatterbox_provider_module_does_not_import_torch() -> None:
    # The provider module must be import-cheap: torch loads only when synthesize runs.
    _assert_import_loads_no_sdks("casttrophizer.providers.tts.chatterbox", ("torch", "chatterbox"))

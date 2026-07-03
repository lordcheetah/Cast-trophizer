"""Shared helpers for the CLI tests: a scripted fake LLM + a ``CliDeps`` builder.

Everything here is offline and deterministic — the CLI is driven end to end with the fake
LLM / TTS / M4B assembler injected via :class:`~casttrophizer.cli.CliDeps`, so no real API,
model, or ffmpeg is ever touched.
"""

from __future__ import annotations

from collections.abc import Callable

from casttrophizer.cli import CliDeps
from casttrophizer.config import AppConfig
from casttrophizer.providers import LLMProvider, TTSProvider
from tests.fakes import FakeLLMProvider, FakeM4BAssembler, FakeTTSProvider

#: Attribution script keyed by the quote text the ``[SEG <id>]`` context markers carry for
#: the ``make_sample_epub`` fixture. Alice is high-confidence (APPROVED); Bob is deliberately
#: low-confidence (< 0.75 -> NEEDS_REVIEW) so ``run --auto-accept`` has an attribution to
#: approve. Both are registered as CHARACTER speakers who then need a voice.
SAMPLE_TEXT_SCRIPT: dict[str, tuple[str | None, float]] = {
    '"Hello there,"': ("Alice", 0.9),
    '"And hello to you,"': ("Bob", 0.5),
    '"Are we there yet?"': ("Alice", 0.9),
}


def build_deps(
    *,
    llm_factory: Callable[[AppConfig], LLMProvider] | None = None,
    tts_factory: Callable[[AppConfig], TTSProvider] | None = None,
    assembler: FakeM4BAssembler | None = None,
    config: AppConfig | None = None,
) -> CliDeps:
    """Build a :class:`CliDeps` wired to offline fakes, overridable per test."""
    return CliDeps(
        llm_factory=llm_factory or (lambda cfg: FakeLLMProvider(text_script=SAMPLE_TEXT_SCRIPT)),
        tts_factory=tts_factory or (lambda cfg: FakeTTSProvider()),
        assembler=assembler if assembler is not None else FakeM4BAssembler(),
        config=config or AppConfig(),
    )

"""Offline, deterministic test doubles for LLM, TTS, and progress reporting."""

from __future__ import annotations

from tests.fakes.fake_corrector import (
    CountingCorrector,
    FakeSpellchecker,
    RaisingCorrector,
)
from tests.fakes.fake_llm import FakeLLMProvider
from tests.fakes.fake_progress import RecordingProgressReporter
from tests.fakes.fake_tts import FakeTTSProvider

__all__ = [
    "FakeLLMProvider",
    "FakeTTSProvider",
    "RecordingProgressReporter",
    "FakeSpellchecker",
    "CountingCorrector",
    "RaisingCorrector",
]

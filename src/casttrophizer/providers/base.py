"""Provider interfaces (ABCs) and their request/response DTOs.

UI and pipeline code depend **only** on these abstractions. Concrete providers
(Claude, LM Studio, Chatterbox) are constructed by the factory in
:mod:`casttrophizer.providers` and injected through the pipeline's ``StageContext`` —
they are never imported by UI or pipeline modules directly. This is what keeps the
providers swappable (local vs. API) without touching callers.

This module is import-cheap and Qt-free: it must not pull in ``anthropic``,
``openai``, ``torch``, or ``chatterbox`` at module load.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AttributionCandidate",
    "LLMMessage",
    "LLMProvider",
    "SynthesisRequest",
    "SynthesisResult",
    "TTSProvider",
]


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
@dataclass
class AttributionCandidate:
    """A proposed speaker for one segment, with a confidence the UI can flag.

    ``speaker_name`` is ``None`` to mean the narrator. Low ``confidence`` values are
    surfaced for user review — the LLM proposes, the user confirms.
    """

    segment_id: str
    speaker_name: str | None  # None => narrator
    confidence: float  # 0.0-1.0
    rationale: str = ""


@dataclass
class LLMMessage:
    """A single chat message for a generic LLM completion."""

    role: str  # "system" | "user" | "assistant"
    content: str


class LLMProvider(ABC):
    """Abstract LLM provider used for text correction and speaker attribution."""

    name: str

    @abstractmethod
    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        """Return a single completion for ``messages`` (text correction, generic prompts)."""

    @abstractmethod
    def attribute_speakers(
        self,
        *,
        context: str,
        candidates: list[str],
        known_speakers: list[str],
    ) -> list[AttributionCandidate]:
        """Propose a speaker (with confidence) for each segment id in ``candidates``.

        ``context`` is the surrounding text; ``known_speakers`` are display names already
        established. The LLM proposes; low-confidence results are surfaced for review.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap reachability/credentials check, so the UI can fall back primary -> backup."""


# --------------------------------------------------------------------------- #
# TTS
# --------------------------------------------------------------------------- #
@dataclass
class SynthesisRequest:
    """A request to synthesize one segment of audio from a reference voice clip."""

    text: str
    voice_clip_path: Path  # read-only reference clip to clone
    params: dict[str, Any] = field(default_factory=dict)  # exaggeration, cfg, seed, ...


@dataclass
class SynthesisResult:
    """The result of synthesizing one segment (written into the workspace audio cache)."""

    audio_path: Path
    sample_rate: int
    duration_s: float


class TTSProvider(ABC):
    """Abstract TTS provider that clones a reference voice to synthesize a segment."""

    name: str

    @abstractmethod
    def synthesize(self, request: SynthesisRequest, out_path: Path) -> SynthesisResult:
        """Generate audio for ``request`` to ``out_path``. Deterministic given the same seed."""

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap check that the provider can run (model present, deps importable)."""

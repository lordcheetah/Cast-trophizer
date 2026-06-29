"""Deterministic, offline fake LLM provider for tests.

``attribute_speakers`` returns scripted candidates (optionally including a
low-confidence one so the review path can be exercised); ``complete`` returns a canned
string. No network, no real model.
"""

from __future__ import annotations

from casttrophizer.providers.base import AttributionCandidate, LLMMessage, LLMProvider

__all__ = ["FakeLLMProvider"]


class FakeLLMProvider(LLMProvider):
    """A scriptable, deterministic stand-in for a real LLM provider."""

    name = "fake-llm"

    def __init__(
        self,
        *,
        completion: str = "FAKE COMPLETION",
        default_confidence: float = 0.95,
        available: bool = True,
    ) -> None:
        self._completion = completion
        self._default_confidence = default_confidence
        self._available = available
        self.complete_calls: list[list[LLMMessage]] = []
        self.attribute_calls: list[tuple[str, list[str], list[str]]] = []

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        self.complete_calls.append(list(messages))
        return self._completion

    def attribute_speakers(
        self,
        *,
        context: str,
        candidates: list[str],
        known_speakers: list[str],
    ) -> list[AttributionCandidate]:
        self.attribute_calls.append((context, list(candidates), list(known_speakers)))
        results: list[AttributionCandidate] = []
        for i, segment_id in enumerate(candidates):
            # Make the first candidate low-confidence so review paths are testable.
            confidence = 0.2 if i == 0 else self._default_confidence
            speaker = known_speakers[0] if known_speakers else None
            results.append(
                AttributionCandidate(
                    segment_id=segment_id,
                    speaker_name=speaker,
                    confidence=confidence,
                    rationale="scripted",
                )
            )
        return results

    def is_available(self) -> bool:
        return self._available

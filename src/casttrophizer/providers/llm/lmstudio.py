"""LM Studio LLM provider — backup (local, OpenAI-compatible endpoint).

A local model is a fallback, not the default, so the review/confirm UX must stay
strong enough that a weaker model is still usable. The ``openai`` SDK is imported
**lazily inside methods** to keep module load cheap.

Feature bodies are stubs (``raise NotImplementedError``) in this skeleton.
"""

from __future__ import annotations

from casttrophizer.providers.base import AttributionCandidate, LLMMessage, LLMProvider

__all__ = ["LMStudioProvider"]


class LMStudioProvider(LLMProvider):
    """LLM provider backed by LM Studio's local OpenAI-compatible endpoint."""

    name = "lmstudio"

    def __init__(self, model: str, base_url: str) -> None:
        self._model = model
        self._base_url = base_url

    def _client(self) -> object:
        """Lazily construct the OpenAI client pointed at the LM Studio endpoint."""
        import openai  # local import: keep module load cheap

        return openai.OpenAI(base_url=self._base_url, api_key="lm-studio")

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        raise NotImplementedError("LMStudioProvider.complete is not yet implemented")

    def attribute_speakers(
        self,
        *,
        context: str,
        candidates: list[str],
        known_speakers: list[str],
    ) -> list[AttributionCandidate]:
        raise NotImplementedError("LMStudioProvider.attribute_speakers is not yet implemented")

    def is_available(self) -> bool:
        raise NotImplementedError("LMStudioProvider.is_available is not yet implemented")

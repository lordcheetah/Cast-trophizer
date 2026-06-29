"""Claude (Anthropic) LLM provider — primary.

The heavy ``anthropic`` SDK is imported **lazily inside methods**, so importing this
module stays cheap and offline-safe; constructing the client only happens when the
provider actually runs. The model id and API key come from
:class:`~casttrophizer.config.AppConfig` (env-overridable) — never hardcoded here.

Feature bodies are stubs (``raise NotImplementedError``) in this skeleton.
"""

from __future__ import annotations

from casttrophizer.providers.base import AttributionCandidate, LLMMessage, LLMProvider

__all__ = ["ClaudeProvider"]


class ClaudeProvider(LLMProvider):
    """LLM provider backed by the Anthropic Claude API."""

    name = "claude"

    def __init__(self, model: str, api_key: str | None = None) -> None:
        self._model = model
        self._api_key = api_key

    def _client(self) -> object:
        """Lazily construct the Anthropic client (imports ``anthropic`` on first use)."""
        import anthropic  # local import: keep module load cheap and offline-safe

        return anthropic.Anthropic(api_key=self._api_key)

    def complete(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        raise NotImplementedError("ClaudeProvider.complete is not yet implemented")

    def attribute_speakers(
        self,
        *,
        context: str,
        candidates: list[str],
        known_speakers: list[str],
    ) -> list[AttributionCandidate]:
        raise NotImplementedError("ClaudeProvider.attribute_speakers is not yet implemented")

    def is_available(self) -> bool:
        raise NotImplementedError("ClaudeProvider.is_available is not yet implemented")

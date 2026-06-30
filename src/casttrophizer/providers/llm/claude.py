"""Claude (Anthropic) LLM provider — primary.

The heavy ``anthropic`` SDK is imported **lazily inside methods**, so importing this
module stays cheap and offline-safe; constructing the client only happens when the
provider actually runs. The model id and API key come from
:class:`~casttrophizer.config.AppConfig` (env-overridable) — never hardcoded here.

Attribution prompts the model for strict JSON and parses it into typed
``AttributionCandidate``s, enforcing one entry per requested segment id and mapping a
``null``/``"narrator"`` speaker to the narrator. Non-JSON / wrong-shape output raises
``LLMProviderError(malformed=True)`` so the attribution orchestration can retry-then-soft-
flag; HTTP/credential/connection failures raise a plain ``LLMProviderError`` (reachability)
so the stage returns FAILED.
"""

from __future__ import annotations

import json
from typing import Any

from casttrophizer.errors import LLMProviderError
from casttrophizer.providers.base import AttributionCandidate, LLMMessage, LLMProvider

__all__ = ["ClaudeProvider"]

#: System prompt for attribution. The user message carries the stage-built context; the
#: model returns strict JSON the provider parses (the stage never parses JSON itself).
_ATTRIBUTION_SYSTEM = (
    "You attribute spoken dialogue to its speaker in a work of fiction. You are given "
    "numbered lines of prose with certain quoted spans marked by a [SEG <id>] tag. For "
    "each marked SEG id, decide who speaks that quote, using dialogue tags (e.g. "
    "'said Alice') and adjacent narration as evidence. Prefer reusing a name from the "
    "provided known-speakers list when it clearly refers to the same character. Use the "
    "narrator only when the marked span is not character dialogue. Return strict JSON "
    "matching the requested schema: one entry per requested SEG id, with a confidence in "
    "[0,1]. Use null for the speaker to mean the narrator."
)

#: JSON schema constraining the response (structured outputs; one entry per SEG id).
_ATTRIBUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "attributions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_id": {"type": "string"},
                    "speaker": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["segment_id", "speaker", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["attributions"],
    "additionalProperties": False,
}

#: Speaker strings the model may use for the narrator (besides JSON null), case-folded.
_NARRATOR_ALIASES = frozenset({"narrator", "the narrator"})


class ClaudeProvider(LLMProvider):
    """LLM provider backed by the Anthropic Claude API."""

    name = "claude"

    def __init__(self, model: str, api_key: str | None = None) -> None:
        self._model = model
        self._api_key = api_key

    def _client(self) -> Any:
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
        """Return a single text completion for ``messages``.

        System messages are folded into the top-level ``system`` field; the rest become
        the ``messages`` array. ``temperature`` is accepted for interface compatibility but
        not forwarded — Opus 4.8 rejects sampling parameters.
        """
        import anthropic  # local import: keep module load cheap and offline-safe

        system = "\n\n".join(m.content for m in messages if m.role == "system") or None
        chat = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        try:
            response = self._client().messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=chat,
            )
        except anthropic.AnthropicError as exc:  # reachability/credentials/HTTP
            raise LLMProviderError(f"Claude completion failed: {exc}") from exc
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

    def attribute_speakers(
        self,
        *,
        context: str,
        candidates: list[str],
        known_speakers: list[str],
    ) -> list[AttributionCandidate]:
        """Attribute each requested segment id, returning one candidate per id.

        Prompts the model for strict JSON, parses it, enforces one-per-requested-id (a
        missing id defaults to a 0.0-confidence narrator candidate), drops unrequested ids,
        and maps ``null``/``"narrator"`` to the narrator. Non-JSON / wrong-shape output
        raises ``LLMProviderError(malformed=True)``; reachability failures raise a plain
        ``LLMProviderError``.
        """
        import anthropic  # local import: keep module load cheap and offline-safe

        known = ", ".join(known_speakers) if known_speakers else "(none yet)"
        user = f"{context}\n\nKnown characters: {known}"

        try:
            response = self._client().messages.create(
                model=self._model,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=_ATTRIBUTION_SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": _ATTRIBUTION_SCHEMA}},
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AnthropicError as exc:  # reachability/credentials/HTTP
            raise LLMProviderError(f"Claude attribution request failed: {exc}") from exc

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return self._parse_attributions(text, candidates)

    @staticmethod
    def _parse_attributions(text: str, candidates: list[str]) -> list[AttributionCandidate]:
        """Parse the model's JSON into one ``AttributionCandidate`` per requested id."""
        try:
            data = json.loads(text)
            entries = data["attributions"]
            if not isinstance(entries, list):
                raise TypeError("'attributions' is not a list")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LLMProviderError(
                f"Claude returned non-conforming attribution JSON: {exc}", malformed=True
            ) from exc

        requested = set(candidates)
        by_id: dict[str, AttributionCandidate] = {}
        for entry in entries:
            try:
                seg_id = str(entry["segment_id"])
            except (TypeError, KeyError) as exc:
                raise LLMProviderError(
                    f"attribution entry missing 'segment_id': {exc}", malformed=True
                ) from exc
            if seg_id not in requested:
                continue  # defensive: drop ids we did not request
            raw_speaker = entry.get("speaker")
            speaker_name: str | None
            if raw_speaker is None or str(raw_speaker).casefold() in _NARRATOR_ALIASES:
                speaker_name = None
            else:
                speaker_name = str(raw_speaker)
            by_id[seg_id] = AttributionCandidate(
                segment_id=seg_id,
                speaker_name=speaker_name,
                confidence=float(entry.get("confidence", 0.0)),
                rationale=str(entry.get("rationale", "")),
            )

        # Enforce one-per-requested-id: a missing id -> 0.0-confidence narrator default.
        return [
            by_id.get(
                seg_id,
                AttributionCandidate(
                    segment_id=seg_id, speaker_name=None, confidence=0.0, rationale="missing"
                ),
            )
            for seg_id in candidates
        ]

    def is_available(self) -> bool:
        """True if an API key is resolvable (so the UI can fall back primary -> backup).

        Cheap and offline: checks the constructor key or the ``ANTHROPIC_API_KEY`` env var.
        Importing ``anthropic`` stays lazy — availability does not construct a client or
        hit the network.
        """
        import os

        return bool(self._api_key or os.environ.get("ANTHROPIC_API_KEY"))

"""ClaudeProvider.attribute_speakers unit tests — offline, no real API.

The ``anthropic`` SDK call is stubbed by monkeypatching ``ClaudeProvider._client`` with a
fake whose ``messages.create`` returns canned text blocks, so we exercise the provider's JSON
parsing and contract enforcement WITHOUT any network or real model (the real SDK call shape is
verified separately by the main agent and must stay untested against a live API).

Covers: JSON -> AttributionCandidate mapping; ``null``/``"narrator"`` -> narrator; one entry
per requested id (missing -> 0.0 narrator default); unrequested ids dropped; non-JSON and
wrong-shape output -> ``LLMProviderError(malformed=True)``; ``is_available`` keyed off the
api key / env var. ``_parse_attributions`` is also exercised directly (it is the pure core).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from casttrophizer.errors import LLMProviderError
from casttrophizer.providers.llm.claude import ClaudeProvider


class _TextBlock:
    """Mimics an anthropic content block with ``type == 'text'``."""

    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.create_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> _Response:
        self.create_kwargs = kwargs
        return _Response(self._text)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


def _provider_returning(text: str) -> tuple[ClaudeProvider, _FakeClient]:
    """A ClaudeProvider whose lazily-built client returns ``text`` from messages.create."""
    provider = ClaudeProvider(model="claude-opus-4-8", api_key="test-key")
    client = _FakeClient(text)
    provider._client = lambda: client  # type: ignore[method-assign]
    return provider, client


# --------------------------------------------------------------------------- #
# happy-path parsing
# --------------------------------------------------------------------------- #
def test_parses_named_and_narrator_attributions() -> None:
    payload = json.dumps(
        {
            "attributions": [
                {"segment_id": "s1", "speaker": "Alice", "confidence": 0.9, "rationale": "tag"},
                {"segment_id": "s2", "speaker": None, "confidence": 0.5},
            ]
        }
    )
    provider, _client = _provider_returning(payload)
    out = provider.attribute_speakers(context="ctx", candidates=["s1", "s2"], known_speakers=[])

    assert [c.segment_id for c in out] == ["s1", "s2"]
    assert out[0].speaker_name == "Alice" and out[0].confidence == 0.9
    assert out[1].speaker_name is None  # null -> narrator
    assert out[1].confidence == 0.5


def test_literal_narrator_string_maps_to_narrator() -> None:
    payload = json.dumps(
        {"attributions": [{"segment_id": "s1", "speaker": "The Narrator", "confidence": 0.8}]}
    )
    provider, _client = _provider_returning(payload)
    out = provider.attribute_speakers(context="c", candidates=["s1"], known_speakers=[])
    assert out[0].speaker_name is None  # "the narrator" alias -> narrator


def test_missing_requested_id_defaults_to_zero_confidence_narrator() -> None:
    payload = json.dumps(
        {"attributions": [{"segment_id": "s1", "speaker": "Alice", "confidence": 0.9}]}
    )
    provider, _client = _provider_returning(payload)
    out = provider.attribute_speakers(context="c", candidates=["s1", "s2"], known_speakers=[])
    assert len(out) == 2
    missing = next(c for c in out if c.segment_id == "s2")
    assert missing.speaker_name is None and missing.confidence == 0.0


def test_unrequested_returned_id_is_dropped() -> None:
    payload = json.dumps(
        {
            "attributions": [
                {"segment_id": "s1", "speaker": "Alice", "confidence": 0.9},
                {"segment_id": "ghost", "speaker": "Ghost", "confidence": 0.9},
            ]
        }
    )
    provider, _client = _provider_returning(payload)
    out = provider.attribute_speakers(context="c", candidates=["s1"], known_speakers=[])
    assert [c.segment_id for c in out] == ["s1"]  # 'ghost' dropped, one-per-requested-id


def test_one_candidate_per_requested_id_preserves_order() -> None:
    payload = json.dumps(
        {
            "attributions": [
                {"segment_id": "s2", "speaker": "Bob", "confidence": 0.7},
                {"segment_id": "s1", "speaker": "Alice", "confidence": 0.9},
            ]
        }
    )
    provider, _client = _provider_returning(payload)
    out = provider.attribute_speakers(context="c", candidates=["s1", "s2"], known_speakers=[])
    # Output order follows the REQUESTED order, not the model's response order.
    assert [c.segment_id for c in out] == ["s1", "s2"]


# --------------------------------------------------------------------------- #
# malformed output -> LLMProviderError(malformed=True)
# --------------------------------------------------------------------------- #
def test_non_json_output_raises_malformed() -> None:
    provider, _client = _provider_returning("not json at all")
    with pytest.raises(LLMProviderError) as exc:
        provider.attribute_speakers(context="c", candidates=["s1"], known_speakers=[])
    assert exc.value.malformed is True


def test_wrong_shape_attributions_not_a_list_raises_malformed() -> None:
    provider, _client = _provider_returning(json.dumps({"attributions": "nope"}))
    with pytest.raises(LLMProviderError) as exc:
        provider.attribute_speakers(context="c", candidates=["s1"], known_speakers=[])
    assert exc.value.malformed is True


def test_missing_attributions_key_raises_malformed() -> None:
    provider, _client = _provider_returning(json.dumps({"other": []}))
    with pytest.raises(LLMProviderError) as exc:
        provider.attribute_speakers(context="c", candidates=["s1"], known_speakers=[])
    assert exc.value.malformed is True


def test_entry_missing_segment_id_raises_malformed() -> None:
    payload = json.dumps({"attributions": [{"speaker": "Alice", "confidence": 0.9}]})
    provider, _client = _provider_returning(payload)
    with pytest.raises(LLMProviderError) as exc:
        provider.attribute_speakers(context="c", candidates=["s1"], known_speakers=[])
    assert exc.value.malformed is True


# --------------------------------------------------------------------------- #
# pure parser + availability
# --------------------------------------------------------------------------- #
def test_parse_attributions_direct() -> None:
    payload = json.dumps(
        {"attributions": [{"segment_id": "s1", "speaker": "Alice", "confidence": 0.9}]}
    )
    out = ClaudeProvider._parse_attributions(payload, ["s1"])
    assert out[0].speaker_name == "Alice"


def test_is_available_uses_constructor_key() -> None:
    assert ClaudeProvider(model="m", api_key="k").is_available() is True


def test_is_available_false_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ClaudeProvider(model="m", api_key=None).is_available() is False


def test_is_available_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert ClaudeProvider(model="m", api_key=None).is_available() is True

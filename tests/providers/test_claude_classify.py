"""ClaudeProvider.classify_speakers unit tests — offline, no real API.

The ``anthropic`` SDK call is stubbed by monkeypatching ``ClaudeProvider._client`` with a
fake whose ``messages.create`` returns canned text blocks, so we exercise the provider's JSON
parsing and contract enforcement WITHOUT any network or real model (the real SDK call shape is
verified separately and must stay untested against a live API).

Covers: JSON -> SpeakerClassification mapping; one entry per requested speaker_id (missing ->
0.0 `unknown` default); unrequested ids dropped; requested order preserved; non-JSON and
wrong-shape output -> ``LLMProviderError(malformed=True)``; an empty speaker list makes NO API
call; the deterministic prompt carries each speaker's id + samples. ``_parse_classifications``
is also exercised directly (it is the pure core).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from casttrophizer.errors import LLMProviderError
from casttrophizer.providers.base import SpeakerProfile
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
        self.call_count = 0

    def create(self, **kwargs: Any) -> _Response:
        self.call_count += 1
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


def _profiles(*ids: str) -> list[SpeakerProfile]:
    return [SpeakerProfile(speaker_id=i, name=i.upper(), samples=[f"line for {i}"]) for i in ids]


# --------------------------------------------------------------------------- #
# happy-path parsing
# --------------------------------------------------------------------------- #
def test_parses_categories_per_requested_id() -> None:
    payload = json.dumps(
        {
            "classifications": [
                {"speaker_id": "s1", "category": "woman", "confidence": 0.9, "rationale": "name"},
                {"speaker_id": "s2", "category": "man", "confidence": 0.8},
            ]
        }
    )
    provider, _client = _provider_returning(payload)
    out = provider.classify_speakers(speakers=_profiles("s1", "s2"))

    assert [c.speaker_id for c in out] == ["s1", "s2"]
    assert out[0].category == "woman" and out[0].confidence == 0.9
    assert out[1].category == "man"


def test_missing_requested_id_defaults_to_unknown() -> None:
    payload = json.dumps(
        {"classifications": [{"speaker_id": "s1", "category": "girl", "confidence": 0.9}]}
    )
    provider, _client = _provider_returning(payload)
    out = provider.classify_speakers(speakers=_profiles("s1", "s2"))
    assert len(out) == 2
    missing = next(c for c in out if c.speaker_id == "s2")
    assert missing.category == "unknown" and missing.confidence == 0.0


def test_unrequested_returned_id_is_dropped() -> None:
    payload = json.dumps(
        {
            "classifications": [
                {"speaker_id": "s1", "category": "boy", "confidence": 0.9},
                {"speaker_id": "ghost", "category": "man", "confidence": 0.9},
            ]
        }
    )
    provider, _client = _provider_returning(payload)
    out = provider.classify_speakers(speakers=_profiles("s1"))
    assert [c.speaker_id for c in out] == ["s1"]  # 'ghost' dropped, one-per-requested-id


def test_output_order_follows_requested_order() -> None:
    payload = json.dumps(
        {
            "classifications": [
                {"speaker_id": "s2", "category": "man", "confidence": 0.7},
                {"speaker_id": "s1", "category": "woman", "confidence": 0.9},
            ]
        }
    )
    provider, _client = _provider_returning(payload)
    out = provider.classify_speakers(speakers=_profiles("s1", "s2"))
    assert [c.speaker_id for c in out] == ["s1", "s2"]  # requested order, not response order


# --------------------------------------------------------------------------- #
# empty input makes no API call
# --------------------------------------------------------------------------- #
def test_empty_speakers_makes_no_api_call() -> None:
    provider, client = _provider_returning(json.dumps({"classifications": []}))
    assert provider.classify_speakers(speakers=[]) == []
    assert client.messages.call_count == 0  # short-circuits before touching the client


# --------------------------------------------------------------------------- #
# malformed output -> LLMProviderError(malformed=True)
# --------------------------------------------------------------------------- #
def test_non_json_output_raises_malformed() -> None:
    provider, _client = _provider_returning("not json at all")
    with pytest.raises(LLMProviderError) as exc:
        provider.classify_speakers(speakers=_profiles("s1"))
    assert exc.value.malformed is True


def test_wrong_shape_classifications_not_a_list_raises_malformed() -> None:
    provider, _client = _provider_returning(json.dumps({"classifications": "nope"}))
    with pytest.raises(LLMProviderError) as exc:
        provider.classify_speakers(speakers=_profiles("s1"))
    assert exc.value.malformed is True


def test_missing_classifications_key_raises_malformed() -> None:
    provider, _client = _provider_returning(json.dumps({"other": []}))
    with pytest.raises(LLMProviderError) as exc:
        provider.classify_speakers(speakers=_profiles("s1"))
    assert exc.value.malformed is True


def test_entry_missing_speaker_id_raises_malformed() -> None:
    payload = json.dumps({"classifications": [{"category": "man", "confidence": 0.9}]})
    provider, _client = _provider_returning(payload)
    with pytest.raises(LLMProviderError) as exc:
        provider.classify_speakers(speakers=_profiles("s1"))
    assert exc.value.malformed is True


# --------------------------------------------------------------------------- #
# prompt shape + pure parser
# --------------------------------------------------------------------------- #
def test_prompt_carries_ids_and_samples() -> None:
    payload = json.dumps(
        {"classifications": [{"speaker_id": "s1", "category": "man", "confidence": 0.9}]}
    )
    provider, client = _provider_returning(payload)
    provider.classify_speakers(
        speakers=[SpeakerProfile(speaker_id="s1", name="Bob", samples=["Howdy."])]
    )
    kwargs = client.messages.create_kwargs
    assert kwargs is not None
    user_msg = kwargs["messages"][0]["content"]
    assert "s1" in user_msg and "Bob" in user_msg and "Howdy." in user_msg


def test_parse_classifications_direct() -> None:
    payload = json.dumps(
        {"classifications": [{"speaker_id": "s1", "category": "girl", "confidence": 0.9}]}
    )
    out = ClaudeProvider._parse_classifications(payload, ["s1"])
    assert out[0].category == "girl"

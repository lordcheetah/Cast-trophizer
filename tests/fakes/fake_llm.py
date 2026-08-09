"""Deterministic, offline fake LLM provider for tests.

``attribute_speakers`` returns scripted candidates (optionally including a
low-confidence one so the review path can be exercised); ``complete`` returns a canned
string. No network, no real model.

Scripting: pass ``script={<segment id>: (speaker_name, confidence)}`` to force an exact
attribution per segment, or ``text_script={<quote text>: (speaker_name, confidence)}`` to
key by the quote text the context carries after each ``[SEG <id>]`` tag (segment ids are
generated at runtime, so text keying is what tests usually want). Pass ``omit_ids`` to drop
those segment ids from the response (tests the missing-id default), ``extra_ids`` to return
an unrequested id (tests the defensive drop), ``raise_malformed=True`` to raise
``LLMProviderError(malformed=True)`` (tests retry-then-soft-flag), and ``raise_unreachable``
to raise a plain ``LLMProviderError`` (tests the FAILED path). ``attribute_calls`` records
every call so tests can assert call counts (idempotency / batching).

Voice-category classification is scripted separately via ``classify_script`` (keyed by
speaker *name* -> ``(category, confidence)``, default ``unknown``); ``raise_classify_malformed``
/ ``raise_classify_unreachable`` exercise the soft-fail path, and ``classify_calls`` records
each classification pass.
"""

from __future__ import annotations

import re

from casttrophizer.errors import LLMProviderError
from casttrophizer.providers.base import (
    AttributionCandidate,
    LLMMessage,
    LLMProvider,
    SpeakerClassification,
    SpeakerProfile,
)

__all__ = ["FakeLLMProvider"]

#: Matches the ``[SEG <id>] <quote text>`` markers the stage's ``build_context`` renders, so
#: a test can script by the human-readable quote text instead of the runtime-generated id.
_SEG_MARKER = re.compile(r"\[SEG (?P<id>\S+)\] (?P<text>\"[^\"]*\"|“[^”]*”)")


class FakeLLMProvider(LLMProvider):
    """A scriptable, deterministic stand-in for a real LLM provider."""

    name = "fake-llm"

    def __init__(
        self,
        *,
        completion: str = "FAKE COMPLETION",
        default_confidence: float = 0.95,
        available: bool = True,
        script: dict[str, tuple[str | None, float]] | None = None,
        text_script: dict[str, tuple[str | None, float]] | None = None,
        omit_ids: set[str] | None = None,
        extra_ids: list[str] | None = None,
        raise_malformed: bool = False,
        raise_unreachable: bool = False,
        classify_script: dict[str, tuple[str, float]] | None = None,
        raise_classify_malformed: bool = False,
        raise_classify_unreachable: bool = False,
    ) -> None:
        self._completion = completion
        self._default_confidence = default_confidence
        self._available = available
        self._script = script or {}
        self._text_script = text_script or {}
        self._omit_ids = omit_ids or set()
        self._extra_ids = extra_ids or []
        self._raise_malformed = raise_malformed
        self._raise_unreachable = raise_unreachable
        self._classify_script = classify_script or {}
        self._raise_classify_malformed = raise_classify_malformed
        self._raise_classify_unreachable = raise_classify_unreachable
        self.complete_calls: list[list[LLMMessage]] = []
        self.attribute_calls: list[tuple[str, list[str], list[str]]] = []
        self.classify_calls: list[list[SpeakerProfile]] = []

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

        if self._raise_unreachable:
            raise LLMProviderError("fake provider unreachable")
        if self._raise_malformed:
            raise LLMProviderError("fake malformed output", malformed=True)

        # Resolve a quote-text -> segment-id map from the context so ``text_script`` (keyed by
        # the human-readable quote text) can drive exact per-segment confidences/speakers.
        text_by_id = {m.group("id"): m.group("text") for m in _SEG_MARKER.finditer(context)}

        results: list[AttributionCandidate] = []
        for i, segment_id in enumerate(candidates):
            if segment_id in self._omit_ids:
                continue  # simulate the model omitting a requested id
            quote_text = text_by_id.get(segment_id)
            if segment_id in self._script:
                speaker, confidence = self._script[segment_id]
            elif quote_text is not None and quote_text in self._text_script:
                speaker, confidence = self._text_script[quote_text]
            else:
                # Default: first candidate low-confidence (review path), rest high.
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
        for extra in self._extra_ids:
            results.append(
                AttributionCandidate(
                    segment_id=extra, speaker_name="Ghost", confidence=0.9, rationale="extra"
                )
            )
        return results

    def classify_speakers(self, *, speakers: list[SpeakerProfile]) -> list[SpeakerClassification]:
        """Return scripted categories keyed by speaker name (default ``unknown``).

        ``classify_script`` maps a speaker's display name -> ``(category, confidence)``. A
        name not in the script classifies ``unknown`` at 0.0. ``raise_classify_unreachable``
        raises a plain ``LLMProviderError`` (reachability); ``raise_classify_malformed``
        raises ``LLMProviderError(malformed=True)`` — both exercise the soft-fail path (the
        orchestration leaves categories ``unknown`` and never fails the stage). ``classify_calls``
        records every call so tests can assert exactly one classification pass per finished run.
        """
        self.classify_calls.append(list(speakers))

        if self._raise_classify_unreachable:
            raise LLMProviderError("fake classifier unreachable")
        if self._raise_classify_malformed:
            raise LLMProviderError("fake malformed classification output", malformed=True)

        results: list[SpeakerClassification] = []
        for profile in speakers:
            category, confidence = self._classify_script.get(profile.name, ("unknown", 0.0))
            results.append(
                SpeakerClassification(
                    speaker_id=profile.speaker_id,
                    category=category,
                    confidence=confidence,
                    rationale="scripted",
                )
            )
        return results

    def is_available(self) -> bool:
        return self._available

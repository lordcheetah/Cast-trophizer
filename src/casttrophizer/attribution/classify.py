"""Post-attribution voice-category classification: one LLM call, soft on failure.

Mirrors :mod:`casttrophizer.attribution.attribute` — pure-ish orchestration over the
in-memory ``Project``; the caller (the attribute stage) owns persistence. Runs once, on the
pass that finishes attribution, classifying every discovered CHARACTER speaker into a
:class:`~casttrophizer.domain.enums.VoiceCategory` in a single ``llm.classify_speakers``
call.

Category is a **per-speaker** property (the narrator is always ``UNKNOWN`` and is never
sent), so this is a separate pass from per-segment attribution rather than folded into it.

**Soft by design:** ANY :class:`~casttrophizer.errors.LLMProviderError` (malformed output or
an unreachable provider) leaves categories ``UNKNOWN`` and returns — classification is
non-critical and never fails the attribute stage, which has already succeeded. A provider
that returns everyone ``unknown`` (the ABC default, e.g. LM Studio v1) is likewise a no-op.
"""

from __future__ import annotations

from casttrophizer.domain.enums import SpeakerRole, VoiceCategory
from casttrophizer.domain.models import Project
from casttrophizer.errors import LLMProviderError
from casttrophizer.providers.base import LLMProvider, SpeakerProfile

__all__ = ["CLASSIFY_SAMPLE_LINES", "classify_speakers"]

#: Max dialogue samples per speaker sent to the LLM. A name plus a few of the character's own
#: quoted lines is enough to bucket gender/age; more just spends tokens.
CLASSIFY_SAMPLE_LINES = 3


def classify_speakers(project: Project, llm: LLMProvider) -> None:
    """Classify project CHARACTER speakers into :class:`VoiceCategory` in place (one call).

    Builds a :class:`SpeakerProfile` per CHARACTER speaker whose category is still
    ``UNKNOWN``, sampling up to :data:`CLASSIFY_SAMPLE_LINES` of that speaker's own quote
    segment texts. Calls ``llm.classify_speakers`` once and maps each result via
    ``VoiceCategory.coerce`` onto the speaker. Soft: any ``LLMProviderError`` leaves
    categories ``UNKNOWN`` and returns. The narrator is never sent (stays ``UNKNOWN``). A
    no-op when there are no as-yet-unclassified CHARACTER speakers.
    """
    targets = [
        sp
        for sp in project.speakers
        if sp.role == SpeakerRole.CHARACTER and sp.category == VoiceCategory.UNKNOWN
    ]
    if not targets:
        return

    samples = _samples_by_speaker(project)
    profiles = [
        SpeakerProfile(speaker_id=sp.id, name=sp.name, samples=samples.get(sp.id, []))
        for sp in targets
    ]

    try:
        results = llm.classify_speakers(speakers=profiles)
    except LLMProviderError:
        return  # soft: leave every target UNKNOWN; classification never fails the stage

    by_id = {sp.id: sp for sp in targets}
    for result in results:
        speaker = by_id.get(result.speaker_id)
        if speaker is not None:  # ignore ids we did not request
            speaker.category = VoiceCategory.coerce(result.category)


def _samples_by_speaker(project: Project) -> dict[str, list[str]]:
    """Collect up to :data:`CLASSIFY_SAMPLE_LINES` quote texts per speaker id, in reading order."""
    samples: dict[str, list[str]] = {}
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                if segment.role != SpeakerRole.CHARACTER or segment.speaker_id is None:
                    continue
                text = segment.text.strip()
                if not text:
                    continue
                bucket = samples.setdefault(segment.speaker_id, [])
                if len(bucket) < CLASSIFY_SAMPLE_LINES:
                    bucket.append(text)
    return samples

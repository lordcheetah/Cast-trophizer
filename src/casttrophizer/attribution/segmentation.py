"""Pure, offline construction of a line's provisional :class:`Segment`s (no LLM).

Splitting a ``Line.text`` into narration/quote :class:`~casttrophizer.domain.models.Segment`s
is the same offline step whether it runs at the **attribute** stage (before the LLM assigns
quote speakers) or at **review** time (when a user's text edit re-segments a line). Extracting
it here — Qt-free, provider-free, deterministic — lets both callers share one rule so the
segment shapes can never drift.

Contract (matches ``attribution.attribute._attribute_window`` step 1 exactly):

* a **quote** span becomes a provisional :class:`Segment` with ``speaker_id=None`` (it renders
  as the narrator until attributed), ``role=NARRATOR``, ``confidence=0.0`` and
  ``review_status=NEEDS_REVIEW`` — so a re-segmented quote re-enters attribution review;
* a **narration** span becomes an APPROVED narrator segment (``speaker_id=narrator.id``,
  ``role=NARRATOR``, ``confidence=1.0``).

Fresh ids are minted per segment and ``audio_cache_key`` / ``audio_status`` default to
``None`` / ``PENDING`` (a brand-new :class:`Segment`), so every rebuilt segment re-renders on
the next synth pass. No LLM SDK is imported here.
"""

from __future__ import annotations

from casttrophizer.attribution.segmenter import KIND_QUOTE, Segmenter
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Segment, Speaker

__all__ = ["build_line_segments"]


def build_line_segments(line_text: str, narrator: Speaker, segmenter: Segmenter) -> list[Segment]:
    """Segment ``line_text`` into ordered provisional narration/quote :class:`Segment`s.

    Narration spans point at ``narrator`` (APPROVED); quote spans are ``speaker_id=None``
    NEEDS_REVIEW candidates (attributed later by the LLM at the attribute stage, or manually in
    review). An empty/whitespace-only line yields ``[]`` (the segmenter returns no spans).
    """
    built: list[Segment] = []
    for span in segmenter.split(line_text):
        if span.kind == KIND_QUOTE:
            built.append(
                Segment(
                    id=new_id("seg"),
                    text=span.text,
                    speaker_id=None,  # provisional; renders as narrator until attributed
                    role=SpeakerRole.NARRATOR,
                    confidence=0.0,
                    review_status=ReviewStatus.NEEDS_REVIEW,
                )
            )
        else:
            built.append(
                Segment(
                    id=new_id("seg"),
                    text=span.text,
                    speaker_id=narrator.id,
                    role=SpeakerRole.NARRATOR,
                    confidence=1.0,
                    review_status=ReviewStatus.APPROVED,
                )
            )
    return built

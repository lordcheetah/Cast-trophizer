"""Offline speaker-attribution package: segmentation + policy + orchestration.

Exposes the deterministic :class:`QuoteSegmenter` (and the :class:`Segmenter` protocol),
the confidence/registry policy, and the per-chapter ``attribute_chapter`` orchestration the
attribute stage drives. Import-cheap and Qt-/SDK-free: importing this package never pulls in
``anthropic`` or ``openai`` (the Claude provider keeps its SDK import lazy).
"""

from __future__ import annotations

from casttrophizer.attribution.attribute import (
    MAX_LINES_PER_BATCH,
    attribute_chapter,
    build_context,
)
from casttrophizer.attribution.classify import (
    CLASSIFY_SAMPLE_LINES,
    classify_speakers,
)
from casttrophizer.attribution.policy import (
    ATTRIBUTION_CONFIDENCE_THRESHOLD,
    ensure_narrator,
    resolve_speaker,
    review_status_for,
)
from casttrophizer.attribution.segmenter import (
    QuoteSegmenter,
    Segmenter,
    SegmentSpan,
)

__all__ = [
    "QuoteSegmenter",
    "Segmenter",
    "SegmentSpan",
    "ATTRIBUTION_CONFIDENCE_THRESHOLD",
    "ensure_narrator",
    "resolve_speaker",
    "review_status_for",
    "MAX_LINES_PER_BATCH",
    "attribute_chapter",
    "build_context",
    "CLASSIFY_SAMPLE_LINES",
    "classify_speakers",
    "default_segmenter",
]


def default_segmenter() -> Segmenter:
    """Return the production segmenter (the deterministic double-quote splitter)."""
    return QuoteSegmenter()

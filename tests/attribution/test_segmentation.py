"""Unit tests for the shared, offline segment builder (no LLM, no store, no Qt).

``build_line_segments`` is the one rule both the attribute stage and review-time text edits use
to turn a line's text into provisional narration/quote :class:`Segment`s — narration -> APPROVED
narrator, quote -> NEEDS_REVIEW ``speaker_id=None``.
"""

from __future__ import annotations

from casttrophizer.attribution.segmentation import build_line_segments
from casttrophizer.attribution.segmenter import QuoteSegmenter
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Speaker


def _narrator() -> Speaker:
    return Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)


def test_narration_becomes_approved_narrator_segment() -> None:
    narrator = _narrator()
    segments = build_line_segments("The hall was silent.", narrator, QuoteSegmenter())
    assert len(segments) == 1
    seg = segments[0]
    assert seg.text == "The hall was silent."
    assert seg.speaker_id == narrator.id
    assert seg.role == SpeakerRole.NARRATOR
    assert seg.confidence == 1.0
    assert seg.review_status == ReviewStatus.APPROVED
    assert seg.audio_cache_key is None  # fresh segment -> re-renders on next synth
    assert seg.audio_status == ReviewStatus.PENDING


def test_quote_becomes_needs_review_narrator_fallback_segment() -> None:
    narrator = _narrator()
    segments = build_line_segments('"Who goes there?"', narrator, QuoteSegmenter())
    assert len(segments) == 1
    seg = segments[0]
    assert seg.text == '"Who goes there?"'
    assert seg.speaker_id is None  # provisional; renders as narrator until attributed
    assert seg.role == SpeakerRole.NARRATOR
    assert seg.confidence == 0.0
    assert seg.review_status == ReviewStatus.NEEDS_REVIEW


def test_mixed_line_preserves_span_order() -> None:
    narrator = _narrator()
    segments = build_line_segments('"Hello," said Alice.', narrator, QuoteSegmenter())
    kinds = [(s.text, s.review_status) for s in segments]
    assert kinds == [
        ('"Hello,"', ReviewStatus.NEEDS_REVIEW),
        ("said Alice.", ReviewStatus.APPROVED),
    ]
    # Every segment carries a distinct fresh id.
    assert len({s.id for s in segments}) == len(segments)


def test_empty_or_whitespace_line_yields_no_segments() -> None:
    narrator = _narrator()
    assert build_line_segments("", narrator, QuoteSegmenter()) == []
    assert build_line_segments("   ", narrator, QuoteSegmenter()) == []

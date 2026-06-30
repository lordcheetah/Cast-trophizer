"""``build_context`` rendering tests — per-line SEG tagging from each line's OWN segments.

These lock the fix for the cross-contamination bug: rendering must tag a quote's ``[SEG <id>]``
marker only on the line that owns it (never on another line that merely shares the same short
quote text), and a quote text repeated within a single line must tag its distinct owning
segments in left-to-right positional order.

Segments are built exactly the way ``_attribute_window`` builds them (real ``QuoteSegmenter``,
quote spans -> provisional ``Segment``s plus a ``quote_owner`` map), so the test exercises real
segment ids.
"""

from __future__ import annotations

import re

from casttrophizer.attribution.attribute import build_context
from casttrophizer.attribution.segmenter import KIND_QUOTE, QuoteSegmenter
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Line, Segment


def _segment_window(
    texts: list[str],
) -> tuple[list[Line], dict[str, Segment], dict[str, Segment]]:
    """Mirror ``_attribute_window`` step 1: segment each line, build quote map + owner map.

    Returns ``(window, quote_segments, quote_owner)`` where ``quote_segments`` maps a quote
    segment id to its ``Segment`` and ``quote_owner`` maps that id to the owning ``Line`` —
    the two structures ``build_context`` consumes.
    """
    segmenter = QuoteSegmenter()
    window: list[Line] = []
    quote_segments: dict[str, Segment] = {}
    quote_owner: dict[str, Line] = {}
    for order, text in enumerate(texts):
        line = Line(id=new_id("line"), chapter_id="ch", order=order, text=text, segments=[])
        built: list[Segment] = []
        for span in segmenter.split(line.text):
            seg = Segment(
                id=new_id("seg"),
                text=span.text,
                speaker_id=None,
                role=SpeakerRole.NARRATOR,
                confidence=0.0,
                review_status=ReviewStatus.NEEDS_REVIEW,
            )
            if span.kind == KIND_QUOTE:
                quote_segments[seg.id] = seg
                quote_owner[seg.id] = line
            built.append(seg)
        line.segments = built
        window.append(line)
    return window, quote_segments, quote_owner


def _rendered_lines(context: str) -> dict[int, str]:
    """Parse ``build_context`` output into ``{line_number: rendered_text}``."""
    out: dict[int, str] = {}
    for raw in context.splitlines():
        m = re.match(r"L(\d+): (.*)", raw)
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def _seg_ids_in(rendered: str) -> list[str]:
    return re.findall(r"\[SEG (\S+)\]", rendered)


def test_shared_quote_tags_each_line_with_its_own_segment_only() -> None:
    """Two lines sharing the identical quote ``"Hi,"`` must NOT cross-contaminate.

    Each rendered line carries exactly one SEG marker, and it is that line's own owning
    segment id (not the other line's, not both).
    """
    window, quote_segments, quote_owner = _segment_window(['"Hi," said Alice.', '"Hi," said Bob.'])

    # The owning quote segment id for each line, in order.
    line1_quote = next(sid for sid, seg in quote_segments.items() if quote_owner[sid] is window[0])
    line2_quote = next(sid for sid, seg in quote_segments.items() if quote_owner[sid] is window[1])
    assert line1_quote != line2_quote

    context = build_context(window, quote_segments, quote_owner)
    rendered = _rendered_lines(context)

    assert _seg_ids_in(rendered[1]) == [line1_quote]
    assert _seg_ids_in(rendered[2]) == [line2_quote]
    # And the tag actually sits on the quote, with surrounding narration preserved.
    assert rendered[1] == f'[SEG {line1_quote}] "Hi," said Alice.'
    assert rendered[2] == f'[SEG {line2_quote}] "Hi," said Bob.'


def test_repeated_identical_quote_in_one_line_gets_distinct_ordered_ids() -> None:
    """A single line with two identical quote texts tags each occurrence with its own id."""
    window, quote_segments, quote_owner = _segment_window(['"No." he said. "No." she repeated.'])

    owned = [seg for sid, seg in quote_segments.items() if quote_owner[sid] is window[0]]
    # Two quote segments, same text, distinct ids, in positional order.
    assert [seg.text for seg in owned] == ['"No."', '"No."']
    first_id, second_id = owned[0].id, owned[1].id
    assert first_id != second_id

    context = build_context(window, quote_segments, quote_owner)
    rendered = _rendered_lines(context)

    # Both ids appear, each on its own occurrence, in order.
    assert _seg_ids_in(rendered[1]) == [first_id, second_id]
    assert rendered[1] == (f'[SEG {first_id}] "No." he said. [SEG {second_id}] "No." she repeated.')

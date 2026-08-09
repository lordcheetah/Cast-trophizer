"""QuoteSegmenter unit tests — deterministic double-quote segmentation (v1 ruleset).

Covers: no-quote -> single narration; narration/quote/narration with exact texts and kinds;
curly == straight; multiple quotes per line; unbalanced -> whole-line narration; apostrophe
not treated as a delimiter; empty -> []; and the cache-key-stability determinism contract.
"""

from __future__ import annotations

from casttrophizer.attribution.segmenter import (
    KIND_NARRATION,
    KIND_QUOTE,
    QuoteSegmenter,
    SegmentSpan,
)


def _seg() -> QuoteSegmenter:
    return QuoteSegmenter()


def test_no_quote_line_is_single_narration_span() -> None:
    spans = _seg().split("The hall was silent.")
    assert spans == [SegmentSpan(text="The hall was silent.", kind=KIND_NARRATION)]


def test_narration_quote_narration_with_exact_texts() -> None:
    spans = _seg().split('The narrator spoke. "Hello there," said Alice.')
    assert spans == [
        SegmentSpan(text="The narrator spoke.", kind=KIND_NARRATION),
        SegmentSpan(text='"Hello there,"', kind=KIND_QUOTE),
        SegmentSpan(text="said Alice.", kind=KIND_NARRATION),
    ]


def test_leading_quote_then_dialogue_tag() -> None:
    spans = _seg().split('"Hello," said Alice.')
    assert spans == [
        SegmentSpan(text='"Hello,"', kind=KIND_QUOTE),
        SegmentSpan(text="said Alice.", kind=KIND_NARRATION),
    ]


def test_curly_quotes_split_like_straight() -> None:
    spans = _seg().split("“Hi,” said Bob.")
    assert spans == [
        SegmentSpan(text="“Hi,”", kind=KIND_QUOTE),
        SegmentSpan(text="said Bob.", kind=KIND_NARRATION),
    ]


def test_two_quotes_with_interleaved_narration() -> None:
    spans = _seg().split('"Yes," she said. "No," he replied.')
    assert spans == [
        SegmentSpan(text='"Yes,"', kind=KIND_QUOTE),
        SegmentSpan(text="she said.", kind=KIND_NARRATION),
        SegmentSpan(text='"No,"', kind=KIND_QUOTE),
        SegmentSpan(text="he replied.", kind=KIND_NARRATION),
    ]


def test_unbalanced_straight_quote_is_whole_line_narration() -> None:
    spans = _seg().split('"oops he said')
    assert spans == [SegmentSpan(text='"oops he said', kind=KIND_NARRATION)]


def test_unbalanced_curly_open_is_whole_line_narration() -> None:
    spans = _seg().split("“oops he said")
    assert spans == [SegmentSpan(text="“oops he said", kind=KIND_NARRATION)]


def test_stray_curly_close_is_whole_line_narration() -> None:
    spans = _seg().split("oops” he said")
    assert spans == [SegmentSpan(text="oops” he said", kind=KIND_NARRATION)]


def test_apostrophe_is_not_a_quote_delimiter() -> None:
    spans = _seg().split('Alice\'s dog ran. "Hi," she said.')
    assert spans == [
        SegmentSpan(text="Alice's dog ran.", kind=KIND_NARRATION),
        SegmentSpan(text='"Hi,"', kind=KIND_QUOTE),
        SegmentSpan(text="she said.", kind=KIND_NARRATION),
    ]


def test_quote_spanning_sentence_boundaries_stays_one_quote() -> None:
    spans = _seg().split('"Stop! Wait. Listen?" she begged.')
    assert spans == [
        SegmentSpan(text='"Stop! Wait. Listen?"', kind=KIND_QUOTE),
        SegmentSpan(text="she begged.", kind=KIND_NARRATION),
    ]


def test_empty_and_whitespace_lines_yield_no_spans() -> None:
    assert _seg().split("") == []
    assert _seg().split("   \t  ") == []


def test_split_is_deterministic() -> None:
    text = 'The narrator spoke. "Hello there," said Alice.'
    seg = _seg()
    assert seg.split(text) == seg.split(text)  # cache-key stability contract

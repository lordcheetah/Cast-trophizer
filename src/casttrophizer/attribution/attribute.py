"""Per-chapter attribution orchestration: segment -> batch -> LLM -> apply.

Keeps :class:`~casttrophizer.pipeline.stages.attribute.SegmentAttributeStage` thin
(mirrors how ``text/base.apply_fixes`` holds the correct stage's policy). Pure-ish on the
in-memory ``Project`` — the stage owns persistence.

Flow per chapter (over only the chapter's *unattributed* lines, in ``MAX_LINES_PER_BATCH``
windows):

1. Segment each line offline (``QuoteSegmenter``) into narration/quote spans and build
   provisional ``Segment``s. Narration spans become APPROVED narrator segments and are
   NEVER sent to the LLM; quote spans are the LLM candidates. An empty line yields zero
   segments.
2. Send the window's quote-segment ids (plus a rendered context + known character names)
   to ``llm.attribute_speakers``. On malformed/non-JSON output or a returned id that
   doesn't exist / missing ids, retry the batch ONCE; if it still fails, soft-flag that
   window's quote segments NEEDS_REVIEW (confidence 0.0, narrator default) and continue —
   one bad batch never kills the run, and nothing is silently committed.
3. Map each returned candidate back to its segment, resolving/creating the Speaker and
   setting ``speaker_id``/``role``/``confidence``/``review_status`` per the policy.

A genuinely unreachable provider raises ``LLMProviderError`` out of this module so the
stage can return FAILED.
"""

from __future__ import annotations

from casttrophizer.attribution.policy import resolve_speaker, review_status_for
from casttrophizer.attribution.segmentation import build_line_segments
from casttrophizer.attribution.segmenter import Segmenter
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.models import Chapter, Line, Project, Segment, Speaker
from casttrophizer.errors import LLMProviderError
from casttrophizer.providers.base import AttributionCandidate, LLMProvider

__all__ = ["MAX_LINES_PER_BATCH", "attribute_chapter", "build_context"]

#: Max lines per LLM call. A chapter with <= this many unattributed lines is one call; a
#: longer one is split into consecutive windows. Bounds tokens on huge chapters while
#: keeping the common case to one call/chapter. Windows derive purely from line order, so
#: the call count is deterministic (tester pins it).
MAX_LINES_PER_BATCH = 60


def attribute_chapter(
    chapter: Chapter,
    todo: list[Line],
    narrator: Speaker,
    project: Project,
    llm: LLMProvider,
    segmenter: Segmenter,
    threshold: float,
) -> None:
    """Segment and attribute ``todo`` (a chapter's unattributed lines) in place.

    Segments every ``todo`` line, then attributes the quote segments in
    ``MAX_LINES_PER_BATCH`` windows. ``narrator`` is the project's reserved narrator
    Speaker. Mutates ``project.speakers`` (new characters) and each line's ``segments``.
    """
    for start in range(0, len(todo), MAX_LINES_PER_BATCH):
        window = todo[start : start + MAX_LINES_PER_BATCH]
        _attribute_window(chapter, window, narrator, project, llm, segmenter, threshold)


def _attribute_window(
    chapter: Chapter,
    window: list[Line],
    narrator: Speaker,
    project: Project,
    llm: LLMProvider,
    segmenter: Segmenter,
    threshold: float,
) -> None:
    """Segment ``window``'s lines, then attribute their quote segments in one LLM call."""
    # Step 1: segment every line (shared offline builder; narration -> APPROVED narrator, quote
    # -> provisional NEEDS_REVIEW), then re-derive the quote maps from the built candidates.
    quote_segments: dict[str, Segment] = {}  # segment id -> the quote Segment
    quote_owner: dict[str, Line] = {}  # segment id -> the line it came from (for context)
    for line in window:
        line.segments = build_line_segments(line.text, narrator, segmenter)
        for seg in line.segments:
            if seg.review_status == ReviewStatus.NEEDS_REVIEW:  # a provisional quote candidate
                quote_segments[seg.id] = seg
                quote_owner[seg.id] = line

    if not quote_segments:
        return  # all-narration window -> no LLM call

    candidate_ids = list(quote_segments.keys())
    context = build_context(window, quote_segments, quote_owner)
    known_speakers = [sp.name for sp in project.speakers if sp.role == SpeakerRole.CHARACTER]

    results = _attribute_with_retry(
        llm, context=context, candidate_ids=candidate_ids, known_speakers=known_speakers
    )

    if results is None:
        # Malformed output after a retry: soft-flag the window's quote segments.
        for seg in quote_segments.values():
            seg.speaker_id = None
            seg.role = SpeakerRole.NARRATOR
            seg.confidence = 0.0
            seg.review_status = ReviewStatus.NEEDS_REVIEW
        return

    by_id = {c.segment_id: c for c in results}  # last-write-wins; unrequested ids ignored
    for seg_id, seg in quote_segments.items():
        candidate = by_id.get(seg_id)
        if candidate is None:
            # Missing id -> safe low-confidence narrator default, flagged for review.
            seg.speaker_id = None
            seg.role = SpeakerRole.NARRATOR
            seg.confidence = 0.0
            seg.review_status = ReviewStatus.NEEDS_REVIEW
            continue
        speaker = resolve_speaker(project, narrator, candidate.speaker_name)
        seg.speaker_id = speaker.id
        seg.role = speaker.role
        seg.confidence = candidate.confidence
        seg.review_status = review_status_for(candidate.confidence, threshold)


def _attribute_with_retry(
    llm: LLMProvider,
    *,
    context: str,
    candidate_ids: list[str],
    known_speakers: list[str],
) -> list[AttributionCandidate] | None:
    """Call ``attribute_speakers``, retrying ONCE on malformed output.

    Returns the candidate list, or ``None`` if the output is still malformed after the
    retry (the caller then soft-flags the batch). "Malformed" = a returned candidate that
    isn't even one of the requested ids and no requested id is covered, i.e. the provider
    raised :class:`~casttrophizer.errors.LLMProviderError` for non-JSON, OR a result set
    that covers none of the requested ids. A genuinely unreachable provider raises
    ``LLMProviderError`` out of this function (the stage maps it to FAILED).
    """
    requested = set(candidate_ids)
    last_exc: LLMProviderError | None = None
    for _attempt in range(2):
        try:
            results = llm.attribute_speakers(
                context=context, candidates=candidate_ids, known_speakers=known_speakers
            )
        except LLMProviderError as exc:
            # Distinguish "provider unreachable" (re-raise -> FAILED) from a parse failure
            # (retry, then soft-flag) via the marker the provider sets on malformed output.
            if getattr(exc, "malformed", False):
                last_exc = exc
                continue
            raise
        if any(c.segment_id in requested for c in results):
            return results
        # Covered none of the requested ids -> treat as malformed; retry once.
    _ = last_exc  # keep the last parse error in scope for debuggers; not re-raised
    return None


def build_context(
    window: list[Line],
    quote_segments: dict[str, Segment],
    quote_owner: dict[str, Line],
) -> str:
    """Render the deterministic prompt body the provider forwards to the model.

    Numbers each line of the window and inline-tags each quote span with ``[SEG <id>]`` so
    the model has dialogue-tag and adjacency cues while mapping answers back by id. The
    provider wraps this in its own system prompt + JSON instructions.

    Each line is tagged from its **own** quote segments only (via ``quote_owner``), so
    sharing an identical short quote across two lines never cross-contaminates, and a quote
    text repeated within one line gets its distinct owning segments tagged in left-to-right
    positional order (the Nth occurrence -> that line's Nth owned segment of that text).
    The segmenter trims whitespace between spans, so we tag occurrences on the original
    ``line.text`` rather than rebuilding from spans (which would drop spacing).
    """
    lines_out: list[str] = ["Attribute each marked quote to its speaker. Lines:"]
    for idx, line in enumerate(window, start=1):
        # The quote segments owned by *this* line, in their positional (split) order.
        owned = [seg for seg_id, seg in quote_segments.items() if quote_owner.get(seg_id) is line]
        lines_out.append(f"L{idx}: {_tag_line(line.text, owned)}")
    lines_out.append("Return one entry per SEG id.")
    return "\n".join(lines_out)


def _tag_line(text: str, owned: list[Segment]) -> str:
    """Inline-tag ``text`` with ``[SEG <id>]`` for each of ``owned`` (a line's own quotes).

    Tags occurrences left-to-right, advancing a cursor so the Nth occurrence of an
    identical quote text maps to that line's Nth owned segment of that text. ``owned`` is in
    the segmenter's positional order, which matches the left-to-right occurrence order.
    """
    cursor = 0
    parts: list[str] = []
    for seg in owned:
        found = text.find(seg.text, cursor)
        if found == -1:
            continue  # defensive: should not happen since the span came from this text
        parts.append(text[cursor:found])
        parts.append(f"[SEG {seg.id}] {seg.text}")
        cursor = found + len(seg.text)
    parts.append(text[cursor:])
    return "".join(parts)

"""Pure, Qt-free review-action operations the UI (and :class:`ReviewService`) call.

Each function **mutates the in-memory** :class:`~casttrophizer.domain.models.Project`
(or one of its sub-objects) and performs **no I/O** — persistence is the caller's job
(:class:`casttrophizer.review.service.ReviewService` owns ``store.save``). This keeps the
operations trivially unit-testable without a store.

Speaker creation/lookup reuses ``attribution.policy.resolve_speaker``/``ensure_narrator``
so the one case-insensitive matching rule is shared (no second implementation).

**Text-edit propagation (LIVE):** ``accept_suggestion`` fixes the matching **segment** text
(not just ``line.text``) so the correction reaches the rendered audio, and ``edit_line_text``
**re-segments** the whole line (via
:func:`~casttrophizer.attribution.segmentation.build_line_segments`) so a free-text rewrite is
re-spoken — its quotes returning to NEEDS_REVIEW for manual re-attribution (the review layer
never calls the LLM). See each function for the exact rule.

**Cache consequence:** the attribution/voice actions change inputs that feed
:meth:`~casttrophizer.workspace.audio_cache.AudioCache.key_for` (a segment's resolved
``voice_clip_id``); ``accept_suggestion`` / ``edit_line_text`` change ``segment.text`` (the
other keyed input). Either way the recomputed key differs, so the synthesize stage re-renders
only the affected segments on its next run. The text actions additionally null the touched
segment's stale ``audio_cache_key`` (and reset ``audio_status`` to PENDING), symmetric with the
synth FAILED branch, so the assemble stage never stitches an out-of-date WAV.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.attribution.policy import ensure_narrator, resolve_speaker
from casttrophizer.attribution.segmentation import build_line_segments
from casttrophizer.attribution.segmenter import QuoteSegmenter, Segmenter
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, VoiceCategory
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Line,
    Project,
    Segment,
    Speaker,
    TextSuggestion,
    VoiceClip,
)

__all__ = [
    "accept_suggestion",
    "reject_suggestion",
    "edit_line_text",
    "approve_attribution",
    "reject_attribution",
    "set_segment_speaker",
    "create_character",
    "reassign_segment_to_new_speaker",
    "register_voice_clip",
    "assign_voice",
    "assign_voice_by_ids",
    "unassign_voice",
    "set_speaker_category",
    "approve_audio",
    "reroll_audio",
    "fail_audio",
]


# --------------------------------------------------------------------------- #
# text suggestions (§B.1)
# --------------------------------------------------------------------------- #
def _find_suggestion(line: Line, suggestion_id: str) -> TextSuggestion:
    for suggestion in line.suggestions:
        if suggestion.id == suggestion_id:
            return suggestion
    raise ValueError(f"no suggestion {suggestion_id!r} on line {line.id!r}")


def accept_suggestion(line: Line, suggestion_id: str) -> bool:
    """Apply a suggestion by replacing its ``original`` token with ``suggested`` in the line.

    A surfaced (PENDING) suggestion stores the specific TOKEN being corrected in
    ``original``/``suggested`` (e.g. ``"narrarator"`` -> ``"narrator"``), NOT the whole line —
    so we replace the first occurrence of ``original`` within ``line.text`` rather than
    overwriting the whole line (which would drop the rest of the sentence). A whole-line
    suggestion (``original`` == the full line) still works: replacing it swaps the line.

    **Propagates to audio:** the fix also reaches the spoken :class:`Segment`. We apply the same
    first-occurrence ``replace(original, suggested, 1)`` to the **first** segment whose text
    contains ``original`` and null that segment's ``audio_cache_key`` (``audio_status`` ->
    PENDING) so it re-renders on the next synth pass. Rules for the ambiguous cases:

    * ``original`` in exactly one segment (normal): that segment is fixed and re-renders.
    * ``original`` in no segment (already-diverged text, a token straddling a narration/quote
      boundary, or a pre-attribution line with no segments): ``line.segments`` untouched — the
      ``line.text`` fix still stands; nothing spoken carries the token, so nothing to re-render.
    * ``original`` in multiple segments / twice in one segment: only the **first containing**
      segment is edited, and ``replace(..., 1)`` fixes only its first occurrence — deterministic,
      mirroring the line-level rule.

    Returns ``True`` iff it **dirtied a segment** (the audio-changing case), so the caller can
    re-open the render stages; the line-text-only path returns ``False`` (nothing spoken changed).
    Attribution is untouched (a spelling fix does not change who speaks). Raises ``ValueError``
    if the suggestion id is not on the line.
    """
    suggestion = _find_suggestion(line, suggestion_id)
    line.text = line.text.replace(suggestion.original, suggestion.suggested, 1)
    suggestion.status = ReviewStatus.APPROVED
    for segment in line.segments:
        if suggestion.original in segment.text:
            segment.text = segment.text.replace(suggestion.original, suggestion.suggested, 1)
            segment.audio_cache_key = None
            segment.audio_status = ReviewStatus.PENDING
            return True
    return False


def reject_suggestion(line: Line, suggestion_id: str) -> None:
    """Reject a suggestion: ``line.text`` unchanged, status -> REJECTED.

    Raises ``ValueError`` if the suggestion id is not on the line.
    """
    suggestion = _find_suggestion(line, suggestion_id)
    suggestion.status = ReviewStatus.REJECTED


def edit_line_text(
    line: Line,
    new_text: str,
    project: Project,
    *,
    segmenter: Segmenter | None = None,
) -> bool:
    """Set ``line.text`` to a user-typed value and **re-segment** the line. Returns re-segmented?

    A whole-line rewrite cannot be token-mapped onto the old segments, so we rebuild them from
    scratch via :func:`~casttrophizer.attribution.segmentation.build_line_segments` (offline; no
    LLM): narration -> APPROVED narrator, quote -> **NEEDS_REVIEW** (``speaker_id=None``). The new
    segments carry ``None`` cache keys, so they re-render on the next synth pass.

    This **re-opens attribution** (criterion 1) for the line's quotes — each new quote segment is
    NEEDS_REVIEW and re-appears in the attribution panel. Re-attribution is **manual**: this
    module is pure/offline and never calls the LLM, so new quotes sit at narrator-fallback until
    the user re-reviews. (Full-re-segment discards any careful per-quote attribution on the line
    — the accepted cost of not span-mapping the edit.)

    No-op guard: if the new spans' texts equal the current segments' texts in order (e.g. a
    whitespace-only edit), **mutate nothing** — leave ``line.text``/``line.segments`` untouched,
    create no narrator, and return ``False`` — an inert edit neither re-opens attribution nor
    forces a re-render. Otherwise set the text, rebuild the segments, and return ``True``.
    ``project`` supplies the reserved narrator (via ``ensure_narrator``, only on a real rebuild);
    ``segmenter`` defaults to a :class:`~casttrophizer.attribution.segmenter.QuoteSegmenter`.
    """
    seg = segmenter or QuoteSegmenter()
    # Decide the no-op FIRST (span texts depend only on the split, not the narrator), so an inert
    # edit doesn't set ``line.text`` or append a narrator as a side effect.
    span_texts = [span.text for span in seg.split(new_text)]
    if span_texts == [s.text for s in line.segments]:
        return False  # inert edit (e.g. whitespace-only): keep everything as-is
    line.text = new_text
    line.segments = build_line_segments(new_text, ensure_narrator(project), seg)
    return True


# --------------------------------------------------------------------------- #
# attribution (§B.2)
# --------------------------------------------------------------------------- #
def approve_attribution(segment: Segment) -> None:
    """Confirm the proposed speaker: ``review_status`` -> APPROVED (speaker/role unchanged)."""
    segment.review_status = ReviewStatus.APPROVED


def reject_attribution(segment: Segment) -> None:
    """Reject the proposal without choosing a replacement: ``review_status`` -> REJECTED.

    A REJECTED segment is NOT a gate blocker (only NEEDS_REVIEW is); ``speaker_id``/``role``
    are left as-is. The intended UI flow is reject -> reassign so the segment gets a valid
    speaker again; if the rejected segment ends up with a voice-less speaker, the voice
    precheck (gate criterion 3) catches it.
    """
    segment.review_status = ReviewStatus.REJECTED


def set_segment_speaker(
    segment: Segment,
    project: Project,
    *,
    speaker_id: str | None,
    role: SpeakerRole,
    approve: bool = True,
) -> None:
    """Override the attribution: point ``segment`` at ``speaker_id`` + ``role``.

    ``speaker_id`` of ``None`` stores ``segment.speaker_id = None``, which **renders as the
    reserved narrator** at synthesis (see
    :func:`~casttrophizer.domain.models.find_narrator`); to attribute a specific character,
    pass that speaker's id. A non-None id must already exist in ``project.speakers`` (raises
    ``ValueError`` otherwise). By default the human's choice marks ``review_status`` APPROVED;
    pass ``approve=False`` to leave the status untouched (note: this neither approves nor flags —
    a still-``NEEDS_REVIEW`` segment keeps blocking the review gate).

    Cache consequence: changing the resolved speaker changes the segment's
    :meth:`AudioCache.key_for`, so the synthesize stage re-renders this segment.
    """
    if speaker_id is not None and not any(sp.id == speaker_id for sp in project.speakers):
        raise ValueError(f"no speaker {speaker_id!r} in project")
    segment.speaker_id = speaker_id
    segment.role = role
    if approve:
        segment.review_status = ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# reassign to a different / new speaker (§B.3)
# --------------------------------------------------------------------------- #
def create_character(project: Project, name: str) -> Speaker:
    """Register (or reuse) a CHARACTER speaker for ``name``.

    Delegates to ``attribution.policy.resolve_speaker`` so a case-insensitive existing
    match is reused (no duplicate) and an unknown name appends a new CHARACTER speaker —
    the one matching rule the attribute stage uses.
    """
    narrator = ensure_narrator(project)
    return resolve_speaker(project, narrator, name)


def reassign_segment_to_new_speaker(segment: Segment, project: Project, name: str) -> Speaker:
    """``create_character(name)`` then point ``segment`` at it (CHARACTER, APPROVED).

    Convenience for the UI "assign to new character" path. Returns the existing or
    newly-created :class:`Speaker`.
    """
    speaker = create_character(project, name)
    set_segment_speaker(
        segment,
        project,
        speaker_id=speaker.id,
        role=SpeakerRole.CHARACTER,
        approve=True,
    )
    return speaker


# --------------------------------------------------------------------------- #
# voice-clip registration & assignment (§B.4)
# --------------------------------------------------------------------------- #
def register_voice_clip(project: Project, source_path: str | Path, label: str) -> VoiceClip:
    """Register a user-picked clip as a READ-ONLY input and append it to ``project``.

    The file is referenced by absolute path and **never copied or mutated** (CLAUDE.md:
    source clips are read-only inputs). Raises ``ValueError`` immediately if the path does
    not exist, so a typo surfaces at assignment time rather than as a later synthesize
    precheck failure. Does not validate that the file is playable audio (the UI may
    pre-validate).
    """
    path = Path(source_path)
    if not path.is_file():
        raise ValueError(f"voice clip source path does not exist: {path}")
    # Store an ABSOLUTE path: a relative one would resolve against whatever CWD the app (or a
    # resumed run / the future UI) happens to have later, and the clip would read as missing.
    clip = VoiceClip(id=new_id("voice"), source_path=str(path.resolve()), label=label)
    project.voice_clips.append(clip)
    return clip


def assign_voice(speaker: Speaker, voice_clip: VoiceClip) -> None:
    """Map ``speaker`` (narrator OR character — identical handling) to ``voice_clip``.

    Cache consequence: changing a speaker's ``voice_clip_id`` changes
    :meth:`AudioCache.key_for` for all of that speaker's segments, so the synthesize stage
    re-renders them on its next run.
    """
    speaker.voice_clip_id = voice_clip.id


def assign_voice_by_ids(project: Project, *, speaker_id: str, voice_clip_id: str) -> None:
    """Lookup-and-assign convenience; raises ``ValueError`` if either id is absent."""
    speaker = next((sp for sp in project.speakers if sp.id == speaker_id), None)
    if speaker is None:
        raise ValueError(f"no speaker {speaker_id!r} in project")
    clip = next((vc for vc in project.voice_clips if vc.id == voice_clip_id), None)
    if clip is None:
        raise ValueError(f"no voice clip {voice_clip_id!r} in project")
    assign_voice(speaker, clip)


def unassign_voice(speaker: Speaker) -> None:
    """Clear ``speaker.voice_clip_id`` (-> None).

    Re-introduces a voice blocker (gate criterion 3); the service must invalidate the
    review-complete flag (see :class:`ReviewService`).
    """
    speaker.voice_clip_id = None


def set_speaker_category(speaker: Speaker, category: VoiceCategory) -> None:
    """Set ``speaker.category`` (the voice bucket that drives ``--rest``/bulk default selection).

    Category affects **neither** the review gate **nor** the :class:`AudioCache` key (that keys
    on the resolved ``voice_clip_id``, never the category) — it only steers which default clip
    ``assign-voice --rest``/bulk-assign picks — so this needs no review-flag invalidation and
    forces no re-render.
    """
    speaker.category = category


# --------------------------------------------------------------------------- #
# per-segment audio review (§B.5)
# --------------------------------------------------------------------------- #
def approve_audio(segment: Segment) -> None:
    """Approve a segment's rendered take: ``audio_status`` COMPLETED -> APPROVED.

    A pure curation marker: both COMPLETED and APPROVED are ``RENDERED_STATUSES`` (the synth
    skip-gate treats them identically and assemble stitches both), so approval changes nothing
    about the render — it just records the user's sign-off so the panel can show "N of M
    approved" and filter the un-approved. Idempotent for an already-APPROVED segment.
    """
    segment.audio_status = ReviewStatus.APPROVED


def reroll_audio(segment: Segment, *, seed: int) -> None:
    """Re-roll a segment for a fresh take: set a new ``audio_seed``, clear its key, -> PENDING.

    Stamps a new ``audio_seed`` (folded into :meth:`AudioCache.key_for` -> a new key -> a distinct
    WAV, even when the project pins a global ``tts_params["seed"]``), nulls the stale
    ``audio_cache_key`` and resets ``audio_status`` to PENDING — mirroring the synth FAILED
    clear-key path so assemble never stitches the old take. The caller (:class:`ReviewService`)
    then either renders the segment now (regenerate) or defers it to the next full run (reject).
    """
    segment.audio_seed = seed
    segment.audio_cache_key = None
    segment.audio_status = ReviewStatus.PENDING


def fail_audio(segment: Segment) -> None:
    """Mark a segment's render FAILED (clearing its key), mirroring the synth FAILED branch.

    Used when a regenerate-now render fails: keeping the segment PENDING would make its row vanish
    from the audio-review list (it is neither rendered nor failed), a silent black hole. FAILED is
    **not** in ``RENDERED_STATUSES``, so the row stays visible with a failed badge **and** the next
    full run re-renders it. The key is nulled so assemble never stitches a stale/absent WAV
    (symmetric with :func:`reroll_audio` and ``synthesize_chapter``'s failure branch).
    """
    segment.audio_cache_key = None
    segment.audio_status = ReviewStatus.FAILED

"""``ReviewService``: the persistence-owning wrapper around the pure review actions.

The UI constructs one :class:`ReviewService` per loaded project and calls it from the Qt
main thread — these are cheap, synchronous mutations plus a small atomic JSON write, so
they do **not** need a worker thread (unlike parse/attribute/synthesize). Each method:

1. applies the corresponding pure action from :mod:`casttrophizer.review.actions`,
2. if the action *could (re)introduce a blocker*, invalidates the review-complete flag
   (pops ``stage_status[REVIEW]``) so a later edit can't sail past a stale COMPLETED, then
3. persists via the injected :class:`~casttrophizer.workspace.store.WorkspaceStore`.

``is_complete`` stays status-driven on the stage (the four other stages rely on that
convention); the synthesize voice precheck is the final backstop regardless.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.domain.enums import SpeakerRole, StageName, VoiceCategory
from casttrophizer.domain.models import Line, Project, Segment, Speaker, VoiceClip
from casttrophizer.review import actions
from casttrophizer.review.gate import ReviewBlockers, review_blockers
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["ReviewService"]


class ReviewService:
    """Applies review actions to one project, invalidating the gate flag and persisting."""

    def __init__(self, store: WorkspaceStore, project: Project) -> None:
        self._store = store
        self._project = project

    @property
    def project(self) -> Project:
        """The in-memory project this service mutates."""
        return self._project

    def blockers(self) -> ReviewBlockers:
        """What's still blocking review (delegates to :func:`review_blockers`)."""
        return review_blockers(self._project)

    def _invalidate_review(self) -> None:
        """Pop ``stage_status[REVIEW]`` so a later blocker re-opens the gate (§A.3a)."""
        self._project.stage_status.pop(str(StageName.REVIEW), None)

    def _invalidate_render(self) -> None:
        """Pop ``stage_status[SYNTHESIZE]`` **and** ``[ASSEMBLE]`` so a dirtied segment re-renders.

        A review edit that changes a segment's audio-cache inputs — its ``text``, its resolved
        ``voice_clip_id`` (a speaker/voice change), or the segment set itself (a re-segment) —
        must re-open **both** render stages. Otherwise the runner skips the still-COMPLETED
        SYNTHESIZE (so the dirtied segments never re-render) and ASSEMBLE stays COMPLETED (so the
        stale M4B is never rebuilt). SynthesizeStage is cache-aware — it re-renders only the
        segments whose recomputed key changed and cache-hits the rest — so re-opening it is cheap
        and correct. Popping a not-present / not-yet-complete stage is a harmless no-op.
        """
        self._project.stage_status.pop(str(StageName.SYNTHESIZE), None)
        self._project.stage_status.pop(str(StageName.ASSEMBLE), None)

    def _save(self) -> None:
        self._store.save(self._project)

    # ----------------------------------------------------------------- #
    # text suggestions
    # ----------------------------------------------------------------- #
    def accept_suggestion(self, line: Line, suggestion_id: str) -> None:
        """Apply a suggestion (resolves a PENDING blocker, propagates to the segment) and persist.

        Accepting only *resolves* a criterion-2 blocker and never adds one (attribution is
        untouched), so — unlike an attribution reassign — this needs no review-flag invalidation.
        When it actually dirtied a segment (the propagation case), it re-opens the render stages so
        the corrected audio re-renders; the line-text-only case changes no audio, so it does not.
        """
        dirtied = actions.accept_suggestion(line, suggestion_id)
        if dirtied:
            self._invalidate_render()
        self._save()

    def reject_suggestion(self, line: Line, suggestion_id: str) -> None:
        """Reject a suggestion (resolves a PENDING blocker) and persist."""
        actions.reject_suggestion(line, suggestion_id)
        self._save()

    def edit_line_text(self, line: Line, new_text: str) -> None:
        """Edit a line's text — re-segmenting it — and persist.

        Delegates to :func:`actions.edit_line_text`, which rebuilds the line's segments (unless
        the edit is inert). When it re-segmented, the new quotes are NEEDS_REVIEW (a fresh
        criterion-1 blocker) so invalidate the review flag, and the new segments have null cache
        keys so also invalidate render — a previously-COMPLETED gate/render must re-open. An inert
        edit changes nothing, so both flags are left alone.
        """
        resegmented = actions.edit_line_text(line, new_text, self._project)
        if resegmented:
            self._invalidate_review()
            self._invalidate_render()
        self._save()

    # ----------------------------------------------------------------- #
    # attribution
    # ----------------------------------------------------------------- #
    def approve_attribution(self, segment: Segment) -> None:
        """Approve a segment's attribution (resolves a NEEDS_REVIEW blocker) and persist."""
        actions.approve_attribution(segment)
        self._save()

    def reject_attribution(self, segment: Segment) -> None:
        """Reject a segment's attribution and persist (REJECTED does not itself block)."""
        actions.reject_attribution(segment)
        self._save()

    def set_segment_speaker(
        self,
        segment: Segment,
        *,
        speaker_id: str | None,
        role: SpeakerRole,
        approve: bool = True,
    ) -> None:
        """Override a segment's speaker and persist.

        Invalidates the review flag: a new speaker without a voice can introduce a
        criterion-3 blocker, and ``approve=False`` could leave the segment unresolved. Also
        invalidates render: the speaker change changes the segment's resolved ``voice_clip_id`` —
        and thus its :meth:`AudioCache.key_for` — so it must re-render.
        """
        actions.set_segment_speaker(
            segment, self._project, speaker_id=speaker_id, role=role, approve=approve
        )
        self._invalidate_review()
        self._invalidate_render()
        self._save()

    def reassign_segment_to_new_speaker(self, segment: Segment, name: str) -> Speaker:
        """Reassign a segment to a (possibly new) character and persist.

        Invalidates the review flag: the (new) character may lack a voice -> criterion-3
        blocker. Also invalidates render: repointing the segment's speaker changes its resolved
        ``voice_clip_id`` (hence its cache key), so it must re-render.
        """
        speaker = actions.reassign_segment_to_new_speaker(segment, self._project, name)
        self._invalidate_review()
        self._invalidate_render()
        self._save()
        return speaker

    # ----------------------------------------------------------------- #
    # voice clips
    # ----------------------------------------------------------------- #
    def register_voice_clip(self, source_path: str | Path, label: str) -> VoiceClip:
        """Register a read-only voice clip and persist (raises ValueError on a bad path)."""
        clip = actions.register_voice_clip(self._project, source_path, label)
        self._save()
        return clip

    def assign_voice(self, speaker: Speaker, voice_clip: VoiceClip) -> None:
        """Assign a voice to a speaker (clears a criterion-3 blocker) and persist.

        Invalidates render: the new clip changes :meth:`AudioCache.key_for` for every one of that
        speaker's segments, so they must re-render.
        """
        actions.assign_voice(speaker, voice_clip)
        self._invalidate_render()
        self._save()

    def assign_voices(self, pairs: list[tuple[Speaker, VoiceClip]]) -> None:
        """Assign many ``(speaker, clip)`` pairs in memory, then persist **once**.

        The batch analogue of :meth:`assign_voice` for ``castrun assign-voice --rest`` —
        a large cast shouldn't fsync per speaker (mirrors the CLI auto-accept single-write
        pattern). Any ``VoiceClip`` already registered on ``self.project`` (e.g. via
        ``actions.register_voice_clip``) is persisted by the same single save. Assigning a
        voice only *clears* the criterion-3 blocker, so — like :meth:`assign_voice` — no
        review-flag invalidation is needed; but each assignment changes those speakers' cache
        keys, so it invalidates render.
        """
        for speaker, voice_clip in pairs:
            actions.assign_voice(speaker, voice_clip)
        if pairs:
            self._invalidate_render()
        self._save()

    def assign_voice_by_ids(self, *, speaker_id: str, voice_clip_id: str) -> None:
        """Assign a voice by ids (clears a criterion-3 blocker) and persist.

        Invalidates render (the assigned clip changes that speaker's segments' cache keys).
        """
        actions.assign_voice_by_ids(
            self._project, speaker_id=speaker_id, voice_clip_id=voice_clip_id
        )
        self._invalidate_render()
        self._save()

    def unassign_voice(self, speaker: Speaker) -> None:
        """Clear a speaker's voice (re-introduces a criterion-3 blocker) and persist.

        Invalidates the review flag so a previously-COMPLETED gate re-opens, and render: clearing
        the clip changes that speaker's segments' cache keys.
        """
        actions.unassign_voice(speaker)
        self._invalidate_review()
        self._invalidate_render()
        self._save()

    def set_speaker_category(self, speaker: Speaker, category: VoiceCategory) -> None:
        """Set a speaker's voice category and persist — **no** review-flag invalidation.

        Category steers only which default clip ``--rest``/bulk-assign picks; it touches neither
        the review gate nor the :class:`AudioCache` key (which keys on the resolved
        ``voice_clip_id``), so it cannot introduce a blocker and forces no re-render.
        """
        actions.set_speaker_category(speaker, category)
        self._save()

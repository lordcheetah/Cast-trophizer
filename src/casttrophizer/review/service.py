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

from casttrophizer.domain.enums import SpeakerRole, StageName
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

    def _save(self) -> None:
        self._store.save(self._project)

    # ----------------------------------------------------------------- #
    # text suggestions
    # ----------------------------------------------------------------- #
    def accept_suggestion(self, line: Line, suggestion_id: str) -> None:
        """Apply a suggestion (resolves a PENDING blocker) and persist."""
        actions.accept_suggestion(line, suggestion_id)
        self._save()

    def reject_suggestion(self, line: Line, suggestion_id: str) -> None:
        """Reject a suggestion (resolves a PENDING blocker) and persist."""
        actions.reject_suggestion(line, suggestion_id)
        self._save()

    def edit_line_text(self, line: Line, new_text: str) -> None:
        """Edit a line's text (segments untouched) and persist."""
        actions.edit_line_text(line, new_text)
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
        criterion-3 blocker, and ``approve=False`` could leave the segment unresolved.
        """
        actions.set_segment_speaker(
            segment, self._project, speaker_id=speaker_id, role=role, approve=approve
        )
        self._invalidate_review()
        self._save()

    def reassign_segment_to_new_speaker(self, segment: Segment, name: str) -> Speaker:
        """Reassign a segment to a (possibly new) character and persist.

        Invalidates the review flag: the (new) character may lack a voice -> criterion-3
        blocker.
        """
        speaker = actions.reassign_segment_to_new_speaker(segment, self._project, name)
        self._invalidate_review()
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
        """Assign a voice to a speaker (clears a criterion-3 blocker) and persist."""
        actions.assign_voice(speaker, voice_clip)
        self._save()

    def assign_voices(self, pairs: list[tuple[Speaker, VoiceClip]]) -> None:
        """Assign many ``(speaker, clip)`` pairs in memory, then persist **once**.

        The batch analogue of :meth:`assign_voice` for ``castrun assign-voice --rest`` —
        a large cast shouldn't fsync per speaker (mirrors the CLI auto-accept single-write
        pattern). Any ``VoiceClip`` already registered on ``self.project`` (e.g. via
        ``actions.register_voice_clip``) is persisted by the same single save. Assigning a
        voice only *clears* the criterion-3 blocker, so — like :meth:`assign_voice` — no
        review-flag invalidation is needed.
        """
        for speaker, voice_clip in pairs:
            actions.assign_voice(speaker, voice_clip)
        self._save()

    def assign_voice_by_ids(self, *, speaker_id: str, voice_clip_id: str) -> None:
        """Assign a voice by ids (clears a criterion-3 blocker) and persist."""
        actions.assign_voice_by_ids(
            self._project, speaker_id=speaker_id, voice_clip_id=voice_clip_id
        )
        self._save()

    def unassign_voice(self, speaker: Speaker) -> None:
        """Clear a speaker's voice (re-introduces a criterion-3 blocker) and persist.

        Invalidates the review flag so a previously-COMPLETED gate re-opens.
        """
        actions.unassign_voice(speaker)
        self._invalidate_review()
        self._save()

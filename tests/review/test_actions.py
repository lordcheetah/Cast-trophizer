"""Pure review-action unit tests (no store, no Qt, no providers).

Covers every operation in :mod:`casttrophizer.review.actions`: text accept/reject/edit,
attribution approve/reject/override, speaker create/reassign, voice register/assign/unassign,
plus the indirect cache-key consequence and read-only-input guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer.attribution.policy import ensure_narrator, resolve_speaker
from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Line, Project, Segment, Speaker, TextSuggestion
from casttrophizer.review import actions
from casttrophizer.review.gate import review_blockers
from casttrophizer.workspace.audio_cache import AudioCache


def _pending_suggestion(original: str, suggested: str) -> TextSuggestion:
    return TextSuggestion(
        id=new_id("sug"),
        original=original,
        suggested=suggested,
        reason="spellcheck",
        confidence=0.7,
        status=ReviewStatus.PENDING,
    )


def _seg(text: str) -> Segment:
    """A minimal APPROVED narrator segment carrying ``text`` (for propagation tests)."""
    return Segment(
        id=new_id("seg"),
        text=text,
        speaker_id=None,
        role=SpeakerRole.NARRATOR,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )


def _first_line(project: Project) -> Line:
    return project.book.chapters[0].lines[0]


def _segment_with_status(project: Project, status: ReviewStatus) -> Segment:
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.review_status == status
    )


def _speaker(project: Project, name: str) -> Speaker:
    return next(sp for sp in project.speakers if sp.name == name)


# --------------------------------------------------------------------------- #
# text suggestions
# --------------------------------------------------------------------------- #
def test_accept_suggestion_applies_text_and_approves(review_ready_project: Project) -> None:
    line = _first_line(review_ready_project)
    suggestion = line.suggestions[0]
    before = line.text
    actions.accept_suggestion(line, suggestion.id)
    # Targeted first-occurrence replacement of the token, NOT a whole-line overwrite.
    assert line.text == before.replace(suggestion.original, suggestion.suggested, 1)
    assert suggestion.status == ReviewStatus.APPROVED


def test_accept_suggestion_replaces_token_not_whole_line() -> None:
    """A token-level suggestion must not overwrite the whole line (regression).

    A PENDING spellcheck/OCR suggestion stores just the TOKEN in original/suggested (e.g.
    'narrarator' -> 'narrator'). Accepting it must fix that token in place and preserve the
    rest of the sentence, not collapse the line to the token.
    """
    line = Line(id=new_id("ln"), chapter_id="c", order=1, text="The narrarator spoke softly.")
    line.suggestions.append(
        TextSuggestion(
            id=new_id("sug"),
            original="narrarator",
            suggested="narrator",
            reason="spellcheck",
            confidence=0.7,
            status=ReviewStatus.PENDING,
        )
    )
    actions.accept_suggestion(line, line.suggestions[0].id)
    assert line.text == "The narrator spoke softly."
    assert line.suggestions[0].status == ReviewStatus.APPROVED


def test_register_voice_clip_stores_absolute_path(
    review_ready_project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative clip path is stored ABSOLUTE so it resolves from any CWD (regression).

    A relative ``source_path`` would resolve against whatever CWD a later/resumed run has,
    reading the clip as missing.
    """
    clip = tmp_path / "voices" / "narr.wav"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"WAV")
    monkeypatch.chdir(tmp_path)

    vc = actions.register_voice_clip(review_ready_project, Path("voices") / "narr.wav", "narrator")
    assert Path(vc.source_path).is_absolute()
    assert Path(vc.source_path) == clip.resolve()


def test_reject_suggestion_leaves_text(review_ready_project: Project) -> None:
    line = review_ready_project.book.chapters[0].lines[1]
    original_text = line.text
    suggestion = line.suggestions[0]
    actions.reject_suggestion(line, suggestion.id)
    assert line.text == original_text
    assert suggestion.status == ReviewStatus.REJECTED


def test_suggestion_action_raises_on_unknown_id(review_ready_project: Project) -> None:
    line = _first_line(review_ready_project)
    with pytest.raises(ValueError):
        actions.accept_suggestion(line, "sug_missing")


def test_accept_suggestion_propagates_to_matching_segment_and_forces_rerender(
    review_ready_project: Project,
) -> None:
    """Accepting fixes the spoken segment too: text changes, cache reset, key differs."""
    project = review_ready_project
    seg = _seg("The narrarator spoke.")  # speaker_id=None -> resolves to the narrator
    line = Line(
        id=new_id("ln"),
        chapter_id="c",
        order=9,
        text="The narrarator spoke.",
        segments=[seg],
        suggestions=[_pending_suggestion("narrarator", "narrator")],
    )
    key_before = AudioCache.key_for(seg, project)
    seg.audio_cache_key = key_before  # simulate an already-rendered segment
    seg.audio_status = ReviewStatus.COMPLETED

    actions.accept_suggestion(line, line.suggestions[0].id)

    assert line.text == "The narrator spoke."
    assert seg.text == "The narrator spoke."  # propagated to the segment
    assert seg.audio_cache_key is None  # stale key nulled
    assert seg.audio_status == ReviewStatus.PENDING
    assert AudioCache.key_for(seg, project) != key_before  # recomputed key differs -> re-render


def test_accept_suggestion_double_occurrence_first_segment_first_occurrence() -> None:
    """Multiple matches: only the first containing segment, and only its first occurrence."""
    seg_a = _seg("foo foo")
    seg_b = _seg("and foo")
    line = Line(
        id=new_id("ln"),
        chapter_id="c",
        order=1,
        text="foo foo and foo",
        segments=[seg_a, seg_b],
        suggestions=[_pending_suggestion("foo", "bar")],
    )
    actions.accept_suggestion(line, line.suggestions[0].id)
    assert line.text == "bar foo and foo"  # line-level: first occurrence only
    assert seg_a.text == "bar foo"  # first containing segment, first occurrence only
    assert seg_b.text == "and foo"  # later segment untouched


def test_accept_suggestion_token_in_no_segment_leaves_segments_untouched() -> None:
    """Token absent from every segment: ``line.text`` still fixed, segments untouched."""
    seg = _seg("an unrelated span")
    seg.audio_cache_key = "STALE"
    seg.audio_status = ReviewStatus.COMPLETED
    line = Line(
        id=new_id("ln"),
        chapter_id="c",
        order=1,
        text="The narrarator spoke.",
        segments=[seg],
        suggestions=[_pending_suggestion("narrarator", "narrator")],
    )
    actions.accept_suggestion(line, line.suggestions[0].id)
    assert line.text == "The narrator spoke."  # line-level fix still stands
    assert seg.text == "an unrelated span"  # untouched
    assert seg.audio_cache_key == "STALE"  # not reset (nothing to re-render)
    assert seg.audio_status == ReviewStatus.COMPLETED


def test_edit_line_text_resegments_and_reopens_attribution(review_ready_project: Project) -> None:
    """A whole-line rewrite re-segments: a new quote returns to NEEDS_REVIEW (re-opens crit 1)."""
    project = review_ready_project
    line = _first_line(project)  # line0: two APPROVED segments, no NEEDS_REVIEW
    before = len(review_blockers(project).needs_attribution)

    resegmented = actions.edit_line_text(line, 'Narration. "New quote," said Zed.', project)

    assert resegmented is True
    assert line.text == 'Narration. "New quote," said Zed.'
    quote = next(s for s in line.segments if s.review_status == ReviewStatus.NEEDS_REVIEW)
    assert quote.speaker_id is None  # narrator-fallback until manually re-attributed
    assert quote.audio_cache_key is None  # fresh segment -> re-renders
    assert len(review_blockers(project).needs_attribution) > before  # attribution re-opened


def test_edit_line_text_narration_only_does_not_reopen_attribution(
    review_ready_project: Project,
) -> None:
    project = review_ready_project
    line = _first_line(project)
    narrator = next(sp for sp in project.speakers if sp.role == SpeakerRole.NARRATOR)
    before = len(review_blockers(project).needs_attribution)

    resegmented = actions.edit_line_text(line, "Just plain narration now.", project)

    assert resegmented is True
    assert len(line.segments) == 1
    assert line.segments[0].review_status == ReviewStatus.APPROVED
    assert line.segments[0].speaker_id == narrator.id
    assert len(review_blockers(project).needs_attribution) == before  # no new blocker


def test_edit_line_text_inert_edit_keeps_segments(review_ready_project: Project) -> None:
    """A no-op (whitespace-only) edit returns False and leaves the segments in place."""
    project = review_ready_project
    line = _first_line(project)
    seg_ids_before = [s.id for s in line.segments]

    resegmented = actions.edit_line_text(line, line.text, project)  # identical text

    assert resegmented is False
    assert [s.id for s in line.segments] == seg_ids_before  # same objects, not rebuilt


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #
def test_approve_attribution(review_ready_project: Project) -> None:
    seg = _segment_with_status(review_ready_project, ReviewStatus.NEEDS_REVIEW)
    speaker_before, role_before = seg.speaker_id, seg.role
    actions.approve_attribution(seg)
    assert seg.review_status == ReviewStatus.APPROVED
    assert (seg.speaker_id, seg.role) == (speaker_before, role_before)


def test_reject_attribution(review_ready_project: Project) -> None:
    seg = _segment_with_status(review_ready_project, ReviewStatus.NEEDS_REVIEW)
    actions.reject_attribution(seg)
    assert seg.review_status == ReviewStatus.REJECTED


def test_set_segment_speaker_repoints_and_approves(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    alice = _speaker(project, "Alice")
    actions.set_segment_speaker(
        seg, project, speaker_id=alice.id, role=SpeakerRole.CHARACTER, approve=True
    )
    assert seg.speaker_id == alice.id
    assert seg.role == SpeakerRole.CHARACTER
    assert seg.review_status == ReviewStatus.APPROVED


def test_set_segment_speaker_no_approve(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    alice = _speaker(project, "Alice")
    actions.set_segment_speaker(
        seg, project, speaker_id=alice.id, role=SpeakerRole.CHARACTER, approve=False
    )
    assert seg.review_status == ReviewStatus.NEEDS_REVIEW  # untouched


def test_set_segment_speaker_raises_unknown(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    with pytest.raises(ValueError):
        actions.set_segment_speaker(seg, project, speaker_id="spk_nope", role=SpeakerRole.CHARACTER)


# --------------------------------------------------------------------------- #
# create / reassign speaker
# --------------------------------------------------------------------------- #
def test_create_character_new_and_reuse(review_ready_project: Project) -> None:
    project = review_ready_project
    n_before = len(project.speakers)
    carol = actions.create_character(project, "Carol")
    assert carol.role == SpeakerRole.CHARACTER
    assert len(project.speakers) == n_before + 1

    # case-insensitive reuse -> no duplicate; agrees with resolve_speaker.
    again = actions.create_character(project, "carol")
    assert again is carol
    assert len(project.speakers) == n_before + 1
    narrator = ensure_narrator(project)
    assert resolve_speaker(project, narrator, "CAROL") is carol


def test_reassign_segment_to_new_speaker(review_ready_project: Project) -> None:
    project = review_ready_project
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    speaker = actions.reassign_segment_to_new_speaker(seg, project, "Dave")
    assert speaker.name == "Dave"
    assert speaker.role == SpeakerRole.CHARACTER
    assert seg.speaker_id == speaker.id
    assert seg.role == SpeakerRole.CHARACTER
    assert seg.review_status == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# voice clips
# --------------------------------------------------------------------------- #
def test_register_voice_clip_records_reference_no_copy(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    tmp_workspace,
) -> None:
    project = review_ready_project
    n_before = len(project.voice_clips)
    src = fake_voice_clips[0]
    clip = actions.register_voice_clip(project, src, "Bob's voice")

    assert clip in project.voice_clips
    assert len(project.voice_clips) == n_before + 1
    assert clip.source_path == str(Path(src))
    assert clip.label == "Bob's voice"
    # READ-ONLY input: nothing copied into the workspace audio dir.
    audio_dir = tmp_workspace.layout.audio_dir
    copied = list(audio_dir.glob("*")) if audio_dir.exists() else []
    assert copied == []


def test_register_voice_clip_raises_on_missing_path(
    review_ready_project: Project, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        actions.register_voice_clip(review_ready_project, tmp_path / "nope.wav", "ghost")


def test_assign_voice_clears_blocker(
    review_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    project = review_ready_project
    assert "Bob" in unresolved_voices(project)
    bob = _speaker(project, "Bob")
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Bob")
    actions.assign_voice(bob, clip)
    assert bob.voice_clip_id == clip.id
    assert "Bob" not in unresolved_voices(project)


def test_assign_voice_narrator_identical(
    review_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    project = review_ready_project
    narrator = _speaker(project, "narrator")
    narrator.voice_clip_id = None  # un-assign to prove the round-trip
    assert "narrator" in unresolved_voices(project)
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Narr")
    actions.assign_voice(narrator, clip)
    assert "narrator" not in unresolved_voices(project)


def test_assign_voice_by_ids_and_unknowns(review_ready_project: Project) -> None:
    project = review_ready_project
    bob = _speaker(project, "Bob")
    clip = project.voice_clips[0]
    actions.assign_voice_by_ids(project, speaker_id=bob.id, voice_clip_id=clip.id)
    assert bob.voice_clip_id == clip.id

    with pytest.raises(ValueError):
        actions.assign_voice_by_ids(project, speaker_id="spk_nope", voice_clip_id=clip.id)
    with pytest.raises(ValueError):
        actions.assign_voice_by_ids(project, speaker_id=bob.id, voice_clip_id="voice_nope")


def test_unassign_voice_reintroduces_blocker(review_ready_project: Project) -> None:
    project = review_ready_project
    alice = _speaker(project, "Alice")
    assert "Alice" not in unresolved_voices(project)
    actions.unassign_voice(alice)
    assert alice.voice_clip_id is None
    assert "Alice" in unresolved_voices(project)


# --------------------------------------------------------------------------- #
# cache-key consequence (indirect; no TTS)
# --------------------------------------------------------------------------- #
def test_set_segment_speaker_changes_cache_key(review_ready_project: Project) -> None:
    project = review_ready_project
    # The NEEDS_REVIEW Bob segment (Bob has no voice) -> repoint to voiced Alice.
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)
    alice = _speaker(project, "Alice")
    key_before = AudioCache.key_for(seg, project)
    actions.set_segment_speaker(seg, project, speaker_id=alice.id, role=SpeakerRole.CHARACTER)
    key_after = AudioCache.key_for(seg, project)
    assert key_before != key_after  # resolved voice changed -> re-render


def test_assign_voice_changes_cache_key(
    review_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    project = review_ready_project
    bob = _speaker(project, "Bob")
    seg = _segment_with_status(project, ReviewStatus.NEEDS_REVIEW)  # a Bob segment
    assert seg.speaker_id == bob.id
    key_before = AudioCache.key_for(seg, project)
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Bob")
    actions.assign_voice(bob, clip)
    key_after = AudioCache.key_for(seg, project)
    assert key_before != key_after  # voice now resolves -> re-render

"""Gate three-criteria isolation matrix + false-positive traps (pure, offline).

The existing ``test_gate.py`` resolves the three blockers *cumulatively*. This file
isolates each criterion against an OTHERWISE-CLEAN project so a regression in any single
criterion (or a false-positive on AUTO_APPLIED / bare REJECTED) is caught directly:

* ONLY criterion 1 unmet (a NEEDS_REVIEW segment) -> incomplete; exact id reported.
* ONLY criterion 2 unmet (a PENDING suggestion) -> incomplete; exact id reported.
* ONLY criterion 3 unmet (an unassigned voice) -> incomplete; exact name reported.
* ALL three clear -> complete.
* An AUTO_APPLIED suggestion does NOT block (false-positive trap).
* A bare REJECTED attribution does NOT block (false-positive trap).

It builds a fresh, fully-clean project so each criterion is toggled in isolation rather
than relying on the mixed ``review_ready_project``.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    TextSuggestion,
    VoiceClip,
)
from casttrophizer.review.gate import is_review_complete, review_blockers


def _silent_wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * 100)
    return path


@pytest.fixture
def clean_project(tmp_path: Path) -> Project:
    """A fully review-complete project: one narrator + one voiced character, no blockers.

    Every segment APPROVED, the one suggestion AUTO_APPLIED, every referenced speaker has an
    existing voice clip. Toggling exactly one thing makes exactly one criterion fail.
    """
    narr_wav = _silent_wav(tmp_path / "voices" / "narrator.wav")
    alice_wav = _silent_wav(tmp_path / "voices" / "alice.wav")
    narr_clip = VoiceClip(id=new_id("voice"), source_path=str(narr_wav), label="N")
    alice_clip = VoiceClip(id=new_id("voice"), source_path=str(alice_wav), label="A")
    narrator = Speaker(
        id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR, voice_clip_id=narr_clip.id
    )
    alice = Speaker(
        id=new_id("spk"), name="Alice", role=SpeakerRole.CHARACTER, voice_clip_id=alice_clip.id
    )
    seg_n = Segment(
        id=new_id("seg"),
        text="The hall was silent.",
        speaker_id=narrator.id,
        role=SpeakerRole.NARRATOR,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )
    seg_a = Segment(
        id=new_id("seg"),
        text='"Hello,"',
        speaker_id=alice.id,
        role=SpeakerRole.CHARACTER,
        confidence=0.99,
        review_status=ReviewStatus.APPROVED,
    )
    line = Line(
        id=new_id("line"),
        chapter_id="c",
        order=0,
        text='The hall was silent. "Hello,"',
        segments=[seg_n, seg_a],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="hal",
                suggested="hall",
                reason="spellcheck",
                confidence=0.99,
                status=ReviewStatus.AUTO_APPLIED,  # resolved -> must not block
            )
        ],
    )
    ch = Chapter(id="c", order=0, title="One", lines=[line])
    book = Book(title="t", author="a", source_ebook_path="x", cover_image_path=None, chapters=[ch])
    return Project(
        schema_version=1,
        id=new_id("proj"),
        name="clean",
        workspace_dir=str(tmp_path),
        book=book,
        speakers=[narrator, alice],
        voice_clips=[narr_clip, alice_clip],
        stage_status={},
        tts_params={"seed": 1},
    )


def _alice_seg(project: Project) -> Segment:
    return project.book.chapters[0].lines[0].segments[1]


def test_clean_project_is_complete_no_blockers(clean_project: Project) -> None:
    blockers = review_blockers(clean_project)
    assert blockers.is_empty
    assert is_review_complete(clean_project) is True
    # AUTO_APPLIED suggestion is present yet not reported (false-positive trap #1).
    assert blockers.pending_suggestions == []


def test_only_criterion_1_blocks_needs_review(clean_project: Project) -> None:
    seg = _alice_seg(clean_project)
    seg.review_status = ReviewStatus.NEEDS_REVIEW
    blockers = review_blockers(clean_project)
    assert blockers.needs_attribution == [seg.id]
    assert blockers.pending_suggestions == []
    assert blockers.unassigned_voices == []
    assert is_review_complete(clean_project) is False


def test_only_criterion_2_blocks_pending_suggestion(clean_project: Project) -> None:
    sug = TextSuggestion(
        id=new_id("sug"),
        original="Helo",
        suggested="Hello",
        reason="spellcheck",
        confidence=0.6,
        status=ReviewStatus.PENDING,
    )
    clean_project.book.chapters[0].lines[0].suggestions.append(sug)
    blockers = review_blockers(clean_project)
    assert blockers.pending_suggestions == [sug.id]
    assert blockers.needs_attribution == []
    assert blockers.unassigned_voices == []
    assert is_review_complete(clean_project) is False


def test_only_criterion_3_blocks_unassigned_voice(clean_project: Project) -> None:
    alice = next(sp for sp in clean_project.speakers if sp.name == "Alice")
    alice.voice_clip_id = None
    blockers = review_blockers(clean_project)
    assert blockers.unassigned_voices == ["Alice"]
    assert blockers.unassigned_voices == unresolved_voices(clean_project)
    assert blockers.needs_attribution == []
    assert blockers.pending_suggestions == []
    assert is_review_complete(clean_project) is False


def test_auto_applied_suggestion_never_blocks(clean_project: Project) -> None:
    """A second AUTO_APPLIED suggestion is also ignored (only PENDING blocks)."""
    clean_project.book.chapters[0].lines[0].suggestions.append(
        TextSuggestion(
            id=new_id("sug"),
            original="silnt",
            suggested="silent",
            reason="spellcheck",
            confidence=0.98,
            status=ReviewStatus.AUTO_APPLIED,
        )
    )
    assert review_blockers(clean_project).pending_suggestions == []
    assert is_review_complete(clean_project) is True


def test_bare_rejected_attribution_does_not_block(clean_project: Project) -> None:
    """A REJECTED segment (no reassignment) still has a voiced speaker -> does NOT block.

    Only NEEDS_REVIEW blocks criterion 1 (DECISION #6: bare REJECTED passes the gate as long
    as its speaker still resolves a voice). The Alice segment keeps Alice's voice, so neither
    criterion 1 nor criterion 3 fires.
    """
    seg = _alice_seg(clean_project)
    seg.review_status = ReviewStatus.REJECTED
    blockers = review_blockers(clean_project)
    assert blockers.needs_attribution == []
    assert blockers.unassigned_voices == []
    assert is_review_complete(clean_project) is True


def test_all_statuses_resolved_means_complete(clean_project: Project) -> None:
    """APPROVED + AUTO_APPLIED + every voice assigned == ready to synthesize."""
    # Sanity: flip the segment through APPROVED again and confirm still complete.
    _alice_seg(clean_project).review_status = ReviewStatus.APPROVED
    assert is_review_complete(clean_project) is True
    assert unresolved_voices(clean_project) == []

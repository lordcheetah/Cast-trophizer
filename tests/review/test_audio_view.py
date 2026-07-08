"""Unit tests for the pure audio-review derivation :mod:`casttrophizer.review.audio_view`.

Offline/deterministic: an in-memory :class:`Project` plus a real :class:`AudioCache` over a temp
workspace (WAVs planted on disk for the rendered segments). Covers the ``audio_status`` mapping,
``is_approved``/``is_failed``/``is_rendered``/``is_playable``/``wav_path``, the whitespace-only and
missing-WAV (dirtied) non-playable cases, the narrator-fallback ``speaker_display`` matching the
attribution panel, and ``approved_count``/``rendered_count``.
"""

from __future__ import annotations

import wave
from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    find_narrator,
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.review.attribution_view import _speaker_display, segment_rows
from casttrophizer.review.audio_view import (
    AudioSegmentRow,
    approved_count,
    audio_segment_rows,
    rendered_count,
)
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 128)


def _seg(
    text: str,
    speaker: Speaker | None,
    status: ReviewStatus,
) -> Segment:
    return Segment(
        id=new_id("seg"),
        text=text,
        speaker_id=speaker.id if speaker is not None else None,
        role=speaker.role if speaker is not None else SpeakerRole.CHARACTER,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
        audio_status=status,
    )


def _project(store: WorkspaceStore) -> Project:
    """A project with one row of each audio state, WAVs planted for the current-key ones."""
    narrator = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)
    alice = Speaker(id=new_id("spk"), name="Alice", role=SpeakerRole.CHARACTER)

    completed = _seg("Narration one.", narrator, ReviewStatus.COMPLETED)
    approved = _seg('"Hi," said Alice.', alice, ReviewStatus.APPROVED)
    failed = _seg("Narration failed.", narrator, ReviewStatus.FAILED)
    pending = _seg("Narration pending.", narrator, ReviewStatus.PENDING)
    whitespace = _seg("   ", narrator, ReviewStatus.COMPLETED)  # status COMPLETED but no audio
    dirtied = _seg("Narration dirtied.", narrator, ReviewStatus.COMPLETED)  # key set, WAV missing
    fallback = _seg('"Who am I?"', None, ReviewStatus.COMPLETED)  # None speaker -> narrator (auto)

    ch_id = new_id("ch")
    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text="mixed",
        segments=[completed, approved, failed, pending, whitespace, dirtied, fallback],
    )
    chapter = Chapter(id=ch_id, order=0, title="One", lines=[line])
    book = Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter])
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Audio",
        workspace_dir=str(store.layout.root),
        book=book,
        speakers=[narrator, alice],
        tts_params={"seed": 7},
    )

    cache = AudioCache(store.layout)
    # Plant on-disk WAVs + current keys for the playable rows.
    for seg in (completed, approved, fallback):
        key = cache.key_for(seg, project)
        _write_wav(cache.path_for_key(key))
        seg.audio_cache_key = key
    # The dirtied segment carries a stale key whose WAV was never written (missing file).
    dirtied.audio_cache_key = cache.key_for(dirtied, project)
    store.save(project)
    return project


def _by_text(rows: list[AudioSegmentRow], text: str) -> AudioSegmentRow:
    return next(r for r in rows if r.text == text)


# --------------------------------------------------------------------------- #
def test_status_mapping_and_flags(tmp_workspace: WorkspaceStore) -> None:
    project = _project(tmp_workspace)
    rows = audio_segment_rows(project, AudioCache(tmp_workspace.layout))

    completed = _by_text(rows, "Narration one.")
    assert completed.audio_status == ReviewStatus.COMPLETED
    assert completed.is_rendered and completed.is_playable
    assert not completed.is_approved and not completed.is_failed
    assert completed.wav_path is not None and completed.wav_path.endswith(".wav")

    approved = _by_text(rows, '"Hi," said Alice.')
    assert approved.is_approved and approved.is_rendered and approved.is_playable

    failed = _by_text(rows, "Narration failed.")
    assert failed.is_failed and not failed.is_rendered and not failed.is_playable
    assert failed.wav_path is None


def test_whitespace_only_is_non_playable_and_not_rendered(tmp_workspace: WorkspaceStore) -> None:
    project = _project(tmp_workspace)
    rows = audio_segment_rows(project, AudioCache(tmp_workspace.layout))
    whitespace = _by_text(rows, "   ")
    # Its status is COMPLETED, but a whitespace-only segment is never rendered/playable.
    assert not whitespace.is_rendered
    assert not whitespace.is_playable
    assert whitespace.wav_path is None


def test_dirtied_segment_with_missing_wav_is_not_playable(tmp_workspace: WorkspaceStore) -> None:
    project = _project(tmp_workspace)
    rows = audio_segment_rows(project, AudioCache(tmp_workspace.layout))
    dirtied = _by_text(rows, "Narration dirtied.")
    # Key set + COMPLETED status, but the WAV is absent -> not playable (dirtied by a prior edit).
    assert dirtied.is_rendered  # status still COMPLETED
    assert not dirtied.is_playable
    assert dirtied.wav_path is None


def test_pending_segment_is_not_rendered(tmp_workspace: WorkspaceStore) -> None:
    project = _project(tmp_workspace)
    rows = audio_segment_rows(project, AudioCache(tmp_workspace.layout))
    pending = _by_text(rows, "Narration pending.")
    assert not pending.is_rendered and not pending.is_playable


def test_narrator_fallback_display_matches_attribution(tmp_workspace: WorkspaceStore) -> None:
    project = _project(tmp_workspace)
    cache = AudioCache(tmp_workspace.layout)
    rows = audio_segment_rows(project, cache)
    fallback = _by_text(rows, '"Who am I?"')
    narrator = find_narrator(project)
    by_id = {sp.id: sp for sp in project.speakers}
    fallback_seg = next(
        s
        for ch in project.book.chapters
        for ln in ch.lines
        for s in ln.segments
        if s.text == '"Who am I?"'
    )
    assert fallback.speaker_display == _speaker_display(fallback_seg, narrator, by_id)
    assert fallback.speaker_display == "narrator (auto)"
    # And a set-speaker row matches the attribution panel's display exactly.
    attr = {r.segment_id: r.speaker_display for r in segment_rows(project)}
    for row in rows:
        assert row.speaker_display == attr[row.segment_id]


def test_counts(tmp_workspace: WorkspaceStore) -> None:
    project = _project(tmp_workspace)
    rows = audio_segment_rows(project, AudioCache(tmp_workspace.layout))
    # rendered = completed + approved + dirtied(still COMPLETED) + fallback = 4
    assert rendered_count(rows) == 4
    # approved = just the Alice APPROVED row
    assert approved_count(rows) == 1

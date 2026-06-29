"""Workspace store + audio cache tests: save/load, atomic write, cache-key determinism."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from casttrophizer.domain.models import Project
from casttrophizer.errors import WorkspaceError
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore


def test_save_then_load_round_trips(tmp_workspace: WorkspaceStore, sample_project: Project) -> None:
    # sample_project is already saved by the fixture.
    loaded = tmp_workspace.load()
    assert loaded == sample_project


def test_atomic_write_leaves_no_partial_file_on_failure(
    tmp_workspace: WorkspaceStore,
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Capture the known-good on-disk bytes, then simulate a mid-write crash and confirm
    # the existing project.json is untouched (atomic replace never happened).
    good_bytes = tmp_workspace.layout.project_file.read_bytes()

    import casttrophizer.workspace.store as store_mod

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(store_mod.os, "replace", boom)

    sample_project.name = "Mutated"
    with pytest.raises(WorkspaceError):
        tmp_workspace.save(sample_project)

    # The original file is intact...
    assert tmp_workspace.layout.project_file.read_bytes() == good_bytes
    # ...and no leftover temp file remains.
    leftovers = list(tmp_workspace.layout.root.glob(".project-*.tmp"))
    assert leftovers == []


def test_load_invalid_json_raises_workspace_error(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    tmp_workspace.layout.project_file.write_text("{ not json", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        tmp_workspace.load()


def test_source_inputs_are_not_copied_or_mutated(
    tmp_workspace: WorkspaceStore,
    sample_project: Project,
    sample_epub: Path,
    fake_voice_clips: list[Path],
) -> None:
    # Source ebook + voice clips are referenced by path; saving must not touch them or
    # copy them into the workspace.
    epub_bytes = sample_epub.read_bytes()
    clip_bytes = [p.read_bytes() for p in fake_voice_clips]

    tmp_workspace.save(sample_project)

    assert sample_epub.read_bytes() == epub_bytes
    for p, original in zip(fake_voice_clips, clip_bytes, strict=True):
        assert p.read_bytes() == original

    # Nothing under the workspace should be a copy of an input.
    workspace_files = {p.name for p in tmp_workspace.layout.root.rglob("*") if p.is_file()}
    assert sample_epub.name not in workspace_files
    for clip in fake_voice_clips:
        assert clip.name not in workspace_files


def test_audio_cache_key_is_stable() -> None:
    a = AudioCache.compute_key("hello", "voice-1", {"seed": 1, "exaggeration": 0.5})
    b = AudioCache.compute_key("hello", "voice-1", {"exaggeration": 0.5, "seed": 1})
    # Same inputs (param order irrelevant) -> same key.
    assert a == b


def test_audio_cache_key_changes_with_text_voice_or_params() -> None:
    base = AudioCache.compute_key("hello", "voice-1", {"seed": 1})
    assert AudioCache.compute_key("HELLO", "voice-1", {"seed": 1}) != base
    assert AudioCache.compute_key("hello", "voice-2", {"seed": 1}) != base
    assert AudioCache.compute_key("hello", "voice-1", {"seed": 2}) != base


def test_audio_cache_key_with_none_voice_is_stable() -> None:
    # A narrator/unassigned segment (voice_clip_id=None) still hashes deterministically.
    a = AudioCache.compute_key("hello", None, {"seed": 1})
    b = AudioCache.compute_key("hello", None, {"seed": 1})
    assert a == b
    # None folds to "" but must NOT collide with a literal "" voice id colliding with text.
    assert AudioCache.compute_key("hello", None, {"seed": 1}) == AudioCache.compute_key(
        "hello", "", {"seed": 1}
    )


def test_audio_cache_key_for_segment_uses_speaker_voice(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    cache = AudioCache(tmp_workspace.layout)
    seg = sample_project.book.chapters[0].lines[0].segments[1]  # Alice's line
    speaker = next(s for s in sample_project.speakers if s.id == seg.speaker_id)
    expected = AudioCache.compute_key(seg.text, speaker.voice_clip_id, sample_project.tts_params)
    assert cache.key_for(seg, sample_project) == expected


def test_audio_cache_key_is_per_segment_not_per_line(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    # The two segments of the same line have different text and different speaker voices,
    # so their cache keys must differ — proving the cache unit is the Segment.
    cache = AudioCache(tmp_workspace.layout)
    line = sample_project.book.chapters[0].lines[0]
    seg0, seg1 = line.segments
    assert cache.key_for(seg0, sample_project) != cache.key_for(seg1, sample_project)


def test_audio_cache_key_changes_when_speaker_voice_reassigned(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    # Reassigning a speaker's voice clip must invalidate that segment's cache key, so the
    # synthesize stage regenerates the audio (text unchanged, voice changed).
    cache = AudioCache(tmp_workspace.layout)
    seg = sample_project.book.chapters[0].lines[0].segments[1]  # Alice's line
    before = cache.key_for(seg, sample_project)

    speaker = next(s for s in sample_project.speakers if s.id == seg.speaker_id)
    speaker.voice_clip_id = "a-different-voice-id"
    after = cache.key_for(seg, sample_project)

    assert before != after


def test_project_json_is_human_readable(
    tmp_workspace: WorkspaceStore, sample_project: Project
) -> None:
    data = json.loads(tmp_workspace.layout.project_file.read_text(encoding="utf-8"))
    assert data["name"] == sample_project.name
    assert data["schema_version"] == sample_project.schema_version

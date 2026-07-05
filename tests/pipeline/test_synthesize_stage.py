"""SynthesizeStage integration tests.

Run against the saved ``synthesize_ready_project`` with a real ``WorkspaceStore``; assertions
read the **persisted** project back (proving it was saved, not just mutated in memory). The
TTS is always the offline ``FakeTTSProvider`` injected via ``StageContext.tts`` — no model, no
network. Covers: completion + persistence + on-disk WAVs; full-cast voice routing (the
correct ``voice_clip_path`` per segment); idempotent cache-skip (zero TTS calls on re-run);
key-change-on-edit regeneration; the fail-fast voice precheck; per-segment flag-and-continue +
the all-failed guard; ctx.tts None / unavailable -> FAILED; whitespace-only skip; stop/resume;
attribution fields untouched; is_complete / next_stage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer.audio.loudness import DEFAULT_PEAK_CEILING_DBFS, LoudnessSettings
from casttrophizer.audio.wavfile import read_wav_mono_float
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.models import Project, Segment, Speaker
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.synthesize import SynthesizeStage
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeTTSProvider, RecordingProgressReporter


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ctx(
    store: WorkspaceStore,
    progress: RecordingProgressReporter,
    tts: FakeTTSProvider | None,
) -> StageContext:
    return StageContext(store=store, progress=progress, tts=tts)


def _all_segments(project: Project) -> list[Segment]:
    return [seg for ch in project.book.chapters for ln in ch.lines for seg in ln.segments]


def _renderable_segments(project: Project) -> list[Segment]:
    return [s for s in _all_segments(project) if s.text.strip()]


def _speaker(project: Project, sid: str | None) -> Speaker:
    return next(s for s in project.speakers if s.id == sid)


def _voice_path(project: Project, segment: Segment) -> str:
    speaker = _speaker(project, segment.speaker_id)
    clip = next(c for c in project.voice_clips if c.id == speaker.voice_clip_id)
    return clip.source_path


class StopAfterNProgress(RecordingProgressReporter):
    """Trips ``should_stop`` only after ``n`` advances (stop mid-render with partial state)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._n = n

    def should_stop(self) -> bool:
        return len(self.advances) >= self._n


# --------------------------------------------------------------------------- #
# completion + persistence + on-disk audio
# --------------------------------------------------------------------------- #
def test_run_completes_persists_and_writes_wavs(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    tts = FakeTTSProvider()
    result = SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    assert result.stage == StageName.SYNTHESIZE
    assert result.status == ReviewStatus.COMPLETED

    reloaded = WorkspaceStore(tmp_workspace.layout).load()
    assert reloaded.stage_status[str(StageName.SYNTHESIZE)] == ReviewStatus.COMPLETED

    cache = AudioCache(tmp_workspace.layout)
    for seg in _renderable_segments(reloaded):
        assert seg.audio_cache_key is not None
        assert seg.audio_status == ReviewStatus.COMPLETED
        assert cache.path_for_key(seg.audio_cache_key).is_file()


def test_progress_total_is_per_segment(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    progress = RecordingProgressReporter()
    SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, progress, FakeTTSProvider())
    )
    total_segments = len(_all_segments(synthesize_ready_project))
    assert progress.total == total_segments
    # One advance per segment (rendered, skipped, or whitespace).
    assert sum(n for n, _ in progress.advances) == total_segments


# --------------------------------------------------------------------------- #
# full-cast voice routing (the headline proof)
# --------------------------------------------------------------------------- #
def test_each_segment_rendered_with_its_speakers_voice(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    tts = FakeTTSProvider()
    SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    # Map the recorded requests by text and assert each used the speaker's clip path.
    by_text = {req.text: req for req in tts.synthesize_calls}
    for seg in _renderable_segments(synthesize_ready_project):
        assert seg.text in by_text, f"segment {seg.text!r} was not rendered"
        assert str(by_text[seg.text].voice_clip_path) == _voice_path(synthesize_ready_project, seg)
    # Narration -> narrator clip, Alice quote -> Alice clip (distinct paths).
    narration = next(
        s for s in _renderable_segments(synthesize_ready_project) if s.role == SpeakerRole.NARRATOR
    )
    quote = next(
        s for s in _renderable_segments(synthesize_ready_project) if s.role == SpeakerRole.CHARACTER
    )
    assert str(by_text[narration.text].voice_clip_path) != str(by_text[quote.text].voice_clip_path)


def test_tts_params_passed_match_cache_key_input(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    tts = FakeTTSProvider()
    SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    for req in tts.synthesize_calls:
        assert req.params == synthesize_ready_project.tts_params


# --------------------------------------------------------------------------- #
# idempotency: cache hit -> zero TTS calls on re-run
# --------------------------------------------------------------------------- #
def test_rerun_is_all_cache_hits_zero_tts_calls(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    first = tmp_workspace.load()
    keys_after_first = {s.id: s.audio_cache_key for s in _renderable_segments(first)}

    # Fresh fake on the persisted project: every segment is current -> no synthesize call.
    fresh = FakeTTSProvider()
    result = SynthesizeStage().run(first, _ctx(tmp_workspace, RecordingProgressReporter(), fresh))
    assert result.status == ReviewStatus.COMPLETED
    assert fresh.synthesize_calls == []

    second = tmp_workspace.load()
    assert {s.id: s.audio_cache_key for s in _renderable_segments(second)} == keys_after_first


# --------------------------------------------------------------------------- #
# change detection: edit -> new key -> only the changed segment re-renders
# --------------------------------------------------------------------------- #
def test_text_edit_regenerates_only_affected_segment(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    persisted = tmp_workspace.load()
    target = _renderable_segments(persisted)[0]
    old_key = target.audio_cache_key
    target.text = target.text + " (edited)"
    target.audio_status = ReviewStatus.PENDING  # mirror the UI 'regenerate' reset is optional;
    # even without this reset, the new text changes the key so the skip gate fails.
    tmp_workspace.save(persisted)

    fresh = FakeTTSProvider()
    SynthesizeStage().run(persisted, _ctx(tmp_workspace, RecordingProgressReporter(), fresh))

    # Exactly one render (the edited segment); everyone else is a cache hit.
    assert len(fresh.synthesize_calls) == 1
    assert fresh.synthesize_calls[0].text == target.text
    reloaded = tmp_workspace.load()
    new_seg = next(s for s in _renderable_segments(reloaded) if s.id == target.id)
    assert new_seg.audio_cache_key is not None
    assert new_seg.audio_cache_key != old_key
    assert AudioCache(tmp_workspace.layout).path_for_key(new_seg.audio_cache_key).is_file()


def test_tts_params_change_regenerates_everything(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    persisted = tmp_workspace.load()
    persisted.tts_params = {"exaggeration": 0.9, "seed": 99}  # global param change
    tmp_workspace.save(persisted)

    fresh = FakeTTSProvider()
    SynthesizeStage().run(persisted, _ctx(tmp_workspace, RecordingProgressReporter(), fresh))
    # Every renderable segment's key changed -> all re-render.
    assert len(fresh.synthesize_calls) == len(_renderable_segments(persisted))


def test_voice_reassignment_regenerates_only_that_speakers_segments(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    """Reassigning the narrator's ``voice_clip_id`` re-renders only narrator segments.

    The cache key folds in the resolved voice-clip id, so changing a *speaker's* assigned
    clip (the §3b "voice edit" path, distinct from text/params edits) must invalidate exactly
    that speaker's segments and leave the other speaker's cache hits untouched. Reassigns the
    narrator onto Alice's existing clip (precheck still passes — the clip exists), then asserts
    only the narrator's renderable segments re-synthesize and they now use Alice's clip path.
    """
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    persisted = tmp_workspace.load()
    narrator = next(s for s in persisted.speakers if s.role == SpeakerRole.NARRATOR)
    alice = next(s for s in persisted.speakers if s.role == SpeakerRole.CHARACTER)
    alice_clip_path = next(
        c.source_path for c in persisted.voice_clips if c.id == alice.voice_clip_id
    )

    # Which renderable segments belong to the narrator (whose voice we will change)?
    narrator_segs = [s for s in _renderable_segments(persisted) if s.speaker_id == narrator.id]
    alice_keys_before = {
        s.id: s.audio_cache_key for s in _renderable_segments(persisted) if s.speaker_id == alice.id
    }

    narrator.voice_clip_id = alice.voice_clip_id  # reassign narrator onto Alice's clip
    tmp_workspace.save(persisted)

    fresh = FakeTTSProvider()
    SynthesizeStage().run(persisted, _ctx(tmp_workspace, RecordingProgressReporter(), fresh))

    # Exactly the narrator's renderable segments re-render; Alice's are cache hits.
    assert len(fresh.synthesize_calls) == len(narrator_segs)
    assert {r.text for r in fresh.synthesize_calls} == {s.text for s in narrator_segs}
    # And the narrator's segments were rendered with Alice's clip (the reassigned voice).
    assert all(str(r.voice_clip_path) == alice_clip_path for r in fresh.synthesize_calls)

    reloaded = tmp_workspace.load()
    # Alice's keys are unchanged (she was a cache hit, no re-render).
    alice_keys_after = {
        s.id: s.audio_cache_key for s in _renderable_segments(reloaded) if s.speaker_id == alice.id
    }
    assert alice_keys_after == alice_keys_before


def test_deleted_wav_forces_rerender_despite_unchanged_key(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    """Deleting a cached WAV on disk re-renders that segment even though its key is unchanged.

    This is the §3a condition-2 (``cache.has(key)``) safeguard: the stored ``audio_cache_key``
    and the rendered status are unchanged, so the key-match and status checks both pass — only
    the file-existence check fails. The segment must re-render (a crash between save and
    file-write, or a user/GC deleting a clip, must not leave a phantom skip). Every other
    segment, whose WAV still exists, stays a cache hit (zero extra calls).
    """
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    persisted = tmp_workspace.load()
    cache = AudioCache(tmp_workspace.layout)

    target = _renderable_segments(persisted)[0]
    key_before = target.audio_cache_key
    assert key_before is not None
    wav_path = cache.path_for_key(key_before)
    assert wav_path.is_file()
    wav_path.unlink()  # delete the WAV; the stored key + COMPLETED status are untouched
    assert not wav_path.is_file()

    # The key on the segment is still the same — only the file is gone.
    assert target.audio_cache_key == key_before
    assert target.audio_status == ReviewStatus.COMPLETED

    fresh = FakeTTSProvider()
    result = SynthesizeStage().run(
        persisted, _ctx(tmp_workspace, RecordingProgressReporter(), fresh)
    )
    assert result.status == ReviewStatus.COMPLETED
    # Only the segment whose file was deleted re-renders; the rest are file-present cache hits.
    assert [r.text for r in fresh.synthesize_calls] == [target.text]
    assert wav_path.is_file()  # the WAV was regenerated under the same key
    reloaded = tmp_workspace.load()
    again = next(s for s in _renderable_segments(reloaded) if s.id == target.id)
    assert again.audio_cache_key == key_before  # same key, freshly re-rendered file


def test_identical_text_voice_params_segments_share_one_key(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    """Two segments with identical text+voice+params hash to one key and one WAV file.

    The fixture has two narrator ``"said Alice."`` segments (one per chapter). They share a
    cache key, so they map to a single ``<key>.wav``; on a re-run both are cache hits. This
    documents the content-addressed dedup from §7 (correct, not a defect) and guards against a
    regression where the key accidentally folds in segment id / position.
    """
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    persisted = tmp_workspace.load()
    dupes = [s for s in _renderable_segments(persisted) if s.text == "said Alice."]
    assert len(dupes) == 2, "fixture should have two identical narrator segments"
    # Identical text/voice/params -> identical key -> one shared file.
    assert dupes[0].audio_cache_key == dupes[1].audio_cache_key
    cache = AudioCache(tmp_workspace.layout)
    assert cache.path_for_key(dupes[0].audio_cache_key).is_file()

    # Re-run: both are cache hits, zero TTS calls.
    fresh = FakeTTSProvider()
    SynthesizeStage().run(persisted, _ctx(tmp_workspace, RecordingProgressReporter(), fresh))
    assert fresh.synthesize_calls == []


# --------------------------------------------------------------------------- #
# fail-fast voice precheck (policy A)
# --------------------------------------------------------------------------- #
def test_unassigned_voice_fails_naming_speaker_zero_tts_calls(
    tmp_workspace: WorkspaceStore, synthesize_unassigned_voice_project: Project
) -> None:
    tts = FakeTTSProvider()
    result = SynthesizeStage().run(
        synthesize_unassigned_voice_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    assert result.status == ReviewStatus.FAILED
    assert "Alice" in result.message
    assert tts.synthesize_calls == []  # nothing rendered before the precheck failed
    assert str(StageName.SYNTHESIZE) not in synthesize_unassigned_voice_project.stage_status
    # No WAV escaped to the audio cache before the precheck bailed.
    assert list(tmp_workspace.layout.audio_dir.glob("*.wav")) == []


def test_missing_clip_file_fails_precheck(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    # Delete the narrator clip file on disk: the precheck must catch the missing source_path.
    fake_voice_clips[0].unlink()
    tts = FakeTTSProvider()
    result = SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    assert result.status == ReviewStatus.FAILED
    assert "narrator" in result.message
    assert tts.synthesize_calls == []
    assert str(StageName.SYNTHESIZE) not in synthesize_ready_project.stage_status
    assert list(tmp_workspace.layout.audio_dir.glob("*.wav")) == []


# --------------------------------------------------------------------------- #
# per-segment failure: flag-and-continue + all-failed guard
# --------------------------------------------------------------------------- #
def test_per_segment_failure_flags_and_continues(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    target = _renderable_segments(synthesize_ready_project)[0]
    tts = FakeTTSProvider(fail_for={target.text})
    result = SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    assert result.status == ReviewStatus.COMPLETED  # one bad segment doesn't kill the run

    reloaded = tmp_workspace.load()
    failed = [s for s in _renderable_segments(reloaded) if s.audio_status == ReviewStatus.FAILED]
    assert [s.id for s in failed] == [target.id]
    # The failed segment got no current cache key/file; the rest are COMPLETED.
    others = [s for s in _renderable_segments(reloaded) if s.id != target.id]
    assert all(s.audio_status == ReviewStatus.COMPLETED for s in others)

    # Re-run with a working fake retries ONLY the failed segment (others are cache hits).
    fresh = FakeTTSProvider()
    result2 = SynthesizeStage().run(
        reloaded, _ctx(tmp_workspace, RecordingProgressReporter(), fresh)
    )
    assert result2.status == ReviewStatus.COMPLETED
    assert [req.text for req in fresh.synthesize_calls] == [target.text]
    done = tmp_workspace.load()
    assert all(s.audio_status == ReviewStatus.COMPLETED for s in _renderable_segments(done))


def test_all_segments_failed_returns_failed(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    tts = FakeTTSProvider(fail_all=True)
    result = SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    assert result.status == ReviewStatus.FAILED
    assert "all segments failed" in result.message
    reloaded = tmp_workspace.load()
    assert str(StageName.SYNTHESIZE) not in reloaded.stage_status
    assert all(s.audio_status == ReviewStatus.FAILED for s in _renderable_segments(reloaded))
    assert all(s.audio_cache_key is None for s in _renderable_segments(reloaded))
    # A raising synth writes no file, so the audio cache stays empty.
    assert list(tmp_workspace.layout.audio_dir.glob("*.wav")) == []


# --------------------------------------------------------------------------- #
# provider precondition failures
# --------------------------------------------------------------------------- #
def test_tts_none_is_failed_status_unset(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    result = SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), None)
    )
    assert result.status == ReviewStatus.FAILED
    assert str(StageName.SYNTHESIZE) not in synthesize_ready_project.stage_status


def test_tts_unavailable_is_failed(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    result = SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider(available=False)),
    )
    assert result.status == ReviewStatus.FAILED
    assert str(StageName.SYNTHESIZE) not in synthesize_ready_project.stage_status


# --------------------------------------------------------------------------- #
# whitespace-only segment skipped, no TTS call, no file
# --------------------------------------------------------------------------- #
def test_whitespace_segment_skipped(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    tts = FakeTTSProvider()
    SynthesizeStage().run(
        synthesize_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), tts)
    )
    # The whitespace-only segment text was never sent to the provider.
    assert all(req.text.strip() for req in tts.synthesize_calls)
    reloaded = tmp_workspace.load()
    ws = next(s for s in _all_segments(reloaded) if not s.text.strip())
    assert ws.audio_cache_key is None
    assert ws.audio_status == ReviewStatus.PENDING


# --------------------------------------------------------------------------- #
# stop / resume
# --------------------------------------------------------------------------- #
def test_stop_persists_partial_then_resumes_without_rerender(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    # Stop after the first chapter's segments advance -> chapter two stays unrendered.
    stage = SynthesizeStage()
    result = stage.run(
        synthesize_ready_project, _ctx(tmp_workspace, StopAfterNProgress(n=1), FakeTTSProvider())
    )
    assert result.status == ReviewStatus.STOPPED

    after_stop = tmp_workspace.load()
    assert after_stop.stage_status.get(str(StageName.SYNTHESIZE)) != ReviewStatus.COMPLETED
    assert stage.is_complete(after_stop) is False
    rendered_at_stop = [
        s for s in _renderable_segments(after_stop) if s.audio_status == ReviewStatus.COMPLETED
    ]
    not_rendered = [
        s for s in _renderable_segments(after_stop) if s.audio_status != ReviewStatus.COMPLETED
    ]
    assert rendered_at_stop  # some progress persisted
    assert not_rendered  # not everything done

    # Resume with a fresh fake -> completes, re-rendering only the not-yet-done segments.
    fresh = FakeTTSProvider()
    result2 = stage.run(after_stop, _ctx(tmp_workspace, RecordingProgressReporter(), fresh))
    assert result2.status == ReviewStatus.COMPLETED
    rendered_texts = {req.text for req in fresh.synthesize_calls}
    assert rendered_texts == {s.text for s in not_rendered}  # no re-render of cached segments
    done = tmp_workspace.load()
    assert all(s.audio_status == ReviewStatus.COMPLETED for s in _renderable_segments(done))


# --------------------------------------------------------------------------- #
# attribution fields untouched; is_complete / next_stage
# --------------------------------------------------------------------------- #
def test_attribution_fields_untouched(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    before = {
        s.id: (s.text, s.speaker_id, s.confidence, s.review_status)
        for s in _all_segments(synthesize_ready_project)
    }
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    reloaded = tmp_workspace.load()
    after = {
        s.id: (s.text, s.speaker_id, s.confidence, s.review_status) for s in _all_segments(reloaded)
    }
    assert after == before  # the stage only writes audio_cache_key/audio_status


def test_is_complete_after_run(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    stage = SynthesizeStage()
    assert stage.is_complete(synthesize_ready_project) is False
    stage.run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    assert stage.is_complete(synthesize_ready_project) is True


def test_pipeline_next_stage_after_synthesize(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    pipeline = Pipeline([SynthesizeStage()])
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()))
    reloaded = tmp_workspace.load()
    assert pipeline.next_stage(reloaded) is None  # the only stage is complete


# --------------------------------------------------------------------------- #
# loudness normalization (per-segment, run in the orchestration)
# --------------------------------------------------------------------------- #
def _enable_loudness(project: Project, **overrides: object) -> None:
    """Seed a loudness block into the project's tts_params (as the CLI would at creation)."""
    project.tts_params["loudness"] = LoudnessSettings(**overrides).to_params()  # type: ignore[arg-type]


def test_loudness_enabled_run_completes_on_silence(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    """Loudness enabled: the silent FakeTTS clips normalize (short-circuit) without crashing."""
    _enable_loudness(synthesize_ready_project)
    tmp_workspace.save(synthesize_ready_project)

    result = SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    assert result.status == ReviewStatus.COMPLETED

    reloaded = tmp_workspace.load()
    cache = AudioCache(tmp_workspace.layout)
    for seg in _renderable_segments(reloaded):
        assert seg.audio_status == ReviewStatus.COMPLETED
        assert seg.audio_cache_key is not None
        assert cache.path_for_key(seg.audio_cache_key).is_file()  # silence untouched, still present


def test_loudness_attenuates_loud_rendered_audio(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    """A LOUD fake render (constant 0.9) is attenuated toward target below the peak ceiling."""
    _enable_loudness(synthesize_ready_project)
    tmp_workspace.save(synthesize_ready_project)

    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider(loud=True)),
    )

    reloaded = tmp_workspace.load()
    cache = AudioCache(tmp_workspace.layout)
    ceiling = 10.0 ** (DEFAULT_PEAK_CEILING_DBFS / 20.0)
    for seg in _renderable_segments(reloaded):
        assert seg.audio_cache_key is not None
        samples, _ = read_wav_mono_float(cache.path_for_key(seg.audio_cache_key))
        peak = max(abs(s) for s in samples)
        assert peak < 0.9  # attenuated from the 0.9 the loud fake wrote
        assert peak <= ceiling + 1e-3  # under the -1 dBFS ceiling (within a 16-bit quantum)


def test_loudness_setting_changes_cache_key(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    """Adding/changing/toggling the loudness block changes AudioCache.key_for for a segment."""
    project = synthesize_ready_project
    seg = _renderable_segments(project)[0]
    key_no_block = AudioCache.key_for(seg, project)

    _enable_loudness(project)  # enabled, -18 LUFS
    key_enabled = AudioCache.key_for(seg, project)
    assert key_enabled != key_no_block  # turning normalization on re-keys the segment

    _enable_loudness(project, target_lufs=-14.0)  # change the target
    key_new_target = AudioCache.key_for(seg, project)
    assert key_new_target != key_enabled

    _enable_loudness(project, enabled=False)  # toggle off
    key_disabled = AudioCache.key_for(seg, project)
    assert key_disabled != key_enabled
    assert key_disabled != key_new_target


def test_loudness_target_change_regenerates_everything(
    tmp_workspace: WorkspaceStore, synthesize_ready_project: Project
) -> None:
    """Changing tts_params['loudness']['target_lufs'] re-renders every renderable segment."""
    _enable_loudness(synthesize_ready_project)
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    persisted = tmp_workspace.load()
    _enable_loudness(persisted, target_lufs=-12.0)  # change the loudness target
    tmp_workspace.save(persisted)

    fresh = FakeTTSProvider()
    SynthesizeStage().run(persisted, _ctx(tmp_workspace, RecordingProgressReporter(), fresh))
    assert len(fresh.synthesize_calls) == len(_renderable_segments(persisted))


def test_cached_segment_is_not_re_normalized(
    tmp_workspace: WorkspaceStore,
    synthesize_ready_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SKIPPED (cache-hit) segment is not re-normalized: normalize runs once per render only."""
    _enable_loudness(synthesize_ready_project)
    tmp_workspace.save(synthesize_ready_project)

    calls: list[Path] = []
    import casttrophizer.audio.synthesize as synth_mod

    def _counting_normalize(path: Path, settings: LoudnessSettings) -> None:
        calls.append(path)

    monkeypatch.setattr(synth_mod, "normalize_wav_file", _counting_normalize)

    # First run: every renderable segment renders -> one normalize call each.
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    after_first = len(calls)
    assert after_first == len(_renderable_segments(synthesize_ready_project))

    # Second run: all cache hits -> zero further normalize calls (cached audio is not touched).
    persisted = tmp_workspace.load()
    SynthesizeStage().run(
        persisted, _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider())
    )
    assert len(calls) == after_first  # no re-normalization of already-rendered segments


def test_no_loudness_block_skips_normalization(
    tmp_workspace: WorkspaceStore,
    synthesize_ready_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project without a loudness block (older project) never calls normalize_wav_file."""
    assert "loudness" not in synthesize_ready_project.tts_params  # fixture has no block

    calls: list[Path] = []
    import casttrophizer.audio.synthesize as synth_mod

    monkeypatch.setattr(synth_mod, "normalize_wav_file", lambda path, settings: calls.append(path))
    SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    assert calls == []  # from_params returned None -> normalization skipped entirely


def test_normalize_failure_does_not_fail_the_render(
    tmp_workspace: WorkspaceStore,
    synthesize_ready_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising ``normalize_wav_file`` is logged-and-swallowed: the render still completes.

    Normalization is post-processing on an already-rendered WAV; a failure there must never
    fail an otherwise-good render. Every segment ends COMPLETED with its (un-normalized) WAV
    still present on disk.
    """
    _enable_loudness(synthesize_ready_project)
    tmp_workspace.save(synthesize_ready_project)

    import casttrophizer.audio.synthesize as synth_mod

    def _boom(path: Path, settings: LoudnessSettings) -> None:
        raise RuntimeError("normalization exploded")

    monkeypatch.setattr(synth_mod, "normalize_wav_file", _boom)

    result = SynthesizeStage().run(
        synthesize_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeTTSProvider()),
    )
    assert result.status == ReviewStatus.COMPLETED  # normalize failure did not fail the render

    reloaded = tmp_workspace.load()
    cache = AudioCache(tmp_workspace.layout)
    for seg in _renderable_segments(reloaded):
        assert seg.audio_status == ReviewStatus.COMPLETED
        assert seg.audio_cache_key is not None
        assert cache.path_for_key(seg.audio_cache_key).is_file()  # raw WAV kept

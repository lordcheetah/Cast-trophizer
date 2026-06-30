"""Voice <-> synthesize agreement and voice-clip lifecycle (offline, FakeTTS only).

Priorities 2 + 3: assigning voices through the review *actions* must (a) make
``unresolved_voices`` empty AND let the synthesize precheck pass under ``FakeTTSProvider``
(the whole point of putting voice assignment at the review gate), and (b) a registered
clip whose file is later deleted must re-open criterion 3.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus
from casttrophizer.domain.models import Project
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.synthesize import SynthesizeStage
from casttrophizer.review import actions
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeTTSProvider, RecordingProgressReporter


def _ctx(store: WorkspaceStore, **kwargs) -> StageContext:
    return StageContext(store=store, progress=RecordingProgressReporter(), **kwargs)


def test_assign_every_speaker_then_synthesize_precheck_passes(
    tmp_workspace: WorkspaceStore,
    review_ready_project: Project,
    fake_voice_clips: list[Path],
) -> None:
    """Assign Bob (the only voice-less referenced speaker) -> precheck clears, render proceeds."""
    project = review_ready_project
    assert unresolved_voices(project) == ["Bob"]

    bob = next(sp for sp in project.speakers if sp.name == "Bob")
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Bob")
    actions.assign_voice(bob, clip)

    # Criterion 3 cleared and agrees bit-for-bit with the synthesize precheck.
    assert unresolved_voices(project) == []

    # Resolve the remaining attribution + suggestion blockers so synthesize can run cleanly,
    # then prove the synthesize stage does NOT FAIL on voices and actually renders.
    seg = next(
        s
        for ch in project.book.chapters
        for ln in ch.lines
        for s in ln.segments
        if s.review_status == ReviewStatus.NEEDS_REVIEW
    )
    actions.approve_attribution(seg)
    tmp_workspace.save(project)

    tts = FakeTTSProvider()
    result = SynthesizeStage().run(project, _ctx(tmp_workspace, tts=tts))
    assert result.status == ReviewStatus.COMPLETED  # did not FAIL on voices
    assert tts.synthesize_calls  # actually rendered segments


def test_assign_voice_by_ids_satisfies_precheck(
    review_ready_project: Project, fake_voice_clips: list[Path]
) -> None:
    project = review_ready_project
    bob = next(sp for sp in project.speakers if sp.name == "Bob")
    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Bob")
    actions.assign_voice_by_ids(project, speaker_id=bob.id, voice_clip_id=clip.id)
    assert "Bob" not in unresolved_voices(project)


def test_register_voice_clip_copies_nothing_into_workspace(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    tmp_workspace: WorkspaceStore,
) -> None:
    """Read-only input: the source clip stays put; the workspace audio dir is unchanged."""
    project = review_ready_project
    audio_dir = tmp_workspace.layout.audio_dir
    before = sorted(p.name for p in audio_dir.glob("*")) if audio_dir.exists() else []

    clip = actions.register_voice_clip(project, fake_voice_clips[0], "Bob")

    # The recorded path points at the original source, not a workspace copy.
    assert Path(clip.source_path) == fake_voice_clips[0]
    assert fake_voice_clips[0].is_file()  # source untouched
    after = sorted(p.name for p in audio_dir.glob("*")) if audio_dir.exists() else []
    assert before == after  # nothing copied in


def test_deleted_clip_file_reopens_criterion_3(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    tmp_path: Path,
) -> None:
    """A VoiceClip whose file is later deleted re-opens criterion 3 (no crash)."""
    project = review_ready_project
    bob = next(sp for sp in project.speakers if sp.name == "Bob")

    # Use a dedicated, deletable copy so we don't disturb other fixtures' clips.
    src = tmp_path / "bob_voice.wav"
    src.write_bytes(fake_voice_clips[0].read_bytes())
    clip = actions.register_voice_clip(project, src, "Bob")
    actions.assign_voice(bob, clip)
    assert "Bob" not in unresolved_voices(project)

    # File vanishes after registration -> speaker resolves to no usable clip again.
    src.unlink()
    assert "Bob" in unresolved_voices(project)

"""End-to-end CLI flow: ``new -> run (halt) -> assign-voice -> run --auto-accept``.

Drives the whole terminal workflow offline against injected fakes and asserts the
stage-status progression, the review-gate halt + blocker reporting, voice assignment by
index and by name, and a produced M4B on completion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer import cli
from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.workspace.store import WorkspaceStore
from tests.cli.conftest import build_deps
from tests.fakes import FakeM4BAssembler


def _run(wd: Path, *argv: str, deps: cli.CliDeps) -> int:
    return cli.main(["--workdir", str(wd), *argv], deps=deps)


def test_new_creates_persisted_project(tmp_path: Path, sample_epub: Path) -> None:
    wd = tmp_path / "ws"
    deps = build_deps()

    rc = _run(wd, "new", "--epub", str(sample_epub), deps=deps)

    assert rc == 0
    store = WorkspaceStore.for_dir(wd)
    assert store.exists()
    project = store.load()
    assert project.book.source_ebook_path == str(sample_epub.resolve())
    assert project.book.chapters == []  # parse hasn't run yet
    assert project.stage_status == {}


def test_full_flow_new_run_assign_run(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    wd = tmp_path / "ws"
    assembler = FakeM4BAssembler()
    deps = build_deps(assembler=assembler)
    clip = str(fake_voice_clips[0])
    store = WorkspaceStore.for_dir(wd)

    # new ------------------------------------------------------------------ #
    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    capsys.readouterr()

    # first run halts at the review gate (voices unassigned) ---------------- #
    rc = _run(wd, "run", deps=deps)
    assert rc == 3
    out = capsys.readouterr().out
    assert "halted for review" in out
    # the halt blocker report names every discovered speaker still needing a voice
    assert "voices needed for" in out
    assert "narrator" in out and "Alice" in out and "Bob" in out
    # and prints an actionable per-speaker assign hint with the speaker's list index
    assert "castrun assign-voice" in out

    project = store.load()
    assert project.stage_status[str(StageName.ATTRIBUTE)] == ReviewStatus.COMPLETED
    assert str(StageName.REVIEW) not in project.stage_status

    # speakers: narrator + Alice + Bob discovered, all unvoiced ------------- #
    assert _run(wd, "speakers", deps=deps) == 0
    out = capsys.readouterr().out
    assert "narrator" in out and "Alice" in out and "Bob" in out
    assert out.count("NO VOICE") == 3

    # assign voices by index (narrator) and by name (Alice, Bob) ----------- #
    assert _run(wd, "assign-voice", "0", clip, deps=deps) == 0
    assert _run(wd, "assign-voice", "Alice", clip, deps=deps) == 0
    assert _run(wd, "assign-voice", "Bob", clip, deps=deps) == 0
    capsys.readouterr()

    assert _run(wd, "speakers", deps=deps) == 0
    assert "NO VOICE" not in capsys.readouterr().out

    # run --auto-accept: clears attribution + suggestions, synthesizes, assembles
    rc = _run(wd, "run", "--auto-accept", deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    assert "output:" in out

    project = store.load()
    for stage in StageName:
        assert project.stage_status[str(stage)] == ReviewStatus.COMPLETED
    # no NEEDS_REVIEW segments and no PENDING suggestions survive auto-accept
    for chapter in project.book.chapters:
        for line in chapter.lines:
            assert all(s.review_status != ReviewStatus.NEEDS_REVIEW for s in line.segments)
            assert all(s.status != ReviewStatus.PENDING for s in line.suggestions)

    # the fake assembler captured the ordered per-segment WAVs + wrote an M4B
    assert len(assembler.requests) == 1
    request = assembler.requests[0]
    assert request.segment_audio_paths
    assert all(p.suffix == ".wav" for p in request.segment_audio_paths)
    assert request.out_path.exists()
    assert request.out_path.suffix == ".m4b"


def test_run_until_presynthesize_stage_reports_stopped_not_complete(
    tmp_path: Path,
    sample_epub: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run --until attribute` must NOT claim the audiobook is complete.

    The runner returns a COMPLETED result for the stage it stopped at, but that is an early
    ``--until`` stop, not a full-pipeline completion — so the CLI reports "stopped after
    attribute" with a non-zero (cooperative-stop) exit, never "complete"/exit 0.
    """
    wd = tmp_path / "ws"
    deps = build_deps()

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    capsys.readouterr()

    rc = _run(wd, "run", "--until", StageName.ATTRIBUTE.value, deps=deps)

    assert rc == 4  # _EXIT_STOPPED: stopped early, not done
    out = capsys.readouterr().out
    assert "stopped after attribute" in out
    assert "complete" not in out

    # attribute did finish; the project is genuinely mid-pipeline (halted before review)
    project = WorkspaceStore.for_dir(wd).load()
    assert project.stage_status[str(StageName.ATTRIBUTE)] == ReviewStatus.COMPLETED
    assert str(StageName.REVIEW) not in project.stage_status


def test_run_on_complete_project_is_noop(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    wd = tmp_path / "ws"
    assembler = FakeM4BAssembler()
    deps = build_deps(assembler=assembler)
    clip = str(fake_voice_clips[0])

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    assert _run(wd, "run", deps=deps) == 3
    for token in ("0", "Alice", "Bob"):
        assert _run(wd, "assign-voice", token, clip, deps=deps) == 0
    assert _run(wd, "run", "--auto-accept", deps=deps) == 0
    capsys.readouterr()

    # a re-run touches nothing: no new assembly request, reports completion, exit 0
    rc = _run(wd, "run", deps=deps)
    assert rc == 0
    assert "complete" in capsys.readouterr().out
    assert len(assembler.requests) == 1


def test_assign_voice_persists_and_clears_unresolved(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid `assign-voice` persists the clip and removes that speaker from the blockers.

    Covers both resolution modes: narrator by list index, Alice/Bob by case-insensitive name.
    """
    wd = tmp_path / "ws"
    deps = build_deps()
    clip = str(fake_voice_clips[0])
    store = WorkspaceStore.for_dir(wd)

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    assert _run(wd, "run", deps=deps) == 3
    capsys.readouterr()

    # every referenced speaker is unresolved before any voice is assigned
    assert set(unresolved_voices(store.load())) == {"narrator", "Alice", "Bob"}

    # by index (narrator) and by case-insensitive name (alice, BOB)
    assert _run(wd, "assign-voice", "0", clip, deps=deps) == 0
    assert _run(wd, "assign-voice", "alice", clip, deps=deps) == 0
    assert _run(wd, "assign-voice", "BOB", clip, deps=deps) == 0
    out = capsys.readouterr().out
    assert out.count("assigned") == 3

    project = store.load()
    assert unresolved_voices(project) == []
    # each speaker now points at a registered clip whose source file exists
    for speaker in project.speakers:
        assert speaker.voice_clip_id is not None
        vc = next(v for v in project.voice_clips if v.id == speaker.voice_clip_id)
        assert Path(vc.source_path).is_file()


def test_status_reflects_stage_progression(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    wd = tmp_path / "ws"
    deps = build_deps()
    clip = str(fake_voice_clips[0])

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    capsys.readouterr()

    # after `new`: everything unset, next stage is parse
    assert _run(wd, "status", deps=deps) == 0
    out = capsys.readouterr().out
    assert "next: parse" in out

    # after the first run: parse/correct/attribute complete, halted before review
    assert _run(wd, "run", deps=deps) == 3
    capsys.readouterr()
    assert _run(wd, "status", deps=deps) == 0
    out = capsys.readouterr().out
    assert "next: review" in out
    assert "voices needed for" in out

    # after assigning voices + auto-accept run: all complete
    for token in ("0", "Alice", "Bob"):
        assert _run(wd, "assign-voice", token, clip, deps=deps) == 0
    assert _run(wd, "run", "--auto-accept", deps=deps) == 0
    capsys.readouterr()
    assert _run(wd, "status", deps=deps) == 0
    out = capsys.readouterr().out
    assert "next: complete" in out

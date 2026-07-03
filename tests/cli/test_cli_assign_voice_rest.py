"""``castrun assign-voice --rest`` — bulk-voice the remaining cast by category.

Offline/deterministic via :class:`~casttrophizer.cli.CliDeps` fakes. Covers per-category
resolution (flags win over env defaults; ``unknown``/uncovered -> ``--default``), the
no-partial-assignment coverage error, the already-voiced no-op, the no-clips error, the
``castrun speakers`` category display, the single-assign path still working, and the full
run to an M4B once ``--rest`` clears review gate criterion 3.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from casttrophizer import cli
from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName, VoiceCategory
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Chapter, Line, Project, Segment, Speaker
from casttrophizer.workspace.store import WorkspaceStore
from tests.cli.conftest import build_deps
from tests.fakes import FakeM4BAssembler


def _wav(path: Path) -> str:
    """Write a tiny silent WAV and return its absolute path string (a read-only clip)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 256)
    return str(path)


def _run(wd: Path, *argv: str, deps: cli.CliDeps) -> int:
    return cli.main(["--workdir", str(wd), *argv], deps=deps)


def _speaker(name: str, category: VoiceCategory, *, voiced: bool) -> Speaker:
    return Speaker(
        id=new_id("spk"),
        name=name,
        role=SpeakerRole.NARRATOR if name == "narrator" else SpeakerRole.CHARACTER,
        voice_clip_id=("clip-" + name) if voiced else None,
        category=category,
    )


def _mixed_project(tmp_workspace: WorkspaceStore, sample_epub: Path, voiced_clip: str) -> Path:
    """A saved project: a voiced man + unvoiced man/woman/unknown, all referenced.

    Returns the workdir. The one pre-voiced speaker proves ``--rest`` leaves it untouched.
    """
    voiced = _speaker("Aldous", VoiceCategory.MAN, voiced=True)
    man = _speaker("Bob", VoiceCategory.MAN, voiced=False)
    woman = _speaker("Sally", VoiceCategory.WOMAN, voiced=False)
    unknown = _speaker("Echo", VoiceCategory.UNKNOWN, voiced=False)

    from casttrophizer.domain.models import VoiceClip

    voiced_vc = VoiceClip(id="clip-Aldous", source_path=voiced_clip, label="Aldous")

    ch_id = new_id("ch")

    def _line(order: int, text: str, sp: Speaker) -> Line:
        return Line(
            id=new_id("line"),
            chapter_id=ch_id,
            order=order,
            text=text,
            segments=[
                Segment(
                    id=new_id("seg"),
                    text=text,
                    speaker_id=sp.id,
                    role=sp.role,
                    confidence=0.95,
                    review_status=ReviewStatus.APPROVED,
                )
            ],
        )

    chapter = Chapter(
        id=ch_id,
        order=0,
        title="One",
        lines=[
            _line(0, '"one," said Aldous.', voiced),
            _line(1, '"two," said Bob.', man),
            _line(2, '"three," said Sally.', woman),
            _line(3, '"four," said Echo.', unknown),
        ],
    )
    project = Project(
        schema_version=2,
        id=new_id("proj"),
        name="Mixed",
        workspace_dir=str(tmp_workspace.layout.root),
        book=Book(title="t", author="a", source_ebook_path=str(sample_epub), chapters=[chapter]),
        speakers=[voiced, man, woman, unknown],
        voice_clips=[voiced_vc],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
        },
    )
    tmp_workspace.save(project)
    return Path(tmp_workspace.layout.root)


def _clip_path_for(project: Project, name: str) -> str | None:
    speaker = next(s for s in project.speakers if s.name == name)
    if speaker.voice_clip_id is None:
        return None
    clip = next(vc for vc in project.voice_clips if vc.id == speaker.voice_clip_id)
    return clip.source_path


# --------------------------------------------------------------------------- #
# happy path: flags resolve per category, unknown -> --default, pre-voiced untouched
# --------------------------------------------------------------------------- #
def test_rest_assigns_per_category_flags_win(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    man_clip = _wav(tmp_path / "clips" / "man.wav")
    woman_clip = _wav(tmp_path / "clips" / "woman.wav")
    default_clip = _wav(tmp_path / "clips" / "default.wav")

    # Config carries env defaults; the explicit --man flag must WIN over the env man default.
    env_man = _wav(tmp_path / "clips" / "env_man.wav")
    config = AppConfig(voice_defaults={"man": env_man, "default": default_clip})
    deps = build_deps(config=config)

    rc = _run(wd, "assign-voice", "--rest", "--man", man_clip, "--woman", woman_clip, deps=deps)
    assert rc == 0
    out = capsys.readouterr().out
    assert "assigned defaults to 3 speaker(s)" in out

    project = WorkspaceStore.for_dir(wd).load()
    assert _clip_path_for(project, "Bob") == man_clip  # flag beat env default
    assert _clip_path_for(project, "Sally") == woman_clip
    assert _clip_path_for(project, "Echo") == default_clip  # unknown -> --default
    assert _clip_path_for(project, "Aldous") == voiced_clip  # pre-voiced untouched
    assert unresolved_voices(project) == []  # gate criterion 3 cleared


def test_rest_uses_env_defaults_when_no_flag(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    env_man = _wav(tmp_path / "clips" / "env_man.wav")
    env_woman = _wav(tmp_path / "clips" / "env_woman.wav")
    env_default = _wav(tmp_path / "clips" / "env_default.wav")
    config = AppConfig(voice_defaults={"man": env_man, "woman": env_woman, "default": env_default})
    deps = build_deps(config=config)

    # No flags at all: every category resolves from the configured env defaults.
    rc = _run(wd, "assign-voice", "--rest", deps=deps)
    assert rc == 0

    project = WorkspaceStore.for_dir(wd).load()
    assert _clip_path_for(project, "Bob") == env_man
    assert _clip_path_for(project, "Sally") == env_woman
    assert _clip_path_for(project, "Echo") == env_default
    assert unresolved_voices(project) == []


# --------------------------------------------------------------------------- #
# coverage error: uncovered category -> non-zero exit, NOTHING assigned
# --------------------------------------------------------------------------- #
def test_rest_uncovered_category_errors_and_assigns_nothing(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    man_clip = _wav(tmp_path / "clips" / "man.wav")
    deps = build_deps(config=AppConfig())  # no env defaults

    # Only --man given: Sally (woman) and Echo (unknown) are uncovered (no --default).
    rc = _run(wd, "assign-voice", "--rest", "--man", man_clip, deps=deps)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no clip for categories" in err
    assert "woman (speakers: Sally)" in err
    assert "unknown (speakers: Echo)" in err

    # NOTHING was assigned: the project on disk is unchanged (all three still unvoiced).
    project = WorkspaceStore.for_dir(wd).load()
    assert _clip_path_for(project, "Bob") is None
    assert _clip_path_for(project, "Sally") is None
    assert _clip_path_for(project, "Echo") is None


def test_rest_no_clips_at_all_errors(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    rc = _run(wd, "assign-voice", "--rest", deps=build_deps(config=AppConfig()))
    assert rc == 1
    assert "no clip for categories" in capsys.readouterr().err


def test_rest_default_covers_every_category(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    default_clip = _wav(tmp_path / "clips" / "default.wav")
    rc = _run(wd, "assign-voice", "--rest", "--default", default_clip, deps=build_deps())
    assert rc == 0
    project = WorkspaceStore.for_dir(wd).load()
    # man/woman/unknown all fall back to --default; shared clip is fine.
    for name in ("Bob", "Sally", "Echo"):
        assert _clip_path_for(project, name) == default_clip
    assert unresolved_voices(project) == []


def test_rest_shared_path_registers_exactly_one_voice_clip(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
) -> None:
    # Register-once optimization: 3 speakers (Bob/Sally/Echo) sharing one --default path must
    # create exactly ONE new VoiceClip record (reused across all three), not three, and all
    # three point at that same clip id.
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    before = len(WorkspaceStore.for_dir(wd).load().voice_clips)  # 1 (Aldous)

    default_clip = _wav(tmp_path / "clips" / "default.wav")
    assert _run(wd, "assign-voice", "--rest", "--default", default_clip, deps=build_deps()) == 0

    project = WorkspaceStore.for_dir(wd).load()
    assert len(project.voice_clips) == before + 1  # one shared record, not three
    shared = {
        next(s for s in project.speakers if s.name == name).voice_clip_id
        for name in ("Bob", "Sally", "Echo")
    }
    assert len(shared) == 1  # all three resolve to the SAME VoiceClip id


# --------------------------------------------------------------------------- #
# edge cases: no-op when all voiced; bad clip path; single-assign path intact
# --------------------------------------------------------------------------- #
def test_rest_noop_when_all_voiced(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    default_clip = _wav(tmp_path / "clips" / "default.wav")
    assert _run(wd, "assign-voice", "--rest", "--default", default_clip, deps=build_deps()) == 0
    capsys.readouterr()
    # Second --rest: everyone is voiced now -> clean no-op.
    rc = _run(wd, "assign-voice", "--rest", "--default", default_clip, deps=build_deps())
    assert rc == 0
    assert "already voiced" in capsys.readouterr().out


def test_rest_bad_clip_path_errors(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    rc = _run(
        wd, "assign-voice", "--rest", "--default", str(tmp_path / "nope.wav"), deps=build_deps()
    )
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_rest_rejects_positional_arguments(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    rc = _run(wd, "assign-voice", "Bob", "--rest", deps=build_deps())
    assert rc == 1
    assert "no positional" in capsys.readouterr().err


def test_single_assign_path_still_works(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    clip = _wav(tmp_path / "clips" / "bob.wav")
    assert _run(wd, "assign-voice", "Bob", clip, deps=build_deps()) == 0
    assert "assigned bob.wav to Bob" in capsys.readouterr().out
    assert _clip_path_for(WorkspaceStore.for_dir(wd).load(), "Bob") == clip


def test_single_assign_missing_clip_reports_error(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    rc = _run(wd, "assign-voice", "Bob", deps=build_deps())  # no CLIP positional, no --rest
    assert rc == 1
    assert "requires SPEAKER and CLIP" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# speakers listing shows categories
# --------------------------------------------------------------------------- #
def test_speakers_shows_categories(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    voiced_clip = _wav(tmp_path / "clips" / "aldous.wav")
    wd = _mixed_project(tmp_workspace, sample_epub, voiced_clip)
    assert _run(wd, "speakers", deps=build_deps()) == 0
    out = capsys.readouterr().out
    assert "Bob (character) [man] - NO VOICE" in out
    assert "Sally (character) [woman] - NO VOICE" in out
    assert "Echo (character) [unknown] - NO VOICE" in out
    assert "Aldous (character) [man] - voiced" in out


# --------------------------------------------------------------------------- #
# gate + full run: --rest clears criterion 3, then run --auto-accept completes
# --------------------------------------------------------------------------- #
def test_rest_then_run_auto_accept_completes_to_m4b(
    tmp_path: Path,
    sample_epub: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wd = tmp_path / "ws"
    default_clip = _wav(tmp_path / "clips" / "default.wav")
    assembler = FakeM4BAssembler()
    deps = build_deps(assembler=assembler)

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    assert _run(wd, "run", deps=deps) == 3  # halts at review (unvoiced cast)
    capsys.readouterr()

    # Discovered speakers are `unknown` (fake classifier is a no-op) -> --default covers all.
    rc = _run(wd, "assign-voice", "--rest", "--default", default_clip, deps=deps)
    assert rc == 0
    assert "assigned defaults to" in capsys.readouterr().out

    assert _run(wd, "run", "--auto-accept", deps=deps) == 0
    assert "output:" in capsys.readouterr().out
    assert len(assembler.requests) == 1
    project = WorkspaceStore.for_dir(wd).load()
    for stage in StageName:
        assert project.stage_status[str(stage)] == ReviewStatus.COMPLETED

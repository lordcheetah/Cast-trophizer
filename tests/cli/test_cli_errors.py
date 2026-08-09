"""CLI error/edge paths: each fails fast with an actionable message + non-zero exit."""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer import cli
from casttrophizer.domain.enums import StageName
from casttrophizer.workspace.store import WorkspaceStore
from tests.cli.conftest import build_deps
from tests.data.make_sample_epub import make_epub_image_only
from tests.fakes import FakeLLMProvider, FakeM4BAssembler, FakeTTSProvider


def _new_and_attribute(wd: Path, sample_epub: Path) -> None:
    """Advance a fresh project through attribute (halts at review) with working fakes."""
    deps = build_deps()
    assert cli.main(["--workdir", str(wd), "new", "--epub", str(sample_epub)], deps=deps) == 0
    assert cli.main(["--workdir", str(wd), "run"], deps=deps) == 3


def _assign_all_voices(wd: Path, clip: str) -> None:
    deps = build_deps()
    for token in ("0", "Alice", "Bob"):
        assert cli.main(["--workdir", str(wd), "assign-voice", token, clip], deps=deps) == 0


def test_new_missing_epub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wd = tmp_path / "ws"
    rc = cli.main(
        ["--workdir", str(wd), "new", "--epub", str(tmp_path / "nope.epub")], deps=build_deps()
    )
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_new_unsupported_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wd = tmp_path / "ws"
    txt = tmp_path / "book.txt"
    txt.write_text("not an epub", encoding="utf-8")
    rc = cli.main(["--workdir", str(wd), "new", "--epub", str(txt)], deps=build_deps())
    assert rc == 1
    assert "no parser supports" in capsys.readouterr().err


def test_new_over_existing_without_force(
    tmp_path: Path, sample_epub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wd = tmp_path / "ws"
    deps = build_deps()
    assert cli.main(["--workdir", str(wd), "new", "--epub", str(sample_epub)], deps=deps) == 0
    capsys.readouterr()
    rc = cli.main(["--workdir", str(wd), "new", "--epub", str(sample_epub)], deps=build_deps())
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    # --force overwrites cleanly
    rc = cli.main(
        ["--workdir", str(wd), "new", "--epub", str(sample_epub), "--force"], deps=build_deps()
    )
    assert rc == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["run"],
        ["status"],
        ["speakers"],
        ["assign-voice", "0", "clip.wav"],
    ],
)
def test_command_without_project(
    tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    wd = tmp_path / "empty"
    rc = cli.main(["--workdir", str(wd), *argv], deps=build_deps())
    assert rc == 1
    assert "no project" in capsys.readouterr().err


def test_run_attribute_pending_no_llm(
    tmp_path: Path, sample_epub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wd = tmp_path / "ws"
    deps = build_deps(llm_factory=lambda cfg: FakeLLMProvider(available=False))
    assert cli.main(["--workdir", str(wd), "new", "--epub", str(sample_epub)], deps=deps) == 0
    capsys.readouterr()

    rc = cli.main(["--workdir", str(wd), "run"], deps=deps)
    assert rc == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    # fail-fast: the preflight ran before any stage, so parse never executed
    project = WorkspaceStore.for_dir(wd).load()
    assert str(StageName.PARSE) not in project.stage_status


def test_run_synthesize_without_tts_extra(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unavailable TTS provider (tts extra missing) FAILs at the synthesize stage guard.

    The CLI no longer preflights the tts extra: the provider is built (cheap, no import), then
    SynthesizeStage's own guard returns FAILED "TTS provider ... unavailable" (exit 1),
    reported to stdout — not an exit-2 preflight.
    """
    wd = tmp_path / "ws"
    _new_and_attribute(wd, sample_epub)
    _assign_all_voices(wd, str(fake_voice_clips[0]))
    capsys.readouterr()

    deps = build_deps(tts_factory=lambda cfg: FakeTTSProvider(available=False))
    rc = cli.main(["--workdir", str(wd), "run", "--auto-accept"], deps=deps)
    assert rc == 1
    assert "unavailable" in capsys.readouterr().out


def test_run_assemble_without_ffmpeg(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing ffmpeg FAILs at the assemble stage guard (exit 1), not an exit-2 preflight.

    The CLI no longer preflights ffmpeg: synthesize completes, then AssembleStage's guard
    returns FAILED "ffmpeg not found on PATH ..." reported to stdout.
    """
    wd = tmp_path / "ws"
    _new_and_attribute(wd, sample_epub)
    _assign_all_voices(wd, str(fake_voice_clips[0]))
    capsys.readouterr()

    deps = build_deps(assembler=FakeM4BAssembler(available=False))
    rc = cli.main(["--workdir", str(wd), "run", "--auto-accept"], deps=deps)
    assert rc == 1
    assert "ffmpeg" in capsys.readouterr().out


def test_run_image_only_epub_fails_at_parse_not_downstream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A+B regression: an image-only EPUB fails at parse (exit 1), never reaching synthesize.

    Before the fix, parse silently succeeded with zero lines and the vacuous review passed
    straight into synthesize, which failed with the confusing "no TTS provider configured".
    Now parse's whole-book no-text guard FAILs first with an actionable message.
    """
    wd = tmp_path / "ws"
    epub = make_epub_image_only(tmp_path / "inputs" / "scan.epub")
    deps = build_deps()
    assert cli.main(["--workdir", str(wd), "new", "--epub", str(epub)], deps=deps) == 0
    capsys.readouterr()

    rc = cli.main(["--workdir", str(wd), "run"], deps=deps)

    assert rc == 1
    out = capsys.readouterr().out
    assert "no readable text" in out
    # never reached synthesize -> the old confusing downstream error must be absent
    assert "no TTS provider configured" not in out
    # parse status stays unset so a fixed input can re-run
    project = WorkspaceStore.for_dir(wd).load()
    assert str(StageName.PARSE) not in project.stage_status


def test_assign_voice_bad_clip_path(
    tmp_path: Path, sample_epub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wd = tmp_path / "ws"
    _new_and_attribute(wd, sample_epub)
    capsys.readouterr()

    rc = cli.main(
        ["--workdir", str(wd), "assign-voice", "0", str(tmp_path / "missing.wav")],
        deps=build_deps(),
    )
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_assign_voice_unknown_speaker(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    wd = tmp_path / "ws"
    _new_and_attribute(wd, sample_epub)
    capsys.readouterr()

    rc = cli.main(
        ["--workdir", str(wd), "assign-voice", "Nobody", str(fake_voice_clips[0])],
        deps=build_deps(),
    )
    assert rc == 1
    assert "no speaker matching" in capsys.readouterr().err


def test_assign_voice_index_out_of_range(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An out-of-range numeric speaker index fails cleanly (no traceback), non-zero exit."""
    wd = tmp_path / "ws"
    _new_and_attribute(wd, sample_epub)  # 3 speakers -> valid indices are 0..2
    capsys.readouterr()

    rc = cli.main(
        ["--workdir", str(wd), "assign-voice", "9", str(fake_voice_clips[0])],
        deps=build_deps(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "out of range" in err
    assert "Traceback" not in err

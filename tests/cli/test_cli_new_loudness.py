"""``castrun new`` seeds the project's ``tts_params`` from config (incl. the loudness block).

Guards the fixed gap where ``_new_project`` ignored ``config.tts_params``: a fresh project must
carry the loudness settings (and any global tts params) so they feed the per-segment cache key
and the synthesis requests from the very first render.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer import cli
from casttrophizer.audio.loudness import LoudnessSettings
from casttrophizer.config import AppConfig
from casttrophizer.workspace.store import WorkspaceStore
from tests.cli.conftest import build_deps


def _run(wd: Path, *argv: str, deps: cli.CliDeps) -> int:
    return cli.main(["--workdir", str(wd), *argv], deps=deps)


def test_new_seeds_default_loudness_block(tmp_path: Path, sample_epub: Path) -> None:
    wd = tmp_path / "ws"
    deps = build_deps(config=AppConfig())  # all-default config

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0

    project = WorkspaceStore.for_dir(wd).load()
    assert project.tts_params["loudness"] == LoudnessSettings().to_params()


def test_new_seeds_configured_loudness_and_global_params(tmp_path: Path, sample_epub: Path) -> None:
    wd = tmp_path / "ws"
    config = AppConfig(
        loudness_enabled=False,
        loudness_target_lufs=-20.0,
        loudness_peak_dbfs=-2.0,
        loudness_max_gain_db=24.0,
        tts_params={"seed": 42},
    )
    deps = build_deps(config=config)

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0

    project = WorkspaceStore.for_dir(wd).load()
    # Global tts params flow through...
    assert project.tts_params["seed"] == 42
    # ...alongside the resolved loudness block.
    assert project.tts_params["loudness"] == {
        "enabled": False,
        "target_lufs": -20.0,
        "peak_ceiling_dbfs": -2.0,
        "max_gain_db": 24.0,
    }

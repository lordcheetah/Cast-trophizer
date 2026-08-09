"""``create_project`` / ``open_project`` — validation, overwrite guard, and load errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer.app_service.projects import (
    ProjectExistsError,
    create_project,
    open_project,
    seed_tts_params,
)
from casttrophizer.audio.loudness import LoudnessSettings
from casttrophizer.config import AppConfig
from casttrophizer.errors import EbookParseError, WorkspaceError
from casttrophizer.workspace.store import WorkspaceStore


def test_create_project_persists_and_returns_store_and_project(
    tmp_path: Path, sample_epub: Path
) -> None:
    wd = tmp_path / "ws"
    store, project = create_project(sample_epub, None, wd, AppConfig())

    assert store.exists()
    assert project.name == sample_epub.stem  # defaults to the EPUB stem
    assert project.book.source_ebook_path == str(sample_epub.resolve())
    assert project.book.chapters == []  # parse has not run yet
    # tts_params carry the loudness block from config from the very first save
    assert project.tts_params["loudness"] == LoudnessSettings().to_params()


def test_create_project_uses_explicit_name(tmp_path: Path, sample_epub: Path) -> None:
    _, project = create_project(sample_epub, "My Book", tmp_path / "ws", AppConfig())
    assert project.name == "My Book"


def test_create_project_missing_epub_raises_ebook_parse_error(tmp_path: Path) -> None:
    with pytest.raises(EbookParseError, match="not found"):
        create_project(tmp_path / "nope.epub", None, tmp_path / "ws", AppConfig())


def test_create_project_unsupported_format_raises_ebook_parse_error(tmp_path: Path) -> None:
    txt = tmp_path / "book.txt"
    txt.write_text("not an epub", encoding="utf-8")
    with pytest.raises(EbookParseError, match="no parser supports"):
        create_project(txt, None, tmp_path / "ws", AppConfig())


def test_create_project_over_existing_raises_unless_overwrite(
    tmp_path: Path, sample_epub: Path
) -> None:
    wd = tmp_path / "ws"
    create_project(sample_epub, None, wd, AppConfig())

    with pytest.raises(ProjectExistsError, match="already exists"):
        create_project(sample_epub, None, wd, AppConfig())

    # overwrite=True re-creates cleanly
    _, project = create_project(sample_epub, None, wd, AppConfig(), overwrite=True)
    assert project.name == sample_epub.stem


def test_open_project_missing_raises_workspace_error(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="no project"):
        open_project(tmp_path / "empty")


def test_open_project_loads_saved_project(tmp_path: Path, sample_epub: Path) -> None:
    wd = tmp_path / "ws"
    _, created = create_project(sample_epub, "Reopened", wd, AppConfig())

    store, project = open_project(wd)
    assert isinstance(store, WorkspaceStore)
    assert project.name == "Reopened"
    assert project.id == created.id


def test_seed_tts_params_folds_global_params_and_loudness() -> None:
    config = AppConfig(loudness_target_lufs=-20.0, tts_params={"seed": 42})
    params = seed_tts_params(config)
    assert params["seed"] == 42
    assert params["loudness"] == LoudnessSettings.from_config(config).to_params()

"""Project creation and loading — the orchestration both front-ends share.

Moved out of ``cli.py`` so the CLI and the UI create/open projects through one code path.
Everything here is Qt-free and import-cheap: creating a project validates the ebook format
via :func:`~casttrophizer.ebook.parser_for` and seeds ``tts_params`` from config, but never
parses the book (``ParseStage`` does that on the first run).
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.audio.loudness import LoudnessSettings
from casttrophizer.config import AppConfig
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Project
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.ebook import parser_for
from casttrophizer.errors import CasttrophizerError, EbookParseError, WorkspaceError
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["ProjectExistsError", "seed_tts_params", "create_project", "open_project"]


class ProjectExistsError(CasttrophizerError):
    """A project already exists in the target workspace and ``overwrite`` was not set."""


def seed_tts_params(config: AppConfig) -> dict[str, object]:
    """Build a new project's ``tts_params`` from config: global params + the loudness block.

    Folds ``config.tts_params`` (global synthesis defaults) together with the resolved loudness
    settings under ``"loudness"`` so both feed the cache key and per-segment requests.
    """
    return {
        **config.tts_params,
        "loudness": LoudnessSettings.from_config(config).to_params(),
    }


def _new_project(
    epub: Path, name: str, workspace_dir: Path, tts_params: dict[str, object]
) -> Project:
    """Build the initial project: a book pointing at the (absolute) EPUB, no chapters yet.

    ParseStage fills ``book.chapters`` and overwrites the placeholder title/author from the
    parsed metadata on its first ``run``. Everything else (speakers, voice clips, stage
    status) starts empty. ``tts_params`` (seeded from :class:`AppConfig`, including the
    ``"loudness"`` block) rides into the project so it feeds the per-segment cache key and the
    synthesis requests from the very first render.
    """
    book = Book(
        title=name,
        author="",
        source_ebook_path=str(epub.resolve()),
        cover_image_path=None,
        chapters=[],
    )
    return Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name=name,
        workspace_dir=str(workspace_dir),
        book=book,
        tts_params=tts_params,
    )


def create_project(
    epub: Path,
    name: str | None,
    workspace_dir: Path,
    config: AppConfig,
    *,
    overwrite: bool = False,
) -> tuple[WorkspaceStore, Project]:
    """Validate the EPUB, build a fresh project, persist it, and return ``(store, project)``.

    Validation order mirrors the old ``cli.cmd_new``: the file must exist and its format must
    be supported (both raise :class:`~casttrophizer.errors.EbookParseError`) before any write.
    An existing project raises :class:`ProjectExistsError` unless ``overwrite`` is set. ``name``
    defaults to the EPUB's filename stem.
    """
    if not epub.is_file():
        raise EbookParseError(f"epub not found: {epub}")
    parser_for(epub)  # validate the format is supported before writing anything

    store = WorkspaceStore.for_dir(workspace_dir)
    if store.exists() and not overwrite:
        raise ProjectExistsError(f"a project already exists at {workspace_dir}")

    project = _new_project(epub, name or epub.stem, workspace_dir, seed_tts_params(config))
    store.save(project)
    return store, project


def open_project(workspace_dir: Path) -> tuple[WorkspaceStore, Project]:
    """Load an existing project from ``workspace_dir``; raise if none is present.

    Raises :class:`~casttrophizer.errors.WorkspaceError` (clear message) when the workspace
    has no ``project.json`` yet.
    """
    store = WorkspaceStore.for_dir(workspace_dir)
    if not store.exists():
        raise WorkspaceError(f"no project at {workspace_dir}; create one first")
    return store, store.load()

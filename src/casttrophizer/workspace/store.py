"""Load and save a project's persisted state as ``project.json``.

The write is **atomic**: the JSON is written to a temporary file in the same directory
and then :func:`os.replace`-d over the destination, so a crash mid-write never leaves a
partial/corrupt ``project.json`` — readers see either the old file or the new one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from casttrophizer.domain.models import Project
from casttrophizer.domain.serialization import project_from_dict, project_to_dict
from casttrophizer.errors import WorkspaceError
from casttrophizer.workspace.layout import WorkspaceLayout

__all__ = ["WorkspaceStore"]


class WorkspaceStore:
    """Reads/writes the ``Project`` JSON for one workspace via a :class:`WorkspaceLayout`."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        self._layout = layout

    @classmethod
    def for_dir(cls, workspace_dir: str | Path) -> WorkspaceStore:
        """Build a store for ``workspace_dir``."""
        return cls(WorkspaceLayout.for_dir(workspace_dir))

    @property
    def layout(self) -> WorkspaceLayout:
        """The underlying path resolver."""
        return self._layout

    def exists(self) -> bool:
        """True if a ``project.json`` is present in this workspace."""
        return self._layout.project_file.is_file()

    def save(self, project: Project) -> None:
        """Atomically serialize ``project`` to ``project.json``.

        Ensures the workspace directory tree exists, writes to a sibling temp file,
        flushes + fsyncs, then :func:`os.replace`-s it into place.
        """
        self._layout.ensure_dirs()
        dest = self._layout.project_file
        payload = json.dumps(project_to_dict(project), indent=2, ensure_ascii=False)

        fd, tmp_name = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".project-", suffix=".json.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, dest)
        except OSError as exc:
            # Best-effort cleanup of the temp file; never leave it behind on failure.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise WorkspaceError(f"failed to save project to {dest}: {exc}") from exc

    def load(self) -> Project:
        """Load and deserialize ``project.json`` into a :class:`Project`."""
        path = self._layout.project_file
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkspaceError(f"failed to read project from {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"{path} is not valid JSON: {exc}") from exc
        return project_from_dict(data)

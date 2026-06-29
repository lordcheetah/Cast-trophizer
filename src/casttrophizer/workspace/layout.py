"""Resolve the on-disk paths inside a per-project workspace directory.

The workspace holds **only derived artifacts**. The user's source ebook and voice
clips stay where they are (referenced by absolute path); nothing is copied or mutated
into the workspace.

Layout::

    <workspace_dir>/
    ├── project.json      # serialized Project (atomic write)
    ├── audio/            # AudioCache: per-segment generated audio (<cache_key>.wav)
    ├── output/           # final assembled <book>.m4b
    └── logs/             # optional per-stage logs
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["WorkspaceLayout"]

PROJECT_FILENAME = "project.json"


@dataclass(frozen=True)
class WorkspaceLayout:
    """Path resolver for a single project workspace rooted at ``root``."""

    root: Path

    @classmethod
    def for_dir(cls, workspace_dir: str | Path) -> WorkspaceLayout:
        """Build a layout for ``workspace_dir`` (does not touch the filesystem)."""
        return cls(Path(workspace_dir))

    @property
    def project_file(self) -> Path:
        """Path to ``project.json``."""
        return self.root / PROJECT_FILENAME

    @property
    def audio_dir(self) -> Path:
        """Directory holding per-segment cached audio."""
        return self.root / "audio"

    @property
    def output_dir(self) -> Path:
        """Directory holding the final assembled audiobook."""
        return self.root / "output"

    @property
    def logs_dir(self) -> Path:
        """Directory for optional per-stage logs."""
        return self.root / "logs"

    def audio_path(self, cache_key: str) -> Path:
        """Path to a single cached audio file for ``cache_key``."""
        return self.audio_dir / f"{cache_key}.wav"

    def ensure_dirs(self) -> None:
        """Create the workspace directory tree if it does not already exist."""
        for d in (self.root, self.audio_dir, self.output_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

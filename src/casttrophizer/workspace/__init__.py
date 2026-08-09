"""On-disk per-project workspace: path layout, project JSON store, and audio cache.

Qt-free and provider-free. Source ebooks and voice clips are treated as read-only
inputs; only derived artifacts are written here.
"""

from __future__ import annotations

from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.layout import WorkspaceLayout
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["AudioCache", "WorkspaceLayout", "WorkspaceStore"]

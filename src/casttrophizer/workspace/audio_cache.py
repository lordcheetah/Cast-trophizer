"""Deterministic per-segment audio cache.

Because TTS renders **per segment**, the cache key is computed from the segment text,
the voice clip used to render it, and the synthesis params::

    cache_key = hash(segment_text + voice_clip_id + tts_params)

The key is stable across runs and changes **iff** any of those three inputs change.
That is what lets :class:`SynthesizeStage` skip regenerating a segment whose
text/voice/params are unchanged — the "full render is expensive" principle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from casttrophizer.domain.models import Project, Segment
from casttrophizer.workspace.layout import WorkspaceLayout

__all__ = ["AudioCache"]


def _canonical_params(params: Mapping[str, Any]) -> str:
    """Serialize ``params`` deterministically (sorted keys) for hashing."""
    return json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class AudioCache:
    """Resolves cache keys and paths for per-segment generated audio."""

    def __init__(self, layout: WorkspaceLayout) -> None:
        self._layout = layout

    @staticmethod
    def compute_key(
        segment_text: str,
        voice_clip_id: str | None,
        tts_params: Mapping[str, Any],
    ) -> str:
        """Compute the deterministic cache key for a segment's audio.

        ``voice_clip_id`` may be ``None`` (narrator or not-yet-assigned voice); it is
        folded into the hash as an empty string so the key is still stable.
        """
        h = hashlib.sha256()
        h.update(segment_text.encode("utf-8"))
        h.update(b"\x00")
        h.update((voice_clip_id or "").encode("utf-8"))
        h.update(b"\x00")
        h.update(_canonical_params(tts_params).encode("utf-8"))
        return h.hexdigest()

    @classmethod
    def key_for(cls, segment: Segment, project: Project) -> str:
        """Compute the cache key for ``segment`` using ``project``'s global tts params.

        The voice clip id is resolved from the segment's speaker. If the speaker (or its
        voice assignment) is missing, the voice component is empty — the key stays
        deterministic and will change once a voice is assigned.
        """
        voice_clip_id = cls._voice_clip_id_for(segment, project)
        return cls.compute_key(segment.text, voice_clip_id, project.tts_params)

    @staticmethod
    def _voice_clip_id_for(segment: Segment, project: Project) -> str | None:
        if segment.speaker_id is None:
            return None
        for speaker in project.speakers:
            if speaker.id == segment.speaker_id:
                return speaker.voice_clip_id
        return None

    def path_for_key(self, cache_key: str) -> Path:
        """Path to the cached ``.wav`` for ``cache_key``."""
        return self._layout.audio_path(cache_key)

    def has(self, cache_key: str) -> bool:
        """True if audio for ``cache_key`` already exists on disk."""
        return self.path_for_key(cache_key).is_file()

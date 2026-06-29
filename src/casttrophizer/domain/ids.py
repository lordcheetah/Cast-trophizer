"""Typed ID aliases and a generator for domain object identifiers.

IDs are opaque strings. The aliases are documentation-only (``X = str``) so they
serialize trivially and never require a custom (de)serializer, while still making
signatures self-describing.
"""

from __future__ import annotations

import uuid

__all__ = [
    "Id",
    "ProjectId",
    "ChapterId",
    "LineId",
    "SegmentId",
    "SpeakerId",
    "VoiceClipId",
    "SuggestionId",
    "new_id",
]

Id = str
ProjectId = str
ChapterId = str
LineId = str
SegmentId = str
SpeakerId = str
VoiceClipId = str
SuggestionId = str


def new_id(prefix: str = "") -> Id:
    """Return a fresh unique id, optionally namespaced by ``prefix``.

    Example: ``new_id("seg")`` -> ``"seg_3f1c..."``. The prefix is cosmetic; it aids
    debugging and log-reading and is not parsed anywhere.
    """
    token = uuid.uuid4().hex
    return f"{prefix}_{token}" if prefix else token

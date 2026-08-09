"""Domain layer: pure data shapes, enums, ids, and their (de)serialization.

This package has no I/O, no provider imports, and no Qt. It is safe to import from
anywhere (UI, pipeline, providers, tests).
"""

from __future__ import annotations

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.ids import (
    ChapterId,
    Id,
    LineId,
    ProjectId,
    SegmentId,
    SpeakerId,
    SuggestionId,
    VoiceClipId,
    new_id,
)
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    TextSuggestion,
    VoiceClip,
)
from casttrophizer.domain.serialization import (
    CURRENT_SCHEMA_VERSION,
    project_from_dict,
    project_to_dict,
)

__all__ = [
    # enums
    "ReviewStatus",
    "SpeakerRole",
    "StageName",
    # ids
    "Id",
    "ProjectId",
    "ChapterId",
    "LineId",
    "SegmentId",
    "SpeakerId",
    "VoiceClipId",
    "SuggestionId",
    "new_id",
    # models
    "Book",
    "Chapter",
    "Line",
    "Project",
    "Segment",
    "Speaker",
    "TextSuggestion",
    "VoiceClip",
    # serialization
    "CURRENT_SCHEMA_VERSION",
    "project_from_dict",
    "project_to_dict",
]

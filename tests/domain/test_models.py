"""Domain serialization tests: lossless round-trip, schema_version, migration errors."""

from __future__ import annotations

import pytest

from casttrophizer.domain.models import Project
from casttrophizer.domain.serialization import (
    CURRENT_SCHEMA_VERSION,
    project_from_dict,
    project_to_dict,
)
from casttrophizer.errors import MigrationError, SerializationError


def test_round_trip_is_lossless(sample_project: Project) -> None:
    data = project_to_dict(sample_project)
    restored = project_from_dict(data)
    assert restored == sample_project


def test_schema_version_is_serialized(sample_project: Project) -> None:
    data = project_to_dict(sample_project)
    assert data["schema_version"] == CURRENT_SCHEMA_VERSION


def test_segment_audio_fields_round_trip(sample_project: Project) -> None:
    # The TTS render unit is the Segment, so audio_cache_key / audio_status live there.
    seg = sample_project.book.chapters[0].lines[0].segments[0]
    seg.audio_cache_key = "deadbeef"
    data = project_to_dict(sample_project)
    restored = project_from_dict(data)
    restored_seg = restored.book.chapters[0].lines[0].segments[0]
    assert restored_seg.audio_cache_key == "deadbeef"


def test_future_schema_version_raises_migration_error(sample_project: Project) -> None:
    data = project_to_dict(sample_project)
    data["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    with pytest.raises(MigrationError):
        project_from_dict(data)


def test_unknown_old_schema_version_raises_migration_error(
    sample_project: Project,
) -> None:
    data = project_to_dict(sample_project)
    data["schema_version"] = 0
    with pytest.raises(MigrationError):
        project_from_dict(data)


def test_missing_schema_version_raises_serialization_error(
    sample_project: Project,
) -> None:
    data = project_to_dict(sample_project)
    del data["schema_version"]
    with pytest.raises(SerializationError):
        project_from_dict(data)


def test_future_schema_error_message_names_the_versions(sample_project: Project) -> None:
    # §9.2 requires a *clear* migration error: the message must surface both the offending
    # version and the supported one so the user knows to upgrade.
    data = project_to_dict(sample_project)
    data["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    with pytest.raises(MigrationError) as excinfo:
        project_from_dict(data)
    msg = str(excinfo.value)
    assert str(CURRENT_SCHEMA_VERSION + 1) in msg
    assert str(CURRENT_SCHEMA_VERSION) in msg


def test_malformed_structure_raises_serialization_error(sample_project: Project) -> None:
    # A current-version dict that is missing a required field is a structural error, not a
    # migration error — it must raise SerializationError, not KeyError leaking out.
    data = project_to_dict(sample_project)
    del data["book"]
    with pytest.raises(SerializationError):
        project_from_dict(data)


def test_round_trip_detects_a_changed_value(sample_project: Project) -> None:
    # Guards against an over-loose equality: a single mutated leaf must break round-trip
    # equality, proving the dataclass __eq__ compares nested content (not identity).
    data = project_to_dict(sample_project)
    restored = project_from_dict(data)
    assert restored == sample_project
    restored.book.chapters[0].lines[0].segments[0].text = "MUTATED"
    assert restored != sample_project


def test_round_trip_via_json_text_is_lossless(sample_project: Project) -> None:
    # The on-disk path is JSON text; round-tripping through json.dumps/loads (not just the
    # dict) confirms every value is JSON-native and survives serialization.
    import json

    blob = json.dumps(project_to_dict(sample_project))
    restored = project_from_dict(json.loads(blob))
    assert restored == sample_project

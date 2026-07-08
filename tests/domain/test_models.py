"""Domain serialization tests: lossless round-trip, schema_version, migration errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from casttrophizer.domain.enums import VoiceCategory
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


def test_voice_category_round_trips(sample_project: Project) -> None:
    # Stamp non-default categories and confirm they survive project_to_dict/from_dict.
    sample_project.speakers[0].category = VoiceCategory.MAN
    sample_project.speakers[1].category = VoiceCategory.WOMAN
    restored = project_from_dict(project_to_dict(sample_project))
    assert [sp.category for sp in restored.speakers] == [
        VoiceCategory.MAN,
        VoiceCategory.WOMAN,
    ]
    assert restored == sample_project


def test_v1_to_v2_migration_defaults_speakers_to_unknown(sample_project: Project) -> None:
    # A hand-built v1 dict (schema_version=1, no per-speaker `category` key) must migrate:
    # every speaker defaults to `unknown` and the loaded project re-serializes at the current
    # version (the v1->v2 category default is what this test isolates).
    data = project_to_dict(sample_project)
    data["schema_version"] = 1
    for speaker in data["speakers"]:
        del speaker["category"]

    restored = project_from_dict(data)
    assert restored.schema_version == CURRENT_SCHEMA_VERSION == 3
    assert all(sp.category == VoiceCategory.UNKNOWN for sp in restored.speakers)
    # Re-serializing stamps the current version and writes the category key back.
    reserialized = project_to_dict(restored)
    assert reserialized["schema_version"] == 3
    assert all(sp["category"] == "unknown" for sp in reserialized["speakers"])


def test_segment_audio_seed_round_trips(sample_project: Project) -> None:
    # The per-segment re-roll seed (schema v3) round-trips losslessly.
    seg = sample_project.book.chapters[0].lines[0].segments[0]
    seg.audio_seed = 123456
    restored = project_from_dict(project_to_dict(sample_project))
    assert restored.book.chapters[0].lines[0].segments[0].audio_seed == 123456
    assert restored == sample_project


def test_v2_to_v3_migration_defaults_segments_audio_seed_to_none(sample_project: Project) -> None:
    # A hand-built v2 dict (schema_version=2, no per-segment `audio_seed` key) must migrate: every
    # segment gains `audio_seed=None` and the loaded project re-serializes at v3. Mirrors the
    # v1->v2 test; `None` is deliberately NOT folded into the cache key, so no WAV invalidation.
    data = project_to_dict(sample_project)
    data["schema_version"] = 2
    for chapter in data["book"]["chapters"]:
        for line in chapter["lines"]:
            for segment in line["segments"]:
                del segment["audio_seed"]

    restored = project_from_dict(data)
    assert restored.schema_version == CURRENT_SCHEMA_VERSION == 3
    assert all(
        seg.audio_seed is None
        for ch in restored.book.chapters
        for ln in ch.lines
        for seg in ln.segments
    )
    reserialized = project_to_dict(restored)
    assert reserialized["schema_version"] == 3
    assert all(
        seg["audio_seed"] is None
        for ch in reserialized["book"]["chapters"]
        for ln in ch["lines"]
        for seg in ln["segments"]
    )


def test_v1_dict_migrates_all_the_way_to_v3(sample_project: Project) -> None:
    # A v1 dict (no `category`, no `audio_seed`) must chain v1->v2->v3 in one load: speakers default
    # to `unknown` AND segments default `audio_seed=None`.
    data = project_to_dict(sample_project)
    data["schema_version"] = 1
    for speaker in data["speakers"]:
        del speaker["category"]
    for chapter in data["book"]["chapters"]:
        for line in chapter["lines"]:
            for segment in line["segments"]:
                del segment["audio_seed"]

    restored = project_from_dict(data)
    assert restored.schema_version == 3
    assert all(sp.category == VoiceCategory.UNKNOWN for sp in restored.speakers)
    assert all(
        seg.audio_seed is None
        for ch in restored.book.chapters
        for ln in ch.lines
        for seg in ln.segments
    )


def test_voice_category_coerce_maps_unknown_strings() -> None:
    assert VoiceCategory.coerce("MAN") is VoiceCategory.MAN
    assert VoiceCategory.coerce("  Woman ") is VoiceCategory.WOMAN
    assert VoiceCategory.coerce("android") is VoiceCategory.UNKNOWN
    assert VoiceCategory.coerce(None) is VoiceCategory.UNKNOWN


def test_round_trip_via_json_text_is_lossless(sample_project: Project) -> None:
    # The on-disk path is JSON text; round-tripping through json.dumps/loads (not just the
    # dict) confirms every value is JSON-native and survives serialization.
    import json

    blob = json.dumps(project_to_dict(sample_project))
    restored = project_from_dict(json.loads(blob))
    assert restored == sample_project


def test_v2_garbage_category_is_a_load_error_not_silently_coerced(
    sample_project: Project,
) -> None:
    # STRICT load-time validation (the whole point of a StrEnum per the plan/design): a
    # *persisted* v2 project whose category is not a real VoiceCategory member is a corrupt
    # file, surfaced as SerializationError — NOT silently coerced to `unknown`. (VoiceCategory
    # .coerce is only for the provider->domain boundary, never the on-disk load path, which
    # uses VoiceCategory(d["category"]).) This pins the intended fail-loud behavior so a future
    # loosening of the loader is a conscious change.
    data = project_to_dict(sample_project)
    data["speakers"][0]["category"] = "wizard"  # not a VoiceCategory member
    with pytest.raises(SerializationError):
        project_from_dict(data)


def test_v1_to_v2_preserves_a_category_key_if_somehow_present(
    sample_project: Project,
) -> None:
    # The migration uses setdefault, so a v1 dict that (unexpectedly) already carries a
    # category is not clobbered back to `unknown`. Guards the "add missing default" semantics.
    data = project_to_dict(sample_project)
    data["schema_version"] = 1
    data["speakers"][0]["category"] = "man"  # pre-existing value survives migration
    del data["speakers"][1]["category"]  # this one gets the default
    restored = project_from_dict(data)
    assert restored.speakers[0].category == VoiceCategory.MAN
    assert restored.speakers[1].category == VoiceCategory.UNKNOWN


def test_v1_project_on_disk_migrates_through_the_workspace_store(
    sample_project: Project,
) -> None:
    # Real-project safety (protects the user's existing `.\work\eb1` v1 workspace): write a
    # v1-shaped project.json to disk (schema_version=1, speakers with NO `category` key) and
    # load it through the ACTUAL WorkspaceStore path (read_text -> json.loads -> migrate), the
    # same code `castrun` runs. Every speaker must migrate to `unknown`, and a subsequent save
    # must re-persist the file at v2 with the category keys written back.
    import json

    from casttrophizer.workspace.layout import WorkspaceLayout
    from casttrophizer.workspace.store import WorkspaceStore

    root = Path(sample_project.workspace_dir)
    store = WorkspaceStore(WorkspaceLayout.for_dir(root))
    store.save(sample_project)  # produces a valid v2 file first

    # Downgrade the on-disk file to a v1 shape by hand (as a pre-feature project would look).
    v1 = json.loads(store.layout.project_file.read_text(encoding="utf-8"))
    v1["schema_version"] = 1
    for speaker in v1["speakers"]:
        del speaker["category"]
    store.layout.project_file.write_text(json.dumps(v1, indent=2), encoding="utf-8")

    loaded = store.load()  # the exact path the CLI uses
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION == 3
    assert all(sp.category == VoiceCategory.UNKNOWN for sp in loaded.speakers)

    # Re-save and confirm the file is now a clean current-version file with category keys present.
    store.save(loaded)
    reread = json.loads(store.layout.project_file.read_text(encoding="utf-8"))
    assert reread["schema_version"] == 3
    assert all(sp["category"] == "unknown" for sp in reread["speakers"])

"""Unit tests for :mod:`casttrophizer.app_service.voices` — the shared CLI/UI bulk-voice seam.

Covers ``resolve_category_clip`` precedence (per-category override > env default > shared
``default`` override > env ``default``), ``plan_bulk_voice`` targeting (exactly
``unresolved_speakers``, uncovered bucketing, no mutation), and ``apply_bulk_voice`` (one clip
per distinct path, real-``ReviewService`` assign + persist, per-category counts, ValueError on a
missing path). The CLI ``--rest`` regression guard is ``tests/cli/test_cli_assign_voice_rest.py``
(kept green unchanged).
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from casttrophizer.app_service.voices import (
    BulkVoicePlan,
    apply_bulk_voice,
    plan_bulk_voice,
    resolve_category_clip,
)
from casttrophizer.audio.synthesize import unresolved_speakers
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName, VoiceCategory
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Chapter, Line, Project, Segment, Speaker
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.store import WorkspaceStore


def _wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 128)
    return str(path)


def _mixed_project(tmp_workspace: WorkspaceStore) -> Project:
    """Saved project: unvoiced man/woman/unknown (referenced) + a voiced man (referenced)."""

    def _speaker(name: str, category: VoiceCategory, *, voiced: bool) -> Speaker:
        return Speaker(
            id=new_id("spk"),
            name=name,
            role=SpeakerRole.CHARACTER,
            voice_clip_id=("clip-" + name) if voiced else None,
            category=category,
        )

    voiced = _speaker("Aldous", VoiceCategory.MAN, voiced=True)
    man = _speaker("Bob", VoiceCategory.MAN, voiced=False)
    woman = _speaker("Sally", VoiceCategory.WOMAN, voiced=False)
    unknown = _speaker("Echo", VoiceCategory.UNKNOWN, voiced=False)

    from casttrophizer.domain.models import VoiceClip

    # The voiced man's clip must exist so ``unresolved_speakers`` excludes him.
    voiced_clip_path = _wav(Path(tmp_workspace.layout.root) / "clips" / "aldous.wav")
    voiced_vc = VoiceClip(id="clip-Aldous", source_path=voiced_clip_path, label="Aldous")

    ch_id = new_id("ch")

    def _line(order: int, text: str, sp: Speaker) -> Line:
        return Line(
            id=new_id("line"),
            chapter_id=ch_id,
            order=order,
            text=text,
            segments=[
                Segment(
                    id=new_id("seg"),
                    text=text,
                    speaker_id=sp.id,
                    role=sp.role,
                    confidence=1.0,
                    review_status=ReviewStatus.APPROVED,
                )
            ],
        )

    chapter = Chapter(
        id=ch_id,
        order=0,
        title="One",
        lines=[
            _line(0, '"one," said Aldous.', voiced),
            _line(1, '"two," said Bob.', man),
            _line(2, '"three," said Sally.', woman),
            _line(3, '"four," said Echo.', unknown),
        ],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Mixed",
        workspace_dir=str(tmp_workspace.layout.root),
        book=Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter]),
        speakers=[voiced, man, woman, unknown],
        voice_clips=[voiced_vc],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
        },
    )
    tmp_workspace.save(project)
    return project


# --------------------------------------------------------------------------- #
# resolve_category_clip precedence
# --------------------------------------------------------------------------- #
def test_per_category_override_wins_over_env_default() -> None:
    config = AppConfig(voice_defaults={"man": "/env/man.wav", "default": "/env/default.wav"})
    overrides = {"man": "/flag/man.wav", "default": None}
    assert resolve_category_clip("man", overrides, config) == "/flag/man.wav"


def test_env_default_used_when_no_per_category_override() -> None:
    config = AppConfig(voice_defaults={"man": "/env/man.wav", "default": "/env/default.wav"})
    overrides = {"man": None, "default": None}
    assert resolve_category_clip("man", overrides, config) == "/env/man.wav"


def test_shared_default_override_used_for_unknown_and_uncovered() -> None:
    config = AppConfig(voice_defaults={"default": "/env/default.wav"})
    overrides = {"default": "/flag/default.wav"}
    # unknown has no per-category key -> the shared default override wins over the env default.
    assert resolve_category_clip("unknown", overrides, config) == "/flag/default.wav"
    # a covered-by-nothing man also falls back to the shared default override.
    assert resolve_category_clip("man", {"man": None, "default": "/flag/default.wav"}, config) == (
        "/flag/default.wav"
    )


def test_env_default_used_when_no_default_override() -> None:
    config = AppConfig(voice_defaults={"default": "/env/default.wav"})
    assert resolve_category_clip("unknown", {"default": None}, config) == "/env/default.wav"


def test_returns_none_when_nothing_resolves() -> None:
    assert resolve_category_clip("woman", {"woman": None, "default": None}, AppConfig()) is None


# --------------------------------------------------------------------------- #
# plan_bulk_voice targeting / uncovered / purity
# --------------------------------------------------------------------------- #
def test_plan_targets_exactly_unresolved_speakers(tmp_workspace: WorkspaceStore) -> None:
    project = _mixed_project(tmp_workspace)
    overrides = {"man": "/m.wav", "woman": "/w.wav", "boy": None, "girl": None, "default": "/d.wav"}

    plan = plan_bulk_voice(project, overrides, AppConfig())

    assert not plan.uncovered
    assigned = {sp.id for sp, _ in plan.assignments}
    assert assigned == {sp.id for sp in unresolved_speakers(project)}
    assert "Aldous" not in {sp.name for sp, _ in plan.assignments}  # already voiced, untouched


def test_plan_buckets_uncovered_categories_and_mutates_nothing(
    tmp_workspace: WorkspaceStore,
) -> None:
    project = _mixed_project(tmp_workspace)
    overrides = {"man": "/m.wav", "woman": None, "boy": None, "girl": None, "default": None}

    plan = plan_bulk_voice(project, overrides, AppConfig())

    assert plan.uncovered == {"woman": ["Sally"], "unknown": ["Echo"]}
    assert [sp.name for sp, _ in plan.assignments] == ["Bob"]
    # PURE: nothing was assigned in memory or on disk.
    assert all(sp.voice_clip_id is None for sp in project.speakers if sp.name != "Aldous")
    reloaded = tmp_workspace.load()
    for name in ("Bob", "Sally", "Echo"):
        assert next(s for s in reloaded.speakers if s.name == name).voice_clip_id is None


# --------------------------------------------------------------------------- #
# apply_bulk_voice
# --------------------------------------------------------------------------- #
def test_apply_dedupes_by_path_assigns_and_counts(
    tmp_workspace: WorkspaceStore, tmp_path: Path
) -> None:
    project = _mixed_project(tmp_workspace)
    shared = _wav(tmp_path / "shared.wav")
    overrides = {"man": None, "woman": None, "boy": None, "girl": None, "default": shared}
    plan = plan_bulk_voice(project, overrides, AppConfig())
    before = len(project.voice_clips)

    service = ReviewService(tmp_workspace, project)
    counts = apply_bulk_voice(service, plan)

    assert counts == {"man": 1, "woman": 1, "unknown": 1}
    assert len(project.voice_clips) == before + 1  # ONE shared clip, not three
    reloaded = tmp_workspace.load()
    ids = {
        next(s for s in reloaded.speakers if s.name == name).voice_clip_id
        for name in ("Bob", "Sally", "Echo")
    }
    assert len(ids) == 1 and None not in ids  # all three point at the same registered clip
    assert unresolved_speakers(reloaded) == []  # gate criterion 3 cleared


def test_apply_raises_value_error_on_missing_path(
    tmp_workspace: WorkspaceStore, tmp_path: Path
) -> None:
    project = _mixed_project(tmp_workspace)
    plan = BulkVoicePlan(
        assignments=[(unresolved_speakers(project)[0], str(tmp_path / "nope.wav"))],
        uncovered={},
    )
    service = ReviewService(tmp_workspace, project)

    with pytest.raises(ValueError, match="does not exist"):
        apply_bulk_voice(service, plan)

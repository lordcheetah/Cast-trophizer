"""Attribution policy unit tests — threshold mapping, narrator reservation, registry."""

from __future__ import annotations

from casttrophizer.attribution.policy import (
    ATTRIBUTION_CONFIDENCE_THRESHOLD,
    ensure_narrator,
    resolve_speaker,
    review_status_for,
)
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Project, Speaker
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION


def _project(speakers: list[Speaker] | None = None) -> Project:
    return Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="p",
        workspace_dir="/ws",
        book=Book(title="t", author="a", source_ebook_path="/src.epub"),
        speakers=speakers or [],
    )


# --------------------------------------------------------------------------- #
# review_status_for
# --------------------------------------------------------------------------- #
def test_threshold_default_is_three_quarters() -> None:
    assert ATTRIBUTION_CONFIDENCE_THRESHOLD == 0.75


def test_high_confidence_is_approved() -> None:
    assert review_status_for(0.9) == ReviewStatus.APPROVED


def test_low_confidence_is_needs_review() -> None:
    assert review_status_for(0.5) == ReviewStatus.NEEDS_REVIEW


def test_threshold_boundary_is_approved() -> None:
    assert review_status_for(0.75) == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# ensure_narrator
# --------------------------------------------------------------------------- #
def test_ensure_narrator_creates_one_when_absent() -> None:
    project = _project()
    narrator = ensure_narrator(project)
    assert narrator.role == SpeakerRole.NARRATOR
    assert narrator in project.speakers
    assert len(project.speakers) == 1


def test_ensure_narrator_reuses_existing_no_duplicate() -> None:
    existing = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.NARRATOR)
    project = _project([existing])
    assert ensure_narrator(project) is existing
    assert len(project.speakers) == 1


# --------------------------------------------------------------------------- #
# resolve_speaker
# --------------------------------------------------------------------------- #
def test_resolve_none_is_narrator() -> None:
    project = _project()
    narrator = ensure_narrator(project)
    assert resolve_speaker(project, narrator, None) is narrator


def test_resolve_existing_name_case_insensitive_reuses() -> None:
    project = _project()
    narrator = ensure_narrator(project)
    first = resolve_speaker(project, narrator, "Alice")
    again = resolve_speaker(project, narrator, "alice")
    assert again is first
    assert sum(1 for s in project.speakers if s.name == "Alice") == 1


def test_resolve_new_name_creates_character() -> None:
    project = _project()
    narrator = ensure_narrator(project)
    bob = resolve_speaker(project, narrator, "Bob")
    assert bob.role == SpeakerRole.CHARACTER
    assert bob.voice_clip_id is None
    assert bob in project.speakers

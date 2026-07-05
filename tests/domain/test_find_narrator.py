"""``find_narrator`` — the domain-layer narrator lookup shared by synthesis and attribution.

Pure and offline: identity by ``role == NARRATOR`` (not by name), ``None`` when no narrator
has been reserved, and parity with ``ensure_narrator`` (which delegates here).
"""

from __future__ import annotations

from casttrophizer.attribution.policy import ensure_narrator
from casttrophizer.domain.enums import SpeakerRole
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Project,
    Speaker,
    find_narrator,
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION


def _empty_project() -> Project:
    book = Book(title="", author="", source_ebook_path="x.epub")
    return Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="p",
        workspace_dir="ws",
        book=book,
    )


def test_returns_the_narrator_role_speaker(review_ready_project: Project) -> None:
    narrator = find_narrator(review_ready_project)
    assert narrator is not None
    assert narrator.role == SpeakerRole.NARRATOR


def test_identity_is_by_role_not_name() -> None:
    # A speaker literally named "narrator" but with a CHARACTER role is NOT the narrator; the
    # only speaker with the NARRATOR role is, regardless of its display name.
    project = _empty_project()
    decoy = Speaker(id=new_id("spk"), name="narrator", role=SpeakerRole.CHARACTER)
    real = Speaker(id=new_id("spk"), name="The Voice", role=SpeakerRole.NARRATOR)
    project.speakers = [decoy, real]
    assert find_narrator(project) is real


def test_none_when_no_narrator_reserved() -> None:
    assert find_narrator(_empty_project()) is None


def test_ensure_narrator_creates_then_find_narrator_locates_it() -> None:
    project = _empty_project()
    assert find_narrator(project) is None
    created = ensure_narrator(project)
    assert created.role == SpeakerRole.NARRATOR
    assert find_narrator(project) is created
    # A second ensure_narrator reuses the same speaker (no duplicate).
    assert ensure_narrator(project) is created
    assert sum(1 for sp in project.speakers if sp.role == SpeakerRole.NARRATOR) == 1

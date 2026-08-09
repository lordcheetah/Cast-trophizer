"""Loop-free unit tests for :class:`VoicePresenter` — no Qt, no event loop, no audio device.

A :class:`FakeVoiceView` records what the presenter pushes (including the ``play_clip`` /
``stop_playback`` audition hooks); a **real** :class:`~casttrophizer.review.service.ReviewService`
mutates an in-memory project loaded from ``tmp_workspace``. Every mutating action must funnel
through that service, tick ``on_reviewed``, and land on disk; every audition intent must decide
what/whether to play **without** an audio backend — asserted below without a widget.
"""

from __future__ import annotations

import wave
from pathlib import Path

from casttrophizer.audio.synthesize import unresolved_speakers
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import (
    ReviewStatus,
    SpeakerRole,
    StageName,
    VoiceCategory,
)
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import (
    Book,
    Chapter,
    Line,
    Project,
    Segment,
    Speaker,
    VoiceClip,
)
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.review.service import ReviewService
from casttrophizer.review.voice_view import CategoryOption, SpeakerVoiceRow
from casttrophizer.ui.voice_presenter import VoicePresenter, VoiceView
from casttrophizer.workspace.store import WorkspaceStore


# --------------------------------------------------------------------------- #
# fakes / helpers
# --------------------------------------------------------------------------- #
class FakeVoiceView:
    """Records every call the presenter makes (structurally a :class:`VoiceView`)."""

    def __init__(self) -> None:
        self.speakers: list[list[SpeakerVoiceRow]] = []
        self.options: list[list[CategoryOption]] = []
        self.needs_voice: list[tuple[int, int]] = []
        self.selected: list[int] = []
        self.played: list[str] = []
        self.stopped = 0
        self.errors: list[tuple[str, str]] = []

    def show_speakers(self, rows: list[SpeakerVoiceRow]) -> None:
        self.speakers.append(rows)

    def show_category_options(self, options: list[CategoryOption]) -> None:
        self.options.append(options)

    def show_needs_voice(self, needs_voice: int, referenced_total: int) -> None:
        self.needs_voice.append((needs_voice, referenced_total))

    def select_speaker(self, index: int) -> None:
        self.selected.append(index)

    def play_clip(self, path: str) -> None:
        self.played.append(path)

    def stop_playback(self) -> None:
        self.stopped += 1

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))


class _Recorder:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def _wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 128)
    return str(path)


def _project(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> Project:
    """Saved project: voiced narrator + Alice, unvoiced Bob, file-missing Carol (all referenced).

    ``stage_status[REVIEW]`` starts COMPLETED so an ``unassign`` can be shown to re-open the gate.
    """
    narrator_clip = VoiceClip(
        id=new_id("voice"), source_path=str(fake_voice_clips[0]), label="Narrator"
    )
    alice_clip = VoiceClip(id=new_id("voice"), source_path=str(fake_voice_clips[1]), label="Alice")
    carol_clip = VoiceClip(
        id=new_id("voice"), source_path=str(tmp_path / "gone.wav"), label="Carol"
    )  # file absent

    narrator = Speaker(
        id=new_id("spk"),
        name="narrator",
        role=SpeakerRole.NARRATOR,
        voice_clip_id=narrator_clip.id,
    )
    alice = Speaker(
        id=new_id("spk"),
        name="Alice",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=alice_clip.id,
        category=VoiceCategory.WOMAN,
    )
    bob = Speaker(
        id=new_id("spk"),
        name="Bob",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=None,
        category=VoiceCategory.MAN,
    )
    carol = Speaker(
        id=new_id("spk"),
        name="Carol",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=carol_clip.id,
        category=VoiceCategory.WOMAN,
    )

    ch_id = new_id("ch")

    def _seg(text: str, sp: Speaker) -> Segment:
        return Segment(
            id=new_id("seg"),
            text=text,
            speaker_id=sp.id,
            role=sp.role,
            confidence=1.0,
            review_status=ReviewStatus.APPROVED,
        )

    line = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text="mixed",
        segments=[
            _seg("Narration.", narrator),
            _seg('"Hi," said Alice.', alice),
            _seg('"Yo," said Bob.', bob),
            _seg('"Hello," said Carol.', carol),
        ],
    )
    chapter = Chapter(id=ch_id, order=0, title="One", lines=[line])
    book = Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter])
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Voices",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        speakers=[narrator, alice, bob, carol],
        voice_clips=[narrator_clip, alice_clip, carol_clip],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
            str(StageName.REVIEW): ReviewStatus.COMPLETED,
        },
    )
    tmp_workspace.save(project)
    return project


def _presenter(store: WorkspaceStore) -> tuple[VoicePresenter, FakeVoiceView, _Recorder]:
    view = FakeVoiceView()
    reviewed = _Recorder()
    presenter = VoicePresenter(view=view, config=AppConfig(), on_reviewed=reviewed)
    presenter.attach(ReviewService(store, store.load()))
    return presenter, view, reviewed


def _speaker_id(project: Project, name: str) -> str:
    return next(sp.id for sp in project.speakers if sp.name == name)


# --------------------------------------------------------------------------- #
# structural conformance / open
# --------------------------------------------------------------------------- #
def test_structural_protocol_conformance() -> None:
    assert isinstance(FakeVoiceView(), VoiceView)


def test_attach_calls_stop_playback(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    _, view, _ = _presenter(tmp_workspace)
    assert view.stopped == 1  # project switch resets the player


def test_open_renders_rows_options_and_needs_voice(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, _ = _presenter(tmp_workspace)

    presenter.open()

    # Default filter is needs-voice-only: Bob (unvoiced) + Carol (file missing) show.
    assert {r.name for r in view.speakers[-1]} == {"Bob", "Carol"}
    # 2 of 4 referenced speakers still need a voice.
    assert view.needs_voice[-1] == (2, 4)
    assert [opt.value for opt in view.options[-1]] == list(VoiceCategory)


# --------------------------------------------------------------------------- #
# assign / unassign / set_category
# --------------------------------------------------------------------------- #
def test_assign_voices_speaker_decrements_count_and_notifies(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    project = _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    bob_id = _speaker_id(presenter._service.project, "Bob")  # type: ignore[union-attr]

    presenter.assign(bob_id, _wav(tmp_path / "bob.wav"))

    live = presenter._service.project  # type: ignore[union-attr]
    bob = next(sp for sp in live.speakers if sp.name == "Bob")
    assert bob.voice_clip_id is not None
    assert bob.id not in {sp.id for sp in unresolved_speakers(live)}
    assert view.needs_voice[-1] == (1, 4)  # decremented
    assert reviewed.count == 1
    assert len(view.speakers) >= 2  # rows re-pushed
    # Persistence: a fresh load reflects the assignment.
    reloaded_bob = next(sp for sp in tmp_workspace.load().speakers if sp.name == "Bob")
    assert reloaded_bob.voice_clip_id is not None
    assert project is not None


def test_assign_missing_file_errors_without_mutation_or_notify(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    bob_id = _speaker_id(presenter._service.project, "Bob")  # type: ignore[union-attr]

    presenter.assign(bob_id, str(tmp_path / "does-not-exist.wav"))

    assert view.errors
    assert reviewed.count == 0
    bob = next(sp for sp in presenter._service.project.speakers if sp.name == "Bob")  # type: ignore[union-attr]
    assert bob.voice_clip_id is None


def test_unassign_reopens_gate_and_increments_count(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    alice_id = _speaker_id(presenter._service.project, "Alice")  # type: ignore[union-attr]

    presenter.unassign(alice_id)

    live = presenter._service.project  # type: ignore[union-attr]
    alice = next(sp for sp in live.speakers if sp.name == "Alice")
    assert alice.voice_clip_id is None
    assert view.needs_voice[-1] == (3, 4)  # Bob + Carol + now Alice
    assert str(StageName.REVIEW) not in live.stage_status  # gate re-opened
    assert reviewed.count == 1


def test_set_category_persists_and_notifies_without_gate_change(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    bob_id = _speaker_id(presenter._service.project, "Bob")  # type: ignore[union-attr]

    presenter.set_category(bob_id, VoiceCategory.WOMAN)

    bob = next(sp for sp in presenter._service.project.speakers if sp.name == "Bob")  # type: ignore[union-attr]
    assert bob.category == VoiceCategory.WOMAN
    assert reviewed.count == 1
    # category does not touch the gate: REVIEW stays COMPLETED.
    assert str(StageName.REVIEW) in presenter._service.project.stage_status  # type: ignore[union-attr]
    assert next(s for s in tmp_workspace.load().speakers if s.name == "Bob").category == (
        VoiceCategory.WOMAN
    )


def test_edit_unknown_speaker_id_errors(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()

    presenter.assign("spk_missing", _wav(tmp_path / "x.wav"))

    assert view.errors
    assert reviewed.count == 0


# --------------------------------------------------------------------------- #
# bulk assign by category
# --------------------------------------------------------------------------- #
def test_bulk_assign_targets_exactly_unresolved_of_each_category(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    live = presenter._service.project  # type: ignore[union-attr]
    before_unresolved = {sp.id for sp in unresolved_speakers(live)}  # Bob (man) + Carol (woman)

    man_clip = _wav(tmp_path / "man.wav")
    woman_clip = _wav(tmp_path / "woman.wav")
    overrides = {"man": man_clip, "woman": woman_clip, "boy": None, "girl": None, "default": None}

    presenter.bulk_assign_by_category(overrides)

    assert unresolved_speakers(live) == []  # all covered
    bob = next(sp for sp in live.speakers if sp.name == "Bob")
    carol = next(sp for sp in live.speakers if sp.name == "Carol")
    assert {bob.id, carol.id} == before_unresolved  # exactly the unresolved set was voiced
    assert reviewed.count == 1
    assert not view.errors


def test_bulk_assign_uncovered_category_errors_and_assigns_nothing(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    live = presenter._service.project  # type: ignore[union-attr]

    # Only man covered; Carol (woman) is uncovered.
    overrides = {
        "man": _wav(tmp_path / "m.wav"),
        "woman": None,
        "boy": None,
        "girl": None,
        "default": None,
    }
    presenter.bulk_assign_by_category(overrides)

    assert view.errors
    assert reviewed.count == 0
    assert {sp.name for sp in unresolved_speakers(live)} == {"Bob", "Carol"}  # nothing assigned


# --------------------------------------------------------------------------- #
# audition (no audio device)
# --------------------------------------------------------------------------- #
def test_audition_voiced_speaker_plays_its_clip_without_notify(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()
    alice_id = _speaker_id(presenter._service.project, "Alice")  # type: ignore[union-attr]

    presenter.audition_speaker(alice_id)

    assert view.played == [str(fake_voice_clips[1])]  # Alice's assigned clip
    assert reviewed.count == 0  # read-only: no shell tick


def test_audition_unvoiced_speaker_errors_without_playing(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, _ = _presenter(tmp_workspace)
    presenter.open()
    bob_id = _speaker_id(presenter._service.project, "Bob")  # type: ignore[union-attr]

    presenter.audition_speaker(bob_id)

    assert view.errors
    assert view.played == []


def test_audition_missing_file_speaker_errors_without_playing(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, _ = _presenter(tmp_workspace)
    presenter.open()
    carol_id = _speaker_id(presenter._service.project, "Carol")  # type: ignore[union-attr]

    presenter.audition_speaker(carol_id)  # clip id set, file absent

    assert view.errors
    assert view.played == []


def test_audition_path_plays_existing_and_errors_on_missing(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, reviewed = _presenter(tmp_workspace)
    presenter.open()

    good = _wav(tmp_path / "pick.wav")
    presenter.audition_path(good)
    assert view.played == [good]

    presenter.audition_path(str(tmp_path / "missing.wav"))
    assert view.errors
    assert view.played == [good]  # the missing one did not play
    assert reviewed.count == 0  # audition never ticks the shell


# --------------------------------------------------------------------------- #
# filter
# --------------------------------------------------------------------------- #
def test_filter_toggles_between_needs_voice_only_and_all(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    _project(tmp_workspace, fake_voice_clips, tmp_path)
    presenter, view, _ = _presenter(tmp_workspace)
    presenter.open()
    assert {r.name for r in view.speakers[-1]} == {"Bob", "Carol"}  # needs-voice default

    presenter.set_filter(False)
    assert len(view.speakers[-1]) == 4  # all speakers

    presenter.set_filter(True)
    assert {r.name for r in view.speakers[-1]} == {"Bob", "Carol"}

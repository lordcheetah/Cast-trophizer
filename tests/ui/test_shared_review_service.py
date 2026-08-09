"""One shared :class:`ReviewService` makes cross-panel clobber impossible by construction.

Slices 2/3 gave each review panel its own ``ReviewService`` over an independent full-project
snapshot; because ``WorkspaceStore.save`` writes the **whole** project, panel B's save could
erase panel A's edit, and slice 3 papered over it by reloading the entering panel from disk on
navigation. Slice 4 collapses those snapshots into a **single** service owned by
``ProjectPresenter`` and shared by reference with all three panels: one in-memory ``Project``,
one save path, nothing to diverge. These tests pin that — both panels' edits coexist on disk
with **no re-attach**, and every panel holds the *same* service object.
"""

from __future__ import annotations

import wave
from pathlib import Path

from casttrophizer.ui.suggestion_presenter import SuggestionPresenter

from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
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
from casttrophizer.ui.attribution_presenter import AttributionPresenter
from casttrophizer.ui.voice_presenter import VoicePresenter
from casttrophizer.workspace.store import WorkspaceStore


def _wav(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 128)
    return str(path)


def _project(store: WorkspaceStore, clip: Path) -> tuple[str, str]:
    """Saved project with a voiced narrator, an UNVOICED Bob, and a NEEDS_REVIEW quote to Bob.

    Returns ``(bob_id, quote_segment_id)`` so the tests can drive each panel at the right item.
    """
    narrator_clip = VoiceClip(id=new_id("voice"), source_path=str(clip), label="Narrator")
    narrator = Speaker(
        id=new_id("spk"),
        name="narrator",
        role=SpeakerRole.NARRATOR,
        voice_clip_id=narrator_clip.id,
    )
    bob = Speaker(id=new_id("spk"), name="Bob", role=SpeakerRole.CHARACTER, voice_clip_id=None)

    ch_id = new_id("ch")
    narration = Segment(
        id=new_id("seg"),
        text="Narration.",
        speaker_id=narrator.id,
        role=SpeakerRole.NARRATOR,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )
    quote = Segment(
        id=new_id("seg"),
        text='"Yo," said Bob.',
        speaker_id=bob.id,
        role=SpeakerRole.CHARACTER,
        confidence=0.2,
        review_status=ReviewStatus.NEEDS_REVIEW,
    )
    line = Line(
        id=new_id("line"), chapter_id=ch_id, order=0, text="mixed", segments=[narration, quote]
    )
    chapter = Chapter(id=ch_id, order=0, title="One", lines=[line])
    book = Book(title="t", author="a", source_ebook_path="/nowhere.epub", chapters=[chapter])
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Both",
        workspace_dir=str(store.layout.root),
        book=book,
        speakers=[narrator, bob],
        voice_clips=[narrator_clip],
        stage_status={str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED},
    )
    store.save(project)
    return bob.id, quote.id


def _voiced(project: Project, speaker_id: str) -> bool:
    return next(sp for sp in project.speakers if sp.id == speaker_id).voice_clip_id is not None


def _approved(project: Project, segment_id: str) -> bool:
    seg = next(
        s
        for ch in project.book.chapters
        for ln in ch.lines
        for s in ln.segments
        if s.id == segment_id
    )
    return seg.review_status == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# minimal null views (these tests assert persistence, not what's pushed at the view)
# --------------------------------------------------------------------------- #
class _NullAttributionView:
    def show_segments(self, rows: object) -> None: ...
    def show_speaker_options(self, options: object) -> None: ...
    def show_progress(self, needs_review: int, total: int) -> None: ...
    def select_segment(self, index: int) -> None: ...
    def show_error(self, title: str, message: str) -> None: ...


class _NullVoiceView:
    def show_speakers(self, rows: object) -> None: ...
    def show_category_options(self, options: object) -> None: ...
    def show_needs_voice(self, needs_voice: int, referenced_total: int) -> None: ...
    def select_speaker(self, index: int) -> None: ...
    def play_clip(self, path: str) -> None: ...
    def stop_playback(self) -> None: ...
    def show_error(self, title: str, message: str) -> None: ...


class _NullSuggestionView:
    def show_suggestions(self, rows: object) -> None: ...
    def show_progress(self, pending: int, total: int) -> None: ...
    def select_suggestion(self, index: int) -> None: ...
    def show_error(self, title: str, message: str) -> None: ...


def _panels(
    service: ReviewService,
) -> tuple[AttributionPresenter, VoicePresenter, SuggestionPresenter]:
    """All three panel presenters attached to the ONE shared service (as ``ui/app.py`` does)."""
    attribution = AttributionPresenter(view=_NullAttributionView(), on_reviewed=lambda: None)
    voice = VoicePresenter(view=_NullVoiceView(), config=AppConfig(), on_reviewed=lambda: None)
    suggestion = SuggestionPresenter(view=_NullSuggestionView(), on_reviewed=lambda: None)
    attribution.attach(service)
    voice.attach(service)
    suggestion.attach(service)
    return attribution, voice, suggestion


def test_single_shared_service_makes_cross_panel_clobber_impossible(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    """Voice-assign in one panel then approve in another — with NO re-attach — both survive.

    This is the state that clobbered under the old per-panel snapshots: the attribution panel
    attached first (Bob unvoiced), then the voice panel voiced Bob, then the attribution panel
    saved. Sharing one service means both panels mutate the *same* ``Project`` object, so the
    second save carries the first edit — clobber is impossible by construction.
    """
    bob_id, quote_id = _project(tmp_workspace, fake_voice_clips[0])
    service = ReviewService(tmp_workspace, tmp_workspace.load())
    attribution, voice, _ = _panels(service)

    voice.assign(bob_id, _wav(tmp_path / "bob.wav"))
    attribution.approve(quote_id)  # no re-attach between the two edits

    final = tmp_workspace.load()
    assert _voiced(final, bob_id), "voice assignment was clobbered by the attribution save"
    assert _approved(final, quote_id), "the approval did not persist"


def test_all_panels_share_the_same_service_object(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path]
) -> None:
    """The three panels hold the *same* service instance — one Project, by reference."""
    _project(tmp_workspace, fake_voice_clips[0])
    service = ReviewService(tmp_workspace, tmp_workspace.load())
    attribution, voice, suggestion = _panels(service)

    assert attribution._service is voice._service
    assert voice._service is suggestion._service
    assert attribution._service is service
    assert attribution._service.project is voice._service.project

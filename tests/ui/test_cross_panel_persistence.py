"""Regression: the two review presenters must not clobber each other's saved edits.

Slice 3 added a *second* editing presenter (voice) alongside the attribution presenter.
Each holds its own :class:`~casttrophizer.review.service.ReviewService` over a full-project
snapshot, and ``WorkspaceStore.save`` writes the **whole** project — so a panel editing a
snapshot taken before the *other* panel's save would silently erase that save.

``ui/app.py`` fixes this by re-attaching the panel you navigate INTO from disk (see
``_open_review_page`` / ``_open_voice_page``), turning navigation into a reload. These tests
pin that behavior at the presenter level: re-attach → both edits survive; skip the re-attach
→ the stale whole-project write clobbers the other panel's edit (documents *why* the fix
exists, and fails if someone removes the re-attach).
"""

from __future__ import annotations

import wave
from pathlib import Path

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


def test_reattach_on_navigation_preserves_both_panels_edits(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    """Voice-assign, then re-attach the attribution panel (as navigation does) and approve.

    Both edits must survive on a fresh load — this is the ``ui/app.py`` re-attach-on-entry path.
    """
    bob_id, quote_id = _project(tmp_workspace, fake_voice_clips[0])

    voice = VoicePresenter(view=_NullVoiceView(), config=AppConfig(), on_reviewed=lambda: None)
    voice.attach(tmp_workspace)
    voice.assign(bob_id, _wav(tmp_path / "bob.wav"))

    attribution = AttributionPresenter(view=_NullAttributionView(), on_reviewed=lambda: None)
    attribution.attach(tmp_workspace)  # navigation re-attaches from disk -> sees Bob's voice
    attribution.approve(quote_id)

    final = tmp_workspace.load()
    assert _voiced(final, bob_id), "voice assignment was clobbered by the attribution save"
    assert _approved(final, quote_id), "the approval did not persist"


def test_stale_snapshot_without_reattach_clobbers_the_other_edit(
    tmp_workspace: WorkspaceStore, fake_voice_clips: list[Path], tmp_path: Path
) -> None:
    """Documents the bug the re-attach prevents: an un-reloaded panel erases the other's save."""
    bob_id, quote_id = _project(tmp_workspace, fake_voice_clips[0])

    # Attribution attaches FIRST (its snapshot has Bob unvoiced), before the voice edit.
    attribution = AttributionPresenter(view=_NullAttributionView(), on_reviewed=lambda: None)
    attribution.attach(tmp_workspace)

    voice = VoicePresenter(view=_NullVoiceView(), config=AppConfig(), on_reviewed=lambda: None)
    voice.attach(tmp_workspace)
    voice.assign(bob_id, _wav(tmp_path / "bob.wav"))  # Bob voiced on disk

    # No re-attach: approving writes the stale whole-project snapshot back, erasing Bob's voice.
    attribution.approve(quote_id)

    final = tmp_workspace.load()
    assert _approved(final, quote_id)
    assert not _voiced(final, bob_id), "expected the stale write to clobber the voice assignment"


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

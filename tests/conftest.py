"""Shared pytest fixtures: workspace, sample epub, voice clips, a saved project, fakes.

Everything here is offline and deterministic — no network, no real TTS/LLM, no ffmpeg.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName
from casttrophizer.domain.ids import new_id
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
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.workspace.layout import WorkspaceLayout
from casttrophizer.workspace.store import WorkspaceStore
from tests.data.make_sample_epub import make_sample_epub
from tests.fakes import FakeLLMProvider, FakeTTSProvider, RecordingProgressReporter


# --------------------------------------------------------------------------- #
# workspace
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_workspace(tmp_path: Path) -> WorkspaceStore:
    """An initialized workspace (layout + store) rooted in a temp directory."""
    layout = WorkspaceLayout.for_dir(tmp_path / "workspace")
    layout.ensure_dirs()
    return WorkspaceStore(layout)


# --------------------------------------------------------------------------- #
# inputs (read-only)
# --------------------------------------------------------------------------- #
@pytest.fixture
def sample_epub(tmp_path: Path) -> Path:
    """A tiny generated EPUB (2 chapters, a few quoted lines)."""
    return make_sample_epub(tmp_path / "inputs" / "sample.epub")


def _write_silent_wav(path: Path, *, seconds: float = 0.05, rate: int = 22050) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


@pytest.fixture
def fake_voice_clips(tmp_path: Path) -> list[Path]:
    """Two tiny generated WAVs that stand in for read-only voice inputs."""
    base = tmp_path / "inputs" / "voices"
    return [
        _write_silent_wav(base / "narrator.wav"),
        _write_silent_wav(base / "alice.wav"),
    ]


# --------------------------------------------------------------------------- #
# a minimal saved project
# --------------------------------------------------------------------------- #
@pytest.fixture
def sample_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    fake_voice_clips: list[Path],
) -> Project:
    """A minimal :class:`Project` saved to disk in ``tmp_workspace``.

    Built by hand (not via the parse stage, which is a stub) so store/serialization
    tests have a realistic, fully-populated object to round-trip.
    """
    narrator_clip = VoiceClip(
        id=new_id("voice"), source_path=str(fake_voice_clips[0]), label="Narrator"
    )
    alice_clip = VoiceClip(id=new_id("voice"), source_path=str(fake_voice_clips[1]), label="Alice")

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
    )

    chapter_id = new_id("ch")
    line_id = new_id("line")
    line = Line(
        id=line_id,
        chapter_id=chapter_id,
        order=0,
        text='The narrator spoke. "Hello there," said Alice.',
        segments=[
            Segment(
                id=new_id("seg"),
                text="The narrator spoke.",
                speaker_id=narrator.id,
                role=SpeakerRole.NARRATOR,
                confidence=0.99,
                review_status=ReviewStatus.APPROVED,
            ),
            Segment(
                id=new_id("seg"),
                text='"Hello there," said Alice.',
                speaker_id=alice.id,
                role=SpeakerRole.CHARACTER,
                confidence=0.4,
                review_status=ReviewStatus.NEEDS_REVIEW,
            ),
        ],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="narrarator",
                suggested="narrator",
                reason="spellcheck",
                confidence=0.97,
                status=ReviewStatus.AUTO_APPLIED,
            )
        ],
    )
    chapter = Chapter(id=chapter_id, order=0, title="Chapter One", lines=[line])

    book = Book(
        title="A Sample Tale",
        author="Test Author",
        source_ebook_path=str(sample_epub),
        cover_image_path=None,
        chapters=[chapter],
    )

    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Sample Project",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        speakers=[narrator, alice],
        voice_clips=[narrator_clip, alice_clip],
        stage_status={str(StageName.PARSE): ReviewStatus.COMPLETED},
        tts_params={"exaggeration": 0.5, "seed": 7},
    )
    tmp_workspace.save(project)
    return project


# --------------------------------------------------------------------------- #
# a project ready to be parsed (no chapters yet)
# --------------------------------------------------------------------------- #
@pytest.fixture
def parse_ready_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
) -> Project:
    """A saved :class:`Project` pointing at ``sample_epub`` with no chapters parsed yet.

    Mirrors the state the parse stage receives: ``book`` has the read-only source path but
    empty chapters/metadata, and ``stage_status`` is empty (parse not yet run).
    """
    book = Book(
        title="",
        author="",
        source_ebook_path=str(sample_epub),
        cover_image_path=None,
        chapters=[],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Parse Ready",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        stage_status={},
    )
    tmp_workspace.save(project)
    return project


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_llm() -> FakeLLMProvider:
    """A deterministic offline LLM provider."""
    return FakeLLMProvider()


@pytest.fixture
def fake_tts() -> FakeTTSProvider:
    """A deterministic offline TTS provider that writes silent WAVs."""
    return FakeTTSProvider()


@pytest.fixture
def recording_progress() -> RecordingProgressReporter:
    """A progress reporter that records calls and supports a controllable stop flag."""
    return RecordingProgressReporter()

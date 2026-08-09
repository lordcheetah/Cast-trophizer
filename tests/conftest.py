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
from casttrophizer.workspace.audio_cache import AudioCache
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
# a project ready to be corrected (parsed, planted errors)
# --------------------------------------------------------------------------- #
@pytest.fixture
def correct_ready_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
) -> Project:
    """A saved, parsed :class:`Project` with planted text errors for the correct stage.

    ``stage_status[PARSE]=COMPLETED`` and a handful of hand-built lines carry exact, known
    defects so correction assertions are deterministic:

    * a double-space + space-before-comma line (auto-fixable whitespace);
    * a misspelling (``narrarator``) that must surface as PENDING, text unchanged;
    * a protected character name (``Aelin``, appears twice so the proper-noun heuristic
      seeds it) that must yield no suggestion;
    * a clean line that yields no suggestion.

    The misspelling/known-word judgements are driven by a fake spellchecker in the tests,
    never the real dictionary.
    """
    ch_id = new_id("ch")

    def _line(order: int, text: str) -> Line:
        return Line(id=new_id("line"), chapter_id=ch_id, order=order, text=text, segments=[])

    lines = [
        _line(0, "He  said , hello"),  # double space + space-before-comma -> AUTO
        _line(1, "The narrarator spoke."),  # misspelling -> PENDING
        _line(2, "Aelin drew her blade."),  # protected name (capitalized, recurs) -> none
        _line(3, "Aelin smiled."),  # second Aelin so the >=2 heuristic seeds it
        _line(4, "The quiet hall was empty."),  # clean -> none
        _line(5, ""),  # empty/heading -> no-op
    ]
    chapter = Chapter(id=ch_id, order=0, title="Chapter One", lines=lines)

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
        name="Correct Ready",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        stage_status={str(StageName.PARSE): ReviewStatus.COMPLETED},
    )
    tmp_workspace.save(project)
    return project


# --------------------------------------------------------------------------- #
# a project ready to be attributed (parsed + corrected, no segments yet)
# --------------------------------------------------------------------------- #
@pytest.fixture
def attribute_ready_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
) -> Project:
    """A saved, parsed+corrected :class:`Project` whose lines carry known dialogue.

    ``stage_status[PARSE]=COMPLETED`` and ``[CORRECT]=COMPLETED``; ``Line.segments`` is
    empty everywhere (segmentation/attribution is this stage's job). Two chapters with
    hand-built lines so attribution assertions are exact:

    * a narrator-only line (no quotes);
    * ``"Hello," said Alice.`` (narration + a quote);
    * ``"Hi," Bob replied.`` (narration + a quote);
    * an empty line (yields zero segments);
    * a second chapter with one more quote line (for stop/resume + batching tests).

    ``project.speakers`` starts empty so the narrator-auto-creation path is exercised.
    """

    def _line(ch_id: str, order: int, text: str) -> Line:
        return Line(id=new_id("line"), chapter_id=ch_id, order=order, text=text, segments=[])

    c1 = new_id("ch")
    ch1_lines = [
        _line(c1, 0, "The hall was silent."),  # narrator-only
        _line(c1, 1, '"Hello," said Alice.'),  # narration + quote
        _line(c1, 2, '"Hi," Bob replied.'),  # narration + quote
        _line(c1, 3, ""),  # empty -> zero segments
    ]
    c2 = new_id("ch")
    ch2_lines = [
        _line(c2, 0, '"We meet again," said Alice.'),  # quote reusing Alice
    ]
    chapters = [
        Chapter(id=c1, order=0, title="Chapter One", lines=ch1_lines),
        Chapter(id=c2, order=1, title="Chapter Two", lines=ch2_lines),
    ]

    book = Book(
        title="A Sample Tale",
        author="Test Author",
        source_ebook_path=str(sample_epub),
        cover_image_path=None,
        chapters=chapters,
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Attribute Ready",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
        },
    )
    tmp_workspace.save(project)
    return project


# --------------------------------------------------------------------------- #
# a project ready for review (attributed; mixed blocker state)
# --------------------------------------------------------------------------- #
@pytest.fixture
def review_ready_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    fake_voice_clips: list[Path],
) -> Project:
    """A saved, attributed :class:`Project` carrying mixed review-blocker state.

    ``stage_status[PARSE/CORRECT/ATTRIBUTE]=COMPLETED``. It deliberately carries one of
    each blocker plus already-resolved items so the gate's "resolved doesn't block" path
    is also exercised:

    * an APPROVED narrator segment + a NEEDS_REVIEW Bob segment (attribution blocker);
    * a line with an AUTO_APPLIED suggestion (resolved) and a PENDING suggestion (blocker);
    * the narrator + Alice have assigned, existing voice clips; **Bob has none** (voice
      blocker — and Bob is referenced by a renderable segment).

    Clearing all three (approve Bob's segment, resolve the pending suggestion, assign Bob a
    voice) makes ``review_blockers`` empty.
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
    bob = Speaker(
        id=new_id("spk"),
        name="Bob",
        role=SpeakerRole.CHARACTER,
        voice_clip_id=None,  # voice blocker
    )

    ch_id = new_id("ch")

    line0 = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=0,
        text='"Hello," said Alice.',
        segments=[
            Segment(
                id=new_id("seg"),
                text='"Hello,"',
                speaker_id=alice.id,
                role=SpeakerRole.CHARACTER,
                confidence=0.95,
                review_status=ReviewStatus.APPROVED,
            ),
            Segment(
                id=new_id("seg"),
                text="said Alice.",
                speaker_id=narrator.id,
                role=SpeakerRole.NARRATOR,
                confidence=0.99,
                review_status=ReviewStatus.APPROVED,
            ),
        ],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="Allice",
                suggested="Alice",
                reason="spellcheck",
                confidence=0.97,
                status=ReviewStatus.AUTO_APPLIED,  # resolved -> does not block
            )
        ],
    )
    line1 = Line(
        id=new_id("line"),
        chapter_id=ch_id,
        order=1,
        text='"Hi," Bob replied.',
        segments=[
            Segment(
                id=new_id("seg"),
                text='"Hi,"',
                speaker_id=bob.id,
                role=SpeakerRole.CHARACTER,
                confidence=0.4,
                review_status=ReviewStatus.NEEDS_REVIEW,  # attribution blocker
            ),
            Segment(
                id=new_id("seg"),
                text="Bob replied.",
                speaker_id=narrator.id,
                role=SpeakerRole.NARRATOR,
                confidence=0.99,
                review_status=ReviewStatus.APPROVED,
            ),
        ],
        suggestions=[
            TextSuggestion(
                id=new_id("sug"),
                original="Bbo",
                suggested="Bob",
                reason="spellcheck",
                confidence=0.6,
                status=ReviewStatus.PENDING,  # suggestion blocker
            )
        ],
    )
    chapter = Chapter(id=ch_id, order=0, title="Chapter One", lines=[line0, line1])

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
        name="Review Ready",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        speakers=[narrator, alice, bob],
        voice_clips=[narrator_clip, alice_clip],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
        },
        tts_params={"exaggeration": 0.5, "seed": 7},
    )
    tmp_workspace.save(project)
    return project


# --------------------------------------------------------------------------- #
# a project ready to be synthesized (attributed, voices assigned)
# --------------------------------------------------------------------------- #
def _seg(text: str, speaker: Speaker, role: SpeakerRole) -> Segment:
    """An already-attributed, not-yet-synthesized segment (audio_status PENDING)."""
    return Segment(
        id=new_id("seg"),
        text=text,
        speaker_id=speaker.id,
        role=role,
        confidence=1.0,
        review_status=ReviewStatus.APPROVED,
    )


def _build_synthesize_project(
    workspace: WorkspaceStore,
    sample_epub: Path,
    voice_clips: list[Path],
    *,
    assign_alice_voice: bool,
) -> Project:
    """Build a saved, attributed project with (optionally) all voices assigned.

    Two chapters of pre-segmented lines (narration + character quotes) so synthesize tests
    have a realistic full-cast object. The narrator and Alice both get assigned voice clips
    from ``fake_voice_clips`` unless ``assign_alice_voice`` is False (the unassigned-voice
    precondition variant). ``stage_status`` has PARSE/CORRECT/ATTRIBUTE COMPLETED.
    """
    narrator_clip = VoiceClip(id=new_id("voice"), source_path=str(voice_clips[0]), label="Narrator")
    alice_clip = VoiceClip(id=new_id("voice"), source_path=str(voice_clips[1]), label="Alice")

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
        voice_clip_id=alice_clip.id if assign_alice_voice else None,
    )

    c1 = new_id("ch")
    c2 = new_id("ch")

    def _line(ch_id: str, order: int, text: str, segments: list[Segment]) -> Line:
        return Line(id=new_id("line"), chapter_id=ch_id, order=order, text=text, segments=segments)

    ch1 = Chapter(
        id=c1,
        order=0,
        title="Chapter One",
        lines=[
            _line(
                c1,
                0,
                "The hall was silent.",
                [_seg("The hall was silent.", narrator, SpeakerRole.NARRATOR)],
            ),
            _line(
                c1,
                1,
                '"Hello," said Alice.',
                [
                    _seg('"Hello,"', alice, SpeakerRole.CHARACTER),
                    _seg("said Alice.", narrator, SpeakerRole.NARRATOR),
                ],
            ),
            _line(c1, 2, "   ", [_seg("   ", narrator, SpeakerRole.NARRATOR)]),  # whitespace-only
        ],
    )
    ch2 = Chapter(
        id=c2,
        order=1,
        title="Chapter Two",
        lines=[
            _line(
                c2,
                0,
                '"We meet again," said Alice.',
                [
                    _seg('"We meet again,"', alice, SpeakerRole.CHARACTER),
                    _seg("said Alice.", narrator, SpeakerRole.NARRATOR),
                ],
            ),
        ],
    )

    book = Book(
        title="A Sample Tale",
        author="Test Author",
        source_ebook_path=str(sample_epub),
        cover_image_path=None,
        chapters=[ch1, ch2],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Synthesize Ready",
        workspace_dir=str(workspace.layout.root),
        book=book,
        speakers=[narrator, alice],
        voice_clips=[narrator_clip, alice_clip],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
        },
        tts_params={"exaggeration": 0.5, "seed": 7},
    )
    workspace.save(project)
    return project


@pytest.fixture
def synthesize_ready_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    fake_voice_clips: list[Path],
) -> Project:
    """A saved, fully-attributed project with every referenced speaker's voice assigned.

    Two chapters of narration + Alice quotes (plus one whitespace-only segment), all speakers
    pointing at existing ``fake_voice_clips``, ``tts_params`` set, and all attribution/correct
    stages COMPLETED — the exact state the synthesize stage expects.
    """
    return _build_synthesize_project(
        tmp_workspace, sample_epub, fake_voice_clips, assign_alice_voice=True
    )


@pytest.fixture
def synthesize_unassigned_voice_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    fake_voice_clips: list[Path],
) -> Project:
    """Same as ``synthesize_ready_project`` but Alice has no assigned voice clip.

    Exercises the §2b fail-fast precheck: the stage must FAIL naming the unresolved speaker
    before any TTS call.
    """
    return _build_synthesize_project(
        tmp_workspace, sample_epub, fake_voice_clips, assign_alice_voice=False
    )


# --------------------------------------------------------------------------- #
# a project ready to be assembled (synthesized: every renderable segment has a WAV)
# --------------------------------------------------------------------------- #
def _plant_segment_audio(
    project: Project,
    cache: AudioCache,
    *,
    durations: dict[str, float],
) -> None:
    """Simulate a completed synthesize: for each renderable segment compute its cache key,
    write a real silent WAV of a known duration at ``cache.path_for_key(key)``, and stamp
    ``audio_cache_key`` + ``audio_status=COMPLETED``. ``durations`` maps segment text ->
    seconds so timing assertions are exact. Whitespace-only segments are left untouched
    (no key, PENDING) to prove they are skipped.
    """
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                if not segment.text.strip():
                    continue
                key = AudioCache.key_for(segment, project)
                _write_silent_wav(cache.path_for_key(key), seconds=durations[segment.text])
                segment.audio_cache_key = key
                segment.audio_status = ReviewStatus.COMPLETED


def _build_assemble_project(
    workspace: WorkspaceStore,
    sample_epub: Path,
    voice_clips: list[Path],
    *,
    cover_path: str | None = None,
) -> Project:
    """A saved, fully-synthesized project ready for assemble.

    Three chapters: two with renderable content (narration + Alice quotes, plus a
    whitespace-only segment that must be skipped) and one empty chapter (zero renderable
    segments -> a zero-length marker). Segment texts are distinct so each renderable segment
    gets its own known duration for exact chapter-timing assertions.
    """
    narrator_clip = VoiceClip(id=new_id("voice"), source_path=str(voice_clips[0]), label="Narrator")
    alice_clip = VoiceClip(id=new_id("voice"), source_path=str(voice_clips[1]), label="Alice")

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

    def _seg2(text: str, speaker: Speaker, role: SpeakerRole) -> Segment:
        return Segment(
            id=new_id("seg"),
            text=text,
            speaker_id=speaker.id,
            role=role,
            confidence=1.0,
            review_status=ReviewStatus.APPROVED,
        )

    def _line(ch_id: str, order: int, text: str, segments: list[Segment]) -> Line:
        return Line(id=new_id("line"), chapter_id=ch_id, order=order, text=text, segments=segments)

    c1 = new_id("ch")
    c2 = new_id("ch")
    c3 = new_id("ch")

    ch1 = Chapter(
        id=c1,
        order=0,
        title="Chapter One",
        lines=[
            _line(
                c1,
                0,
                "The hall was silent.",
                [_seg2("The hall was silent.", narrator, SpeakerRole.NARRATOR)],
            ),
            _line(
                c1,
                1,
                '"Hello," said Alice.',
                [
                    _seg2('"Hello,"', alice, SpeakerRole.CHARACTER),
                    _seg2("said Alice softly.", narrator, SpeakerRole.NARRATOR),
                ],
            ),
            _line(
                c1,
                2,
                "   ",
                [_seg2("   ", narrator, SpeakerRole.NARRATOR)],
            ),  # whitespace-only -> skipped
        ],
    )
    ch2 = Chapter(
        id=c2,
        order=1,
        title="Chapter Two",
        lines=[
            _line(
                c2,
                0,
                '"We meet again," said Alice.',
                [
                    _seg2('"We meet again,"', alice, SpeakerRole.CHARACTER),
                    _seg2("said Alice again.", narrator, SpeakerRole.NARRATOR),
                ],
            ),
        ],
    )
    ch3 = Chapter(id=c3, order=2, title="Chapter Three (empty)", lines=[])

    book = Book(
        title="A Sample Tale",
        author="Test Author",
        source_ebook_path=str(sample_epub),
        cover_image_path=cover_path,
        chapters=[ch1, ch2, ch3],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="Assemble Ready",
        workspace_dir=str(workspace.layout.root),
        book=book,
        speakers=[narrator, alice],
        voice_clips=[narrator_clip, alice_clip],
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
            str(StageName.ATTRIBUTE): ReviewStatus.COMPLETED,
            str(StageName.REVIEW): ReviewStatus.COMPLETED,
            str(StageName.SYNTHESIZE): ReviewStatus.COMPLETED,
        },
        tts_params={"exaggeration": 0.5, "seed": 7},
    )

    _plant_segment_audio(
        project, AudioCache(workspace.layout), durations=ASSEMBLE_SEGMENT_DURATIONS
    )
    workspace.save(project)
    return project


#: The known per-segment WAV durations planted by ``_build_assemble_project`` (seconds), keyed
#: by segment text. Tests import this to compute expected chapter start/end times exactly.
ASSEMBLE_SEGMENT_DURATIONS: dict[str, float] = {
    "The hall was silent.": 0.20,
    '"Hello,"': 0.10,
    "said Alice softly.": 0.30,
    '"We meet again,"': 0.15,
    "said Alice again.": 0.25,
}


@pytest.fixture
def assemble_ready_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    fake_voice_clips: list[Path],
) -> Project:
    """A saved, fully-synthesized project: every renderable segment has an on-disk WAV.

    Three chapters (two with content + a whitespace-only segment, one empty), all upstream
    stages COMPLETED, and each renderable segment stamped ``audio_cache_key`` + COMPLETED with
    a real silent WAV of a known duration in the audio cache — the exact state assemble expects.
    """
    return _build_assemble_project(tmp_workspace, sample_epub, fake_voice_clips)


@pytest.fixture
def assemble_unrendered_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    fake_voice_clips: list[Path],
) -> Project:
    """Same as ``assemble_ready_project`` but one renderable segment is unrendered.

    Its ``audio_cache_key`` is cleared and ``audio_status`` reset to PENDING (and its WAV
    removed), exercising the fail-fast precheck: assemble must FAIL naming the unrendered
    segment before any ffmpeg work.
    """
    project = _build_assemble_project(tmp_workspace, sample_epub, fake_voice_clips)
    cache = AudioCache(tmp_workspace.layout)
    target = next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text == "The hall was silent."
    )
    assert target.audio_cache_key is not None
    cache.path_for_key(target.audio_cache_key).unlink()
    target.audio_cache_key = None
    target.audio_status = ReviewStatus.PENDING
    tmp_workspace.save(project)
    return project


@pytest.fixture
def sample_cover_image(tmp_path: Path) -> Path:
    """A tiny valid PNG standing in for a read-only cover input."""
    # A 1x1 transparent PNG (67 bytes) — enough for mutagen/format-detection tests.
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    path = tmp_path / "inputs" / "cover.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)
    return path


@pytest.fixture
def assemble_with_cover_project(
    tmp_workspace: WorkspaceStore,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    sample_cover_image: Path,
) -> Project:
    """Same as ``assemble_ready_project`` but ``book.cover_image_path`` points at a real PNG.

    Exercises the embed-if-set cover path (the cover file is a read-only input, referenced but
    never copied into the workspace).
    """
    return _build_assemble_project(
        tmp_workspace, sample_epub, fake_voice_clips, cover_path=str(sample_cover_image)
    )


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

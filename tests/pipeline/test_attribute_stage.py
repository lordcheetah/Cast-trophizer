"""SegmentAttributeStage integration tests.

Run against the saved ``attribute_ready_project`` with a real ``WorkspaceStore``; assertions
read the **persisted** project back (proving it was saved, not just mutated in memory). The
LLM is always the offline ``FakeLLMProvider`` injected via ``StageContext.llm`` — no network,
no real model. Covers: completion + persistence; narration-vs-quote segment shapes; the
speaker registry (create/reuse, no dup, voice_clip_id None); threshold-driven
APPROVED/NEEDS_REVIEW; narration never sent to the LLM; the confirmed retry-then-soft-flag
malformed policy; missing/extra returned ids; ctx.llm None / unavailable -> FAILED;
idempotent re-run (zero extra LLM calls); stop/resume; batching at MAX_LINES_PER_BATCH;
empty-line -> zero segments; is_complete / next_stage; Line.text untouched.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.attribution.attribute import MAX_LINES_PER_BATCH
from casttrophizer.domain.enums import ReviewStatus, SpeakerRole, StageName, VoiceCategory
from casttrophizer.domain.ids import new_id
from casttrophizer.domain.models import Book, Chapter, Line, Project, Segment, Speaker
from casttrophizer.domain.serialization import CURRENT_SCHEMA_VERSION
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageContext
from casttrophizer.pipeline.stages.attribute import SegmentAttributeStage
from casttrophizer.workspace.store import WorkspaceStore
from tests.fakes import FakeLLMProvider, RecordingProgressReporter


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ctx(
    store: WorkspaceStore,
    progress: RecordingProgressReporter,
    llm: FakeLLMProvider | None,
) -> StageContext:
    return StageContext(store=store, progress=progress, llm=llm)


def _scripted_llm() -> FakeLLMProvider:
    """A fake that maps the fixture's exact quotes to known speakers/confidences.

    ``"Hello,"`` -> Alice 0.9 (APPROVED); ``"Hi,"`` -> Bob 0.5 (NEEDS_REVIEW);
    ``"We meet again,"`` -> Alice 0.95 (reuses the same Alice speaker, cross-chapter).
    """
    return FakeLLMProvider(
        text_script={
            '"Hello,"': ("Alice", 0.9),
            '"Hi,"': ("Bob", 0.5),
            '"We meet again,"': ("Alice", 0.95),
        }
    )


def _all_segments(project: Project) -> list[Segment]:
    return [seg for ch in project.book.chapters for ln in ch.lines for seg in ln.segments]


def _line(project: Project, ch_idx: int, order: int) -> Line:
    return next(ln for ln in project.book.chapters[ch_idx].lines if ln.order == order)


def _speaker(project: Project, sid: str | None) -> Speaker:
    return next(s for s in project.speakers if s.id == sid)


class StopAfterNProgress(RecordingProgressReporter):
    """Trips ``should_stop`` only after ``n`` advances (stop mid-pass with partial state)."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._n = n

    def should_stop(self) -> bool:
        return len(self.advances) >= self._n


# --------------------------------------------------------------------------- #
# completion + persistence
# --------------------------------------------------------------------------- #
def test_run_completes_and_persists(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    assert result.stage == StageName.ATTRIBUTE
    assert result.status == ReviewStatus.COMPLETED

    reloaded = tmp_workspace.load()
    assert reloaded.stage_status[str(StageName.ATTRIBUTE)] == ReviewStatus.COMPLETED


def test_attributed_segments_survive_reload(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    # A brand-new store proves the segments came off disk, not from a cached object.
    reloaded = WorkspaceStore(tmp_workspace.layout).load()
    hello = _line(reloaded, 0, 1)  # '"Hello," said Alice.'
    assert [s.text for s in hello.segments] == ['"Hello,"', "said Alice."]
    quote = hello.segments[0]
    assert quote.role == SpeakerRole.CHARACTER
    assert _speaker(reloaded, quote.speaker_id).name == "Alice"


# --------------------------------------------------------------------------- #
# segment shapes: narrator-only, narration+quote
# --------------------------------------------------------------------------- #
def test_narrator_only_line_one_approved_narrator_segment(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    reloaded = tmp_workspace.load()
    narr_line = _line(reloaded, 0, 0)  # "The hall was silent."
    assert len(narr_line.segments) == 1
    seg = narr_line.segments[0]
    assert seg.text == "The hall was silent."
    assert seg.role == SpeakerRole.NARRATOR
    assert seg.review_status == ReviewStatus.APPROVED
    assert seg.confidence == 1.0
    # Points at the reserved narrator speaker.
    assert _speaker(reloaded, seg.speaker_id).role == SpeakerRole.NARRATOR


def test_dialogue_line_splits_into_quote_then_narration(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    reloaded = tmp_workspace.load()
    hello = _line(reloaded, 0, 1)  # '"Hello," said Alice.'
    assert [(s.text, s.role) for s in hello.segments] == [
        ('"Hello,"', SpeakerRole.CHARACTER),
        ("said Alice.", SpeakerRole.NARRATOR),
    ]
    quote, narration = hello.segments
    assert quote.confidence == 0.9
    assert quote.review_status == ReviewStatus.APPROVED
    assert narration.review_status == ReviewStatus.APPROVED  # narrator by construction
    assert narration.confidence == 1.0


def test_empty_line_yields_zero_segments(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    reloaded = tmp_workspace.load()
    empty = _line(reloaded, 0, 3)  # ""
    assert empty.segments == []


# --------------------------------------------------------------------------- #
# speaker registry
# --------------------------------------------------------------------------- #
def test_new_names_create_one_character_each_reused_across_chapters(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    reloaded = tmp_workspace.load()
    by_name = {s.name: s for s in reloaded.speakers}
    # Exactly: one narrator + Alice + Bob (Alice said in two chapters -> still ONE speaker).
    assert sorted(by_name) == ["Alice", "Bob", "narrator"]
    assert [s.name for s in reloaded.speakers].count("Alice") == 1

    alice = by_name["Alice"]
    bob = by_name["Bob"]
    assert alice.role == SpeakerRole.CHARACTER and alice.voice_clip_id is None
    assert bob.role == SpeakerRole.CHARACTER and bob.voice_clip_id is None

    # The cross-chapter "We meet again," quote resolves to the SAME Alice id.
    ch2_quote = _line(reloaded, 1, 0).segments[0]
    assert ch2_quote.speaker_id == alice.id


def test_reserved_narrator_created_when_speakers_empty(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    assert attribute_ready_project.speakers == []
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    reloaded = tmp_workspace.load()
    narrators = [s for s in reloaded.speakers if s.role == SpeakerRole.NARRATOR]
    assert len(narrators) == 1
    assert narrators[0].name == "narrator"


# --------------------------------------------------------------------------- #
# threshold policy on persisted segments
# --------------------------------------------------------------------------- #
def test_low_confidence_quote_is_needs_review(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    reloaded = tmp_workspace.load()
    bob_quote = _line(reloaded, 0, 2).segments[0]  # '"Hi,"' -> Bob 0.5
    assert bob_quote.confidence == 0.5
    assert bob_quote.review_status == ReviewStatus.NEEDS_REVIEW
    assert _speaker(reloaded, bob_quote.speaker_id).name == "Bob"


# --------------------------------------------------------------------------- #
# narration is NEVER sent to the LLM (call-capture)
# --------------------------------------------------------------------------- #
def test_narration_never_sent_to_llm(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    llm = _scripted_llm()
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    reloaded = tmp_workspace.load()
    # The candidate ids passed to the LLM, gathered across every call.
    sent_ids = {sid for (_ctx_str, ids, _known) in llm.attribute_calls for sid in ids}
    quote_ids = {s.id for s in _all_segments(reloaded) if s.role == SpeakerRole.CHARACTER}
    narration_ids = {
        s.id
        for s in _all_segments(reloaded)
        if s.role == SpeakerRole.NARRATOR and s.review_status == ReviewStatus.APPROVED
    }
    # Every sent id is a quote (character) segment; no narration id was ever a candidate.
    assert sent_ids == quote_ids
    assert sent_ids.isdisjoint(narration_ids)


# --------------------------------------------------------------------------- #
# malformed -> retry-then-soft-flag (CONFIRMED policy: NOT fail)
# --------------------------------------------------------------------------- #
def test_malformed_output_retries_then_soft_flags_and_completes(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    llm = FakeLLMProvider(raise_malformed=True)
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    # The stage STILL completes (one bad batch must not kill the run).
    assert result.status == ReviewStatus.COMPLETED

    reloaded = tmp_workspace.load()
    assert reloaded.stage_status[str(StageName.ATTRIBUTE)] == ReviewStatus.COMPLETED

    # Every quote segment was soft-flagged: narrator default, confidence 0.0, NEEDS_REVIEW.
    quote_segs = [
        s
        for ln in (_line(reloaded, 0, 1), _line(reloaded, 0, 2), _line(reloaded, 1, 0))
        for s in ln.segments
        if s.text.startswith('"')
    ]
    assert quote_segs  # there are quotes to flag
    for seg in quote_segs:
        assert seg.review_status == ReviewStatus.NEEDS_REVIEW
        assert seg.confidence == 0.0
        assert seg.speaker_id is None
        assert seg.role == SpeakerRole.NARRATOR

    # Retry-ONCE: each chapter's quote-bearing batch was called exactly twice (initial + retry).
    # Two chapters carry quotes -> 4 calls total (2 per chapter).
    assert len(llm.attribute_calls) == 4


def test_missing_returned_id_defaults_to_needs_review_narrator(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    # Build a fake that omits whatever quote ids it is asked about by scripting nothing and
    # then dropping them: omit_ids is keyed by id, so derive ids from a dry first segmentation.
    # Simpler: a fake whose text_script covers only "Hello," leaves "Hi," to the default path,
    # but to test the MISSING-id default we drop every returned id via omit on the fly.
    class OmitAllLLM(FakeLLMProvider):
        def attribute_speakers(self, *, context, candidates, known_speakers):  # type: ignore[override]
            self.attribute_calls.append((context, list(candidates), list(known_speakers)))
            return []  # returns nothing -> covers none of the requested ids

    llm = OmitAllLLM()
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    # Returning no requested id reads as malformed -> retry-then-soft-flag, still COMPLETED.
    assert result.status == ReviewStatus.COMPLETED
    reloaded = tmp_workspace.load()
    bob_quote = _line(reloaded, 0, 2).segments[0]
    assert bob_quote.review_status == ReviewStatus.NEEDS_REVIEW
    assert bob_quote.confidence == 0.0
    assert bob_quote.speaker_id is None


def test_extra_unrequested_id_is_ignored(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    llm = FakeLLMProvider(
        text_script={
            '"Hello,"': ("Alice", 0.9),
            '"Hi,"': ("Bob", 0.9),
            '"We meet again,"': ("Alice", 0.9),
        },
        extra_ids=["seg_ghost_not_requested"],
    )
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    assert result.status == ReviewStatus.COMPLETED
    reloaded = tmp_workspace.load()
    # No "Ghost" speaker leaked in from the unrequested extra id.
    assert all(s.name != "Ghost" for s in reloaded.speakers)
    assert all(s.id != "seg_ghost_not_requested" for s in _all_segments(reloaded))


# --------------------------------------------------------------------------- #
# provider failure paths (FAILED, status unset, re-runnable)
# --------------------------------------------------------------------------- #
def test_llm_none_is_failed_status_unset(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), None)
    )
    assert result.status == ReviewStatus.FAILED
    assert str(StageName.ATTRIBUTE) not in attribute_ready_project.stage_status


def test_provider_unavailable_is_failed(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    result = SegmentAttributeStage().run(
        attribute_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), FakeLLMProvider(available=False)),
    )
    assert result.status == ReviewStatus.FAILED
    assert str(StageName.ATTRIBUTE) not in attribute_ready_project.stage_status


def test_unreachable_provider_midrun_is_failed_partial_saved(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    # A reachability error (not malformed) propagates -> FAILED, status unset, partial persisted.
    llm = FakeLLMProvider(raise_unreachable=True)
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    assert result.status == ReviewStatus.FAILED
    reloaded = tmp_workspace.load()
    assert str(StageName.ATTRIBUTE) not in reloaded.stage_status
    # Re-runnable: a working provider on a fresh run completes.
    result2 = SegmentAttributeStage().run(
        reloaded, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    assert result2.status == ReviewStatus.COMPLETED


# --------------------------------------------------------------------------- #
# idempotency: re-run adds no segments and makes ZERO extra LLM calls
# --------------------------------------------------------------------------- #
def test_rerun_adds_no_segments_and_no_extra_llm_calls(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    stage = SegmentAttributeStage()
    llm = _scripted_llm()
    stage.run(attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm))

    first = tmp_workspace.load()
    first_seg_ids = {
        ln.id: [s.id for s in ln.segments] for ch in first.book.chapters for ln in ch.lines
    }
    calls_after_first = len(llm.attribute_calls)
    speakers_after_first = len(first.speakers)

    # Second run on the persisted (already-attributed) project.
    stage.run(first, _ctx(tmp_workspace, RecordingProgressReporter(), llm))
    second = tmp_workspace.load()
    second_seg_ids = {
        ln.id: [s.id for s in ln.segments] for ch in second.book.chapters for ln in ch.lines
    }

    assert second_seg_ids == first_seg_ids  # identical segments, identical ids -> no dups
    assert len(llm.attribute_calls) == calls_after_first  # ZERO additional attribute calls
    assert len(second.speakers) == speakers_after_first  # registry unchanged


# --------------------------------------------------------------------------- #
# stop / resume
# --------------------------------------------------------------------------- #
def test_stop_after_first_chapter_persists_partial_then_resumes(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    stage = SegmentAttributeStage()
    llm = _scripted_llm()

    # Stop trips after chapter one advances -> chapter two stays unattributed.
    result = stage.run(attribute_ready_project, _ctx(tmp_workspace, StopAfterNProgress(n=1), llm))
    assert result.status == ReviewStatus.STOPPED

    after_stop = tmp_workspace.load()
    assert after_stop.stage_status.get(str(StageName.ATTRIBUTE)) != ReviewStatus.COMPLETED
    assert stage.is_complete(after_stop) is False
    # Chapter one attributed; chapter two not.
    assert _line(after_stop, 0, 1).segments  # '"Hello," said Alice.' attributed
    assert _line(after_stop, 1, 0).segments == []  # chapter two untouched
    calls_at_stop = len(llm.attribute_calls)

    # Resume with stop cleared -> completes, no double-attribution of chapter one.
    result2 = stage.run(after_stop, _ctx(tmp_workspace, RecordingProgressReporter(), llm))
    assert result2.status == ReviewStatus.COMPLETED

    done = tmp_workspace.load()
    assert _line(done, 1, 0).segments  # chapter two now attributed
    # Chapter one was not re-attributed: only chapter two's batch was called on resume.
    assert len(llm.attribute_calls) == calls_at_stop + 1
    # Alice still appears once (resume reused, did not duplicate).
    assert [s.name for s in done.speakers].count("Alice") == 1


# --------------------------------------------------------------------------- #
# batching at MAX_LINES_PER_BATCH
# --------------------------------------------------------------------------- #
def test_chapter_over_batch_size_windows_into_multiple_calls(
    tmp_workspace: WorkspaceStore, sample_epub: Path
) -> None:
    # One chapter with MAX_LINES_PER_BATCH + 5 quote-bearing lines -> 2 windows -> 2 LLM calls.
    n_lines = MAX_LINES_PER_BATCH + 5
    ch_id = new_id("ch")
    lines = [
        Line(
            id=new_id("line"),
            chapter_id=ch_id,
            order=i,
            text=f'"Line {i}," said Alice.',
            segments=[],
        )
        for i in range(n_lines)
    ]
    book = Book(
        title="t",
        author="a",
        source_ebook_path=str(sample_epub),
        chapters=[Chapter(id=ch_id, order=0, title="Big", lines=lines)],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="batching",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
        },
    )
    tmp_workspace.save(project)

    llm = FakeLLMProvider(default_confidence=0.9)
    result = SegmentAttributeStage().run(
        project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    assert result.status == ReviewStatus.COMPLETED
    # 65 lines / 60 per window -> 2 windows -> 2 calls.
    assert len(llm.attribute_calls) == 2
    assert len(llm.attribute_calls[0][1]) == MAX_LINES_PER_BATCH  # first window full
    assert len(llm.attribute_calls[1][1]) == n_lines - MAX_LINES_PER_BATCH  # remainder


def test_all_narration_chapter_makes_no_llm_calls_but_completes(
    tmp_workspace: WorkspaceStore, sample_epub: Path
) -> None:
    ch_id = new_id("ch")
    lines = [
        Line(
            id=new_id("line"),
            chapter_id=ch_id,
            order=i,
            text=f"Pure narration line {i}.",
            segments=[],
        )
        for i in range(3)
    ]
    book = Book(
        title="t",
        author="a",
        source_ebook_path=str(sample_epub),
        chapters=[Chapter(id=ch_id, order=0, title="Quiet", lines=lines)],
    )
    project = Project(
        schema_version=CURRENT_SCHEMA_VERSION,
        id=new_id("proj"),
        name="narration-only",
        workspace_dir=str(tmp_workspace.layout.root),
        book=book,
        stage_status={
            str(StageName.PARSE): ReviewStatus.COMPLETED,
            str(StageName.CORRECT): ReviewStatus.COMPLETED,
        },
    )
    tmp_workspace.save(project)

    llm = FakeLLMProvider()
    result = SegmentAttributeStage().run(
        project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    assert result.status == ReviewStatus.COMPLETED
    assert llm.attribute_calls == []  # zero LLM calls
    reloaded = tmp_workspace.load()
    for ln in reloaded.book.chapters[0].lines:
        assert len(ln.segments) == 1
        assert ln.segments[0].role == SpeakerRole.NARRATOR
        assert ln.segments[0].review_status == ReviewStatus.APPROVED


# --------------------------------------------------------------------------- #
# is_complete / next_stage / Line.text untouched
# --------------------------------------------------------------------------- #
def test_is_complete_after_run(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    stage = SegmentAttributeStage()
    assert stage.is_complete(attribute_ready_project) is False
    stage.run(
        attribute_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm()),
    )
    assert stage.is_complete(attribute_ready_project) is True


def test_pipeline_next_stage_advances_to_review(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    pipeline = Pipeline([SegmentAttributeStage()])
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm()))
    reloaded = tmp_workspace.load()
    # The only stage is attribute, now complete -> next_stage is None.
    assert pipeline.next_stage(reloaded) is None


# --------------------------------------------------------------------------- #
# voice-category classification pass (finishing pass; soft; not re-run on resume)
# --------------------------------------------------------------------------- #
def _classify_llm() -> FakeLLMProvider:
    """Attribution as ``_scripted_llm`` + a classification script for Alice/Bob."""
    return FakeLLMProvider(
        text_script={
            '"Hello,"': ("Alice", 0.9),
            '"Hi,"': ("Bob", 0.5),
            '"We meet again,"': ("Alice", 0.95),
        },
        classify_script={"Alice": ("woman", 0.9), "Bob": ("man", 0.9)},
    )


def test_classification_stamps_categories_and_narrator_stays_unknown(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    llm = _classify_llm()
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    assert result.status == ReviewStatus.COMPLETED

    reloaded = tmp_workspace.load()
    by_name = {s.name: s for s in reloaded.speakers}
    assert by_name["Alice"].category == VoiceCategory.WOMAN
    assert by_name["Bob"].category == VoiceCategory.MAN
    # The narrator is never sent for classification -> stays unknown.
    assert by_name["narrator"].category == VoiceCategory.UNKNOWN
    # Exactly ONE classification pass, over the two discovered CHARACTER speakers.
    assert len(llm.classify_calls) == 1
    assert {p.name for p in llm.classify_calls[0]} == {"Alice", "Bob"}


def test_classification_unreachable_is_soft_stage_still_completed(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    # A classifier that raises a reachability error must NOT fail the (successful) attribution.
    llm = FakeLLMProvider(
        text_script={
            '"Hello,"': ("Alice", 0.9),
            '"Hi,"': ("Bob", 0.5),
            '"We meet again,"': ("Alice", 0.95),
        },
        raise_classify_unreachable=True,
    )
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    assert result.status == ReviewStatus.COMPLETED
    reloaded = tmp_workspace.load()
    assert reloaded.stage_status[str(StageName.ATTRIBUTE)] == ReviewStatus.COMPLETED
    assert all(s.category == VoiceCategory.UNKNOWN for s in reloaded.speakers)


def test_classification_malformed_is_soft_stage_still_completed(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    llm = FakeLLMProvider(
        text_script={
            '"Hello,"': ("Alice", 0.9),
            '"Hi,"': ("Bob", 0.5),
            '"We meet again,"': ("Alice", 0.95),
        },
        raise_classify_malformed=True,
    )
    result = SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm)
    )
    assert result.status == ReviewStatus.COMPLETED
    reloaded = tmp_workspace.load()
    assert all(s.category == VoiceCategory.UNKNOWN for s in reloaded.speakers)


def test_classification_not_re_run_once_categories_are_set(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    # Second run on the already-attributed+classified project must not reclassify: every
    # CHARACTER speaker already has a non-unknown category, so no profiles are sent.
    stage = SegmentAttributeStage()
    llm = _classify_llm()
    stage.run(attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), llm))
    assert len(llm.classify_calls) == 1

    reloaded = tmp_workspace.load()
    stage.run(reloaded, _ctx(tmp_workspace, RecordingProgressReporter(), llm))
    assert len(llm.classify_calls) == 1  # no second classification call


def test_resumed_pipeline_run_makes_zero_classify_calls(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    # Drive the stage through the actual Pipeline (which skips COMPLETED stages via
    # is_complete). Once attribute is COMPLETED, a second Pipeline.run must NOT re-enter the
    # stage — so the classification pass fires exactly once across both runs, never on resume.
    llm = _classify_llm()
    pipeline = Pipeline([SegmentAttributeStage()])
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter(), llm))
    assert len(llm.classify_calls) == 1  # classified on the finishing pass

    # A resumed run over the now-COMPLETED project: the stage is skipped entirely.
    pipeline.run(_ctx(tmp_workspace, RecordingProgressReporter(), llm))
    assert len(llm.classify_calls) == 1  # zero additional classification calls on resume


def test_line_text_untouched(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    before = {ln.id: ln.text for ch in attribute_ready_project.book.chapters for ln in ch.lines}
    SegmentAttributeStage().run(
        attribute_ready_project, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm())
    )
    reloaded = tmp_workspace.load()
    after = {ln.id: ln.text for ch in reloaded.book.chapters for ln in ch.lines}
    assert after == before  # the stage segments; it never edits Line.text


# --------------------------------------------------------------------------- #
# explicit re-attribute mechanism: clearing segments re-triggers attribution
# --------------------------------------------------------------------------- #
def test_clearing_segments_re_triggers_attribution_with_new_ids(
    tmp_workspace: WorkspaceStore, attribute_ready_project: Project
) -> None:
    stage = SegmentAttributeStage()
    stage.run(
        attribute_ready_project,
        _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm()),
    )

    persisted = tmp_workspace.load()
    hello = _line(persisted, 0, 1)
    old_ids = [s.id for s in hello.segments]
    assert old_ids  # was attributed

    # Simulate the UI "re-attribute this line" action: clear segments, then re-run.
    hello.segments = []
    tmp_workspace.save(persisted)
    stage.run(persisted, _ctx(tmp_workspace, RecordingProgressReporter(), _scripted_llm()))

    reloaded = _line(tmp_workspace.load(), 0, 1)
    new_ids = [s.id for s in reloaded.segments]
    assert reloaded.segments  # re-attributed
    assert set(new_ids).isdisjoint(old_ids)  # fresh segment ids, no reuse of stale audio keys

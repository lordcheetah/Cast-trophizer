"""Provider-need gating + ``--auto-accept`` invariants for the CLI runner.

These are the deviation-sensitive guarantees from the plan (§`_run_once` provider gating and
§`_auto_accept`), verified empirically by driving ``cli.main(argv)`` with **spy** provider
factories injected through :class:`~casttrophizer.cli.CliDeps`:

* an LLM is built **only** while attribution is still pending;
* a TTS provider is built **only** once the review gate is actually reachable (so the
  halt-at-review run never constructs one);
* ``--auto-accept`` approves NEEDS_REVIEW attributions and rejects PENDING text suggestions
  (text left as-is) **without** assigning voices — so it must still halt at review when a
  speaker is unvoiced (criterion 3), never silently complete;
* an idempotent re-run of a COMPLETED project builds no providers at all.

All fakes are offline/deterministic; no real API/model/ffmpeg is ever touched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from casttrophizer import cli
from casttrophizer.audio.synthesize import unresolved_voices
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project
from casttrophizer.providers import LLMProvider, TTSProvider
from casttrophizer.review.gate import review_blockers
from casttrophizer.workspace.store import WorkspaceStore
from tests.cli.conftest import SAMPLE_TEXT_SCRIPT, build_deps
from tests.fakes import FakeLLMProvider, FakeM4BAssembler, FakeTTSProvider


class _Spy:
    """A counting provider factory: records how many times the CLI built the provider."""

    def __init__(self, provider: object) -> None:
        self._provider = provider
        self.calls = 0

    def __call__(self, config: AppConfig) -> object:
        self.calls += 1
        return self._provider


def _spy_llm() -> _Spy:
    return _Spy(FakeLLMProvider(text_script=SAMPLE_TEXT_SCRIPT))


def _spy_tts() -> _Spy:
    return _Spy(FakeTTSProvider())


def _deps_with_spies(
    *,
    llm: Callable[[AppConfig], LLMProvider],
    tts: Callable[[AppConfig], TTSProvider],
    assembler: FakeM4BAssembler | None = None,
) -> cli.CliDeps:
    return build_deps(llm_factory=llm, tts_factory=tts, assembler=assembler)


def _run(wd: Path, *argv: str, deps: cli.CliDeps) -> int:
    return cli.main(["--workdir", str(wd), *argv], deps=deps)


def _seg_by_text(project: Project, text: str):  # noqa: ANN202 - test helper
    return next(
        seg
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
        if seg.text == text
    )


# --------------------------------------------------------------------------- #
# §2 --auto-accept invariants (approve attribution + reject suggestion; NEVER voices)
# --------------------------------------------------------------------------- #
def test_auto_accept_before_voices_still_halts_and_resolves_criteria_1_and_2(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run --auto-accept` with an unvoiced speaker: clears criteria 1 & 2 but still halts.

    ``review_ready_project`` carries exactly one NEEDS_REVIEW segment ("Hi," -> Bob), one
    PENDING text suggestion (Bbo -> Bob, on the ``"Hi," Bob replied.`` line), and Bob has no
    voice. Auto-accept must approve the attribution and *reject* the suggestion (text as-is),
    but must NOT assign a voice — so the run stays halted at the gate (exit 3).
    """
    wd = Path(review_ready_project.workspace_dir)
    llm, tts = _spy_llm(), _spy_tts()
    deps = _deps_with_spies(llm=llm, tts=tts)

    # Snapshot the PENDING suggestion + its line text so we can prove "text as-is" after.
    before = WorkspaceStore.for_dir(wd).load()
    pending_line = next(
        ln
        for ch in before.book.chapters
        for ln in ch.lines
        if any(s.status == ReviewStatus.PENDING for s in ln.suggestions)
    )
    original_text = pending_line.text

    rc = _run(wd, "run", "--auto-accept", deps=deps)

    assert rc == 3  # still NEEDS_REVIEW: Bob is unvoiced (criterion 3)
    out = capsys.readouterr().out
    assert "halted for review" in out
    assert "voices needed for: Bob" in out
    # criteria 1 & 2 are cleared, so the "re-run with --auto-accept" hint is gone
    assert "re-run with `--auto-accept`" not in out

    # attribution was COMPLETED, so no LLM was ever built; review never became reachable,
    # so no TTS was ever built either.
    assert llm.calls == 0
    assert tts.calls == 0

    after = WorkspaceStore.for_dir(wd).load()
    # criterion 1: the low-confidence Bob attribution became APPROVED via the real service
    bob_seg = _seg_by_text(after, '"Hi,"')
    assert bob_seg.review_status == ReviewStatus.APPROVED
    assert all(
        s.review_status != ReviewStatus.NEEDS_REVIEW
        for ch in after.book.chapters
        for ln in ch.lines
        for s in ln.segments
    )
    # criterion 2: the PENDING suggestion was REJECTED and the line text is UNCHANGED
    resolved_line = next(
        ln for ch in after.book.chapters for ln in ch.lines if ln.id == pending_line.id
    )
    assert all(s.status != ReviewStatus.PENDING for s in resolved_line.suggestions)
    assert any(s.status == ReviewStatus.REJECTED for s in resolved_line.suggestions)
    assert resolved_line.text == original_text
    # criterion 3 is untouched: Bob still blocks
    assert unresolved_voices(after) == ["Bob"]
    assert review_blockers(after).unassigned_voices == ["Bob"]
    # the review gate never wrote COMPLETED
    assert str(StageName.REVIEW) not in after.stage_status


def test_auto_accept_completes_only_after_the_unvoiced_speaker_is_voiced(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Assign Bob's voice, then `run --auto-accept` completes and builds a TTS provider."""
    wd = Path(review_ready_project.workspace_dir)
    clip = str(fake_voice_clips[0])
    llm, tts = _spy_llm(), _spy_tts()
    assembler = FakeM4BAssembler()
    deps = _deps_with_spies(llm=llm, tts=tts, assembler=assembler)

    assert _run(wd, "assign-voice", "Bob", clip, deps=deps) == 0
    capsys.readouterr()

    rc = _run(wd, "run", "--auto-accept", deps=deps)

    assert rc == 0
    assert "output:" in capsys.readouterr().out
    # attribution already COMPLETED -> LLM never built; synthesize was reached -> TTS built once
    assert llm.calls == 0
    assert tts.calls == 1
    assert len(assembler.requests) == 1

    project = WorkspaceStore.for_dir(wd).load()
    for stage in StageName:
        assert project.stage_status[str(stage)] == ReviewStatus.COMPLETED


# --------------------------------------------------------------------------- #
# §3 provider-need gating
# --------------------------------------------------------------------------- #
def test_tts_factory_not_called_on_halt_at_review_run(
    tmp_path: Path,
    sample_epub: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first `run` (parse->correct->attribute, halts at review) must not build TTS.

    Synthesis is only reachable past the gate, so the TTS factory stays untouched — this is
    what lets the review flow run without the ``tts`` extra / ffmpeg installed. Regression
    guard for the fresh-project vacuous-``is_review_complete`` gating defect: synthesize is
    gated on attribution being COMPLETED, not on the emptiness of a not-yet-parsed project.
    """
    wd = tmp_path / "ws"
    llm, tts = _spy_llm(), _spy_tts()
    deps = _deps_with_spies(llm=llm, tts=tts)

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    capsys.readouterr()

    rc = _run(wd, "run", deps=deps)

    assert rc == 3
    assert llm.calls == 1  # attribution pending -> LLM built exactly once
    assert tts.calls == 0  # review not reached -> TTS must not be built


def test_attribute_complete_run_does_not_build_llm(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Once attribution is COMPLETED, `run` must not demand an LLM (no key, no build).

    Uses a fresh project whose attribution is already done; the LLM factory would raise if the
    CLI tried to build it, proving the gate never reaches for a provider it does not need.
    """
    wd = Path(review_ready_project.workspace_dir)
    clip = str(fake_voice_clips[0])

    def _boom_llm(config: AppConfig) -> LLMProvider:
        raise AssertionError("LLM must not be built once attribution is COMPLETED")

    tts = _spy_tts()
    deps = build_deps(llm_factory=_boom_llm, tts_factory=tts, assembler=FakeM4BAssembler())

    assert _run(wd, "assign-voice", "Bob", clip, deps=deps) == 0
    capsys.readouterr()

    # completes without ever constructing the LLM (would raise) — synthesize builds TTS once
    rc = _run(wd, "run", "--auto-accept", deps=deps)
    assert rc == 0
    assert tts.calls == 1


def test_idempotent_rerun_of_complete_project_builds_no_providers(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A re-run of a COMPLETED project is a no-op (exit 0) and constructs no providers."""
    wd = tmp_path / "ws"
    clip = str(fake_voice_clips[0])

    # drive the whole flow to completion with ordinary working fakes
    build = build_deps(assembler=FakeM4BAssembler())
    assert _run(wd, "new", "--epub", str(sample_epub), deps=build) == 0
    assert _run(wd, "run", deps=build) == 3
    for token in ("0", "Alice", "Bob"):
        assert _run(wd, "assign-voice", token, clip, deps=build) == 0
    assert _run(wd, "run", "--auto-accept", deps=build) == 0
    capsys.readouterr()

    # now re-run with spies: nothing is pending, so no provider is built and no assembly happens
    llm, tts = _spy_llm(), _spy_tts()
    assembler = FakeM4BAssembler()
    rerun = _deps_with_spies(llm=llm, tts=tts, assembler=assembler)
    rc = _run(wd, "run", deps=rerun)

    assert rc == 0
    assert "complete" in capsys.readouterr().out
    assert llm.calls == 0
    assert tts.calls == 0
    assert assembler.requests == []

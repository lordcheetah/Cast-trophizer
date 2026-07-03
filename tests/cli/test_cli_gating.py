"""Provider-construction + ``--auto-accept`` invariants for the CLI runner.

The CLI **always** constructs both providers and injects them into the ``StageContext`` (so
no stage is ever reached without one — ``Pipeline.run`` advances through several stages in
one call). Construction is cheap: it imports no heavy SDK (``torch``/``anthropic`` load
lazily inside the provider methods), so the real invariant is not "was the factory called"
but "was the provider actually *used*". These tests drive ``cli.main(argv)`` with **spy**
factories injected through :class:`~casttrophizer.cli.CliDeps` and assert:

* on a halt-at-review run the TTS provider is built but its ``synthesize`` is never called
  (the review gate is never passed);
* once attribution is COMPLETED the LLM is built but its ``attribute_speakers`` is never
  called, and the run does not demand an LLM key;
* ``--auto-accept`` approves NEEDS_REVIEW attributions and rejects PENDING text suggestions
  (text left as-is) **without** assigning voices — so it must still halt at review when a
  speaker is unvoiced (criterion 3), never silently complete;
* an idempotent re-run of a COMPLETED project uses no provider (built but never invoked).

The module-import cheapness (importing ``casttrophizer.cli`` pulls in no
``torch``/``anthropic``/``chatterbox``) is asserted by the subprocess
``tests/cli/test_cli_import.py``. That the *real* provider constructors are themselves cheap
(so always-building them on a run stays lazy) was confirmed empirically — their ``__init__``
imports nothing heavy and ``is_available`` uses env-var/``find_spec`` checks only. All fakes
here are offline/deterministic; no real API/model/ffmpeg is ever touched.
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
    """A counting provider factory: records how many times the CLI built the provider.

    ``provider`` is the single fake instance handed back on every call, so a test can inspect
    it after the run (e.g. ``synthesize_calls`` / ``attribute_calls``) to prove the provider
    was built-but-unused vs. actually invoked.
    """

    def __init__(self, provider: object) -> None:
        self.provider = provider
        self.calls = 0

    def __call__(self, config: AppConfig) -> object:
        self.calls += 1
        return self.provider


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

    # both providers are built (cheap), but neither is *used*: attribution was already
    # COMPLETED (LLM never invoked) and review never passed (TTS never invoked).
    assert llm.provider.attribute_calls == []
    assert tts.provider.synthesize_calls == []

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
    # attribution already COMPLETED -> LLM built but never invoked; synthesize was reached ->
    # the TTS provider was actually used.
    assert llm.provider.attribute_calls == []
    assert tts.provider.synthesize_calls != []
    assert len(assembler.requests) == 1

    project = WorkspaceStore.for_dir(wd).load()
    for stage in StageName:
        assert project.stage_status[str(stage)] == ReviewStatus.COMPLETED


# --------------------------------------------------------------------------- #
# §3 provider-need gating
# --------------------------------------------------------------------------- #
def test_halt_at_review_run_builds_tts_but_never_uses_it(
    tmp_path: Path,
    sample_epub: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first `run` (parse->correct->attribute, halts at review) builds TTS but never uses it.

    Both providers are constructed up front (cheap, no heavy import), but synthesis is only
    reachable past the gate, so the TTS provider's ``synthesize`` is never invoked — this is
    what lets the review flow run without the ``tts`` extra / ffmpeg installed. The LLM, by
    contrast, is actually used because attribution runs this pass.
    """
    wd = tmp_path / "ws"
    llm, tts = _spy_llm(), _spy_tts()
    deps = _deps_with_spies(llm=llm, tts=tts)

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    capsys.readouterr()

    rc = _run(wd, "run", deps=deps)

    assert rc == 3
    assert llm.provider.attribute_calls != []  # attribution ran this pass -> LLM used
    assert tts.provider.synthesize_calls == []  # review not reached -> TTS built but unused


def test_attribute_complete_run_does_not_demand_llm_key(
    review_ready_project: Project,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Once attribution is COMPLETED, `run` must not demand an LLM key.

    The LLM is still constructed (cheap) and injected, but because attribution is already
    done the key preflight is skipped and its ``attribute_speakers`` is never invoked — so an
    *unavailable* LLM (no ``ANTHROPIC_API_KEY``) does not block the run from completing.
    """
    wd = Path(review_ready_project.workspace_dir)
    clip = str(fake_voice_clips[0])

    # An LLM with no key available: the run must still complete (preflight skipped, unused).
    llm = _Spy(FakeLLMProvider(available=False))
    tts = _spy_tts()
    deps = build_deps(llm_factory=llm, tts_factory=tts, assembler=FakeM4BAssembler())

    assert _run(wd, "assign-voice", "Bob", clip, deps=deps) == 0
    capsys.readouterr()

    rc = _run(wd, "run", "--auto-accept", deps=deps)
    assert rc == 0
    # the LLM was built but never invoked; synthesize was reached, so the TTS was used
    assert llm.provider.attribute_calls == []
    assert tts.provider.synthesize_calls != []


def test_run_until_parse_needs_no_llm_key(
    tmp_path: Path,
    sample_epub: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run --until parse` must not demand an LLM key — attribute never runs this pass.

    The key preflight is gated on the attribute stage being *reachable* given ``--until``, so
    stopping before attribute (to review OCR/text) works with no ``ANTHROPIC_API_KEY``.
    """
    wd = tmp_path / "ws"
    llm = _Spy(FakeLLMProvider(available=False))  # no key configured
    deps = build_deps(llm_factory=llm, tts_factory=_spy_tts(), assembler=FakeM4BAssembler())

    assert _run(wd, "new", "--epub", str(sample_epub), deps=deps) == 0
    capsys.readouterr()

    rc = _run(wd, "run", "--until", "parse", deps=deps)
    out = capsys.readouterr().out.lower()
    assert rc != 2  # NOT blocked by the LLM-key preflight (exit 2)
    assert "anthropic_api_key" not in out  # preflight message never shown
    assert llm.provider.attribute_calls == []  # LLM built but never invoked
    assert "stopped after parse" in out  # parse ran; pass stopped before attribute


def test_idempotent_rerun_of_complete_project_uses_no_providers(
    tmp_path: Path,
    sample_epub: Path,
    fake_voice_clips: list[Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A re-run of a COMPLETED project is a no-op (exit 0): providers are built but unused.

    Every stage is already complete, so ``Pipeline.run`` skips them all — the constructed
    providers are never invoked and no assembly happens.
    """
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

    # now re-run with spies: nothing is pending, so neither provider is invoked and no assembly
    llm, tts = _spy_llm(), _spy_tts()
    assembler = FakeM4BAssembler()
    rerun = _deps_with_spies(llm=llm, tts=tts, assembler=assembler)
    rc = _run(wd, "run", deps=rerun)

    assert rc == 0
    assert "complete" in capsys.readouterr().out
    assert llm.provider.attribute_calls == []
    assert tts.provider.synthesize_calls == []
    assert assembler.requests == []

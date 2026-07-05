"""Qt-free command-line runner (``castrun``) that drives the pipeline from a terminal.

This is an **orchestrator only**: it constructs the existing
:class:`~casttrophizer.pipeline.runner.Pipeline`, builds a
:class:`~casttrophizer.pipeline.stage.StageContext`, injects providers, and calls
``Pipeline.run(ctx)``. All stage logic, resumability, and the review gate already exist and
are unchanged. The shared orchestration (project creation, provider construction, the run
pass, outcome/status classification) lives in :mod:`casttrophizer.app_service`; this module
is a thin presentation wrapper over it (argparse, printing, exit-code mapping, SIGINT, and
the ``--auto-accept`` re-run).

Command surface::

    castrun [--workdir DIR] <command> ...

    new           --epub PATH [--name NAME] [--force]
    run           [--until STAGE] [--auto-accept] [--provider claude|lmstudio]
    status
    speakers
    assign-voice  SPEAKER CLIP        # SPEAKER = list index (int) or name (case-insensitive)
    assign-voice  --rest [--man P] [--woman P] [--boy P] [--girl P] [--default P]
                                      # bulk-voice every still-unvoiced speaker by category

Import discipline: this module is Qt-free and import-cheap. It never imports ``PySide6``,
``torch``, ``anthropic``, ``openai``, or ``chatterbox`` at module load. Both providers are
constructed on every ``run`` and injected into the :class:`StageContext`, but construction
is cheap — the heavy SDKs (``torch``/``anthropic``) load lazily inside the provider methods,
so they are only imported if a stage (synthesize/attribute) actually calls the provider.
Providers/assembler reach the CLI through :class:`CliDeps` so tests inject offline fakes.
"""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

from casttrophizer.app_service.deps import (
    AppServiceDeps,
    default_llm_factory,
    default_tts_factory,
)
from casttrophizer.app_service.outcome import RunOutcomeKind, interpret_result
from casttrophizer.app_service.pipeline_service import build_pipeline, run_pipeline
from casttrophizer.app_service.projects import ProjectExistsError, create_project
from casttrophizer.app_service.status import next_stage_name, stage_status_rows
from casttrophizer.audio.synthesize import unresolved_speakers
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.domain.models import Project, Speaker, VoiceClip
from casttrophizer.errors import CasttrophizerError, ConfigError, PreconditionError
from casttrophizer.pipeline.progress import ProgressReporter
from casttrophizer.pipeline.stage import StageResult
from casttrophizer.providers import LLMProvider, TTSProvider
from casttrophizer.review import actions
from casttrophizer.review.gate import describe_blockers, review_blockers
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.store import WorkspaceStore

if TYPE_CHECKING:
    from casttrophizer.audio.assembler import M4BAssembler

__all__ = ["main", "CliDeps", "PrintReporter"]

# Process exit codes (mirrors the stages' own fail-fast guards).
_EXIT_OK = 0
_EXIT_ERROR = 1  # generic/domain error, or a stage returning FAILED
_EXIT_PRECONDITION = 2  # LLM key preflight / provider-config error before the pipeline runs.
# NOTE: the tts-extra and ffmpeg preflights were removed — their unavailability now surfaces
# as a stage FAILED (exit 1) via the stages' own guards, not as an exit-2 preflight.
_EXIT_NEEDS_REVIEW = 3  # the run halted at the review gate
_EXIT_STOPPED = 4  # a cooperative stop was requested (Ctrl-C)


class CliError(CasttrophizerError):
    """A user-facing CLI failure carrying the process exit code to return."""

    def __init__(self, message: str, *, code: int = _EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# progress reporting (ProgressReporter protocol; Ctrl-C flips the stop flag)
# --------------------------------------------------------------------------- #
class PrintReporter:
    """A minimal console :class:`ProgressReporter` with a cooperative-stop flag.

    :meth:`request_stop` is what the SIGINT handler flips; stages poll :meth:`should_stop`
    at their checkpoints and persist partial state before returning STOPPED, so a later
    ``run`` resumes.
    """

    def __init__(self) -> None:
        self._total = 0
        self._done = 0
        self._stop = False

    def set_total(self, total: int) -> None:
        self._total = total
        self._done = 0

    def advance(self, n: int = 1, *, message: str | None = None) -> None:
        self._done += n
        suffix = f" - {message}" if message else ""
        print(f"    [{self._done}/{self._total}]{suffix}")

    def message(self, text: str) -> None:
        print(f"  - {text}")

    def should_stop(self) -> bool:
        return self._stop

    def request_stop(self) -> None:
        """Flip the cooperative-stop flag (called from the SIGINT handler)."""
        self._stop = True


def _install_sigint(progress: ProgressReporter) -> Callable[[], None]:
    """Route SIGINT (Ctrl-C) to ``progress.request_stop`` when the reporter supports it.

    Returns a callable that restores the previously-installed SIGINT handler; the caller
    must invoke it (via ``try/finally``) so a single ``run`` doesn't leak a global handler
    into the rest of the process (which would corrupt the in-process test suite).
    """
    request_stop = getattr(progress, "request_stop", None)
    if not callable(request_stop):
        return lambda: None

    def _handler(signum: int, frame: FrameType | None) -> None:
        print("\n  (stop requested — finishing the current step, then persisting…)")
        request_stop()

    try:
        previous = signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):
        return lambda: None  # not on the main thread; cooperative stop stays unavailable

    def _restore() -> None:
        try:
            signal.signal(signal.SIGINT, previous)
        except (ValueError, OSError):
            pass

    return _restore


# --------------------------------------------------------------------------- #
# injection seam (delegates the shared fields to app_service.AppServiceDeps)
# --------------------------------------------------------------------------- #
@dataclass
class CliDeps:
    """Injection seam so tests drive the CLI with offline fakes (no real API/model/ffmpeg).

    The provider/assembler/config fields mirror
    :class:`~casttrophizer.app_service.deps.AppServiceDeps` (see :meth:`to_app_service_deps`);
    ``progress_factory`` is CLI-only (the UI supplies its own Qt reporter).
    """

    llm_factory: Callable[[AppConfig], LLMProvider] = default_llm_factory
    tts_factory: Callable[[AppConfig], TTSProvider] = default_tts_factory
    assembler: M4BAssembler | None = None  # -> AssembleStage(assembler=...); None = real one
    progress_factory: Callable[[], ProgressReporter] = PrintReporter
    config: AppConfig | None = None  # None -> AppConfig.from_env()

    def to_app_service_deps(self) -> AppServiceDeps:
        """Adapt to the shared :class:`AppServiceDeps` (drops the CLI-only progress factory)."""
        return AppServiceDeps(
            llm_factory=self.llm_factory,
            tts_factory=self.tts_factory,
            assembler=self.assembler,
            config=self.config,
        )


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _workdir(args: argparse.Namespace) -> Path:
    return Path(args.workdir)


def _require_store(args: argparse.Namespace) -> WorkspaceStore:
    """Load an existing workspace store, or fail fast if no project lives there."""
    store = WorkspaceStore.for_dir(_workdir(args))
    if not store.exists():
        raise CliError(
            f"no project at {_workdir(args)}; run `castrun new --epub ...` first",
            code=_EXIT_ERROR,
        )
    return store


def _speaker_has_voice(project: Project, speaker: Speaker) -> bool:
    """True iff ``speaker`` has an assigned voice clip whose source file exists on disk."""
    if speaker.voice_clip_id is None:
        return False
    clip = next((vc for vc in project.voice_clips if vc.id == speaker.voice_clip_id), None)
    if clip is None:
        return False
    return Path(clip.source_path).is_file()


def _resolve_speaker(project: Project, token: str) -> Speaker:
    """Resolve ``token`` to a speaker by list index (int) or case-insensitive name."""
    if token.lstrip("-").isdigit():
        idx = int(token)
        if 0 <= idx < len(project.speakers):
            return project.speakers[idx]
        raise CliError(
            f"speaker index {idx} out of range (0..{len(project.speakers) - 1})",
            code=_EXIT_ERROR,
        )
    folded = token.casefold()
    for speaker in project.speakers:
        if speaker.name.casefold() == folded:
            return speaker
    raise CliError(
        f"no speaker matching {token!r}; run `castrun speakers` to list", code=_EXIT_ERROR
    )


# --------------------------------------------------------------------------- #
# --auto-accept: resolve gate criteria 1 & 2 non-interactively
# --------------------------------------------------------------------------- #
def _auto_accept(store: WorkspaceStore, project: Project) -> bool:
    """Approve NEEDS_REVIEW attributions as-proposed and reject PENDING text suggestions.

    Clears review-gate criteria 1 (attribution) and 2 (text suggestions) without touching
    voices (criterion 3, always manual). Rejecting suggestions keeps ``line.text`` exactly as
    the automated pass left it — uncertain OCR/spellcheck guesses are never applied unreviewed.
    Returns True iff anything changed (so the caller can skip a pointless re-run).

    Applies the pure :mod:`~casttrophizer.review.actions` in-memory across every item and
    persists **once** at the end (a real novel has thousands of segments/suggestions — one
    atomic whole-project write instead of one fsync per item). The single save lands on disk
    before the caller's re-run reloads, so the continued pass sees the resolved gate state.
    """
    changed = False
    for chapter in project.book.chapters:
        for line in chapter.lines:
            for segment in line.segments:
                if segment.review_status == ReviewStatus.NEEDS_REVIEW:
                    actions.approve_attribution(segment)
                    changed = True
            for suggestion in list(line.suggestions):
                if suggestion.status == ReviewStatus.PENDING:
                    actions.reject_suggestion(line, suggestion.id)
                    changed = True
    if changed:
        store.save(project)
    return changed


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_new(args: argparse.Namespace, deps: CliDeps) -> int:
    """Create a project from an EPUB and persist it into the workspace."""
    workdir = _workdir(args)
    config = deps.config or AppConfig.from_env()
    try:
        _, project = create_project(
            Path(args.epub), args.name, workdir, config, overwrite=args.force
        )
    except ProjectExistsError as exc:
        raise CliError(f"{exc}; pass --force to overwrite", code=_EXIT_ERROR) from exc
    except CasttrophizerError as exc:
        raise CliError(str(exc), code=_EXIT_ERROR) from exc

    print(f"created project {project.name!r} at {workdir}")
    print("next: castrun run   (parses, corrects, attributes, then halts for review)")
    return _EXIT_OK


def cmd_run(args: argparse.Namespace, deps: CliDeps) -> int:
    """Advance the pipeline from wherever it is; ``--auto-accept`` clears criteria 1 & 2."""
    store = _require_store(args)
    app_deps = deps.to_app_service_deps()
    config = deps.config or AppConfig.from_env()
    pipeline = build_pipeline(app_deps)
    progress = deps.progress_factory()
    restore_sigint = _install_sigint(progress)
    until = StageName(args.until) if args.until else None
    final = pipeline.stages[-1].name

    try:
        result = run_pipeline(
            store, pipeline, app_deps, config, progress, provider=args.provider, until=until
        )

        if result.status == ReviewStatus.NEEDS_REVIEW and args.auto_accept:
            project = store.load()
            if _auto_accept(store, project):
                result = run_pipeline(
                    store, pipeline, app_deps, config, progress, provider=args.provider, until=until
                )
    except (PreconditionError, ConfigError) as exc:
        # The shared layer raises these Qt/CLI-free; map them to the exit-2 precondition UX.
        raise CliError(str(exc), code=_EXIT_PRECONDITION) from exc
    finally:
        restore_sigint()

    return _report_run(result, store, until=until, final=final)


def _report_run(
    result: StageResult,
    store: WorkspaceStore,
    *,
    until: StageName | None = None,
    final: StageName = StageName.ASSEMBLE,
) -> int:
    """Print the classified run outcome + next-step hints and map it to a process exit code.

    Classification (COMPLETED vs. a ``--until`` early stop, NEEDS_REVIEW, STOPPED, FAILED)
    comes from :func:`~casttrophizer.app_service.interpret_result`; this function only prints
    and maps to a process exit code. An early ``--until`` stop classifies as STOPPED (the
    audiobook is NOT done), reported with the cooperative-stop exit code, never "complete".
    """
    outcome = interpret_result(result, store, until=until, final=final)

    if outcome.kind == RunOutcomeKind.COMPLETED:
        if outcome.output_path is not None:
            print(f"complete; output: {outcome.output_path}")
        else:
            print("complete")
        return _EXIT_OK

    if outcome.kind == RunOutcomeKind.NEEDS_REVIEW:
        assert outcome.blockers is not None  # populated for NEEDS_REVIEW
        blockers = outcome.blockers
        project = store.load()
        print(f"halted for review: {outcome.summary}")
        if blockers.needs_attribution or blockers.pending_suggestions:
            print("  approve attributions / resolve text edits: re-run with `--auto-accept`")
        for index, speaker in enumerate(project.speakers):
            if speaker.name in blockers.unassigned_voices:
                print(
                    f"  assign a voice: castrun assign-voice {index} <clip.wav>"
                    f"  (speaker: {speaker.name})"
                )
        if blockers.unassigned_voices:
            print(
                "  or bulk-voice the rest by category: "
                "castrun assign-voice --rest --default <clip.wav>"
            )
        return _EXIT_NEEDS_REVIEW

    if outcome.kind == RunOutcomeKind.STOPPED:
        if result.status == ReviewStatus.COMPLETED:  # early --until stop
            print(f"stopped after {result.stage.value}; re-run `castrun run` to continue")
        else:
            print("stopped; re-run `castrun run` to resume")
        return _EXIT_STOPPED

    print(f"failed: {result.message}")
    return _EXIT_ERROR


def cmd_status(args: argparse.Namespace, deps: CliDeps) -> int:
    """Print per-stage status, the next stage, and (past attribute) the review blockers."""
    store = _require_store(args)
    project = store.load()
    pipeline = build_pipeline(deps.to_app_service_deps())

    print(f"project: {project.name}")
    for row in stage_status_rows(project, pipeline):
        print(f"  {row.name.value:11} {row.status.value if row.status else '-'}")

    nxt = next_stage_name(project, pipeline)
    print(f"next: {nxt.value if nxt else 'complete'}")

    by_name = {stage.name: stage for stage in pipeline.stages}
    if by_name[StageName.ATTRIBUTE].is_complete(project):
        print(f"review: {describe_blockers(review_blockers(project))}")
    return _EXIT_OK


def cmd_speakers(args: argparse.Namespace, deps: CliDeps) -> int:
    """List discovered speakers with a stable index, name, role, and voice state."""
    store = _require_store(args)
    project = store.load()
    if not project.speakers:
        print("no speakers yet; run `castrun run` to attribute the book first")
        return _EXIT_OK
    for index, speaker in enumerate(project.speakers):
        state = "voiced" if _speaker_has_voice(project, speaker) else "NO VOICE"
        print(
            f"[{index}] {speaker.name} ({speaker.role.value}) "
            f"[{speaker.category.value}] - {state}"
        )
    return _EXIT_OK


def cmd_assign_voice(args: argparse.Namespace, deps: CliDeps) -> int:
    """Assign a voice clip to one speaker, or (``--rest``) bulk-voice the remaining cast."""
    store = _require_store(args)
    project = store.load()

    if args.rest:
        return _cmd_assign_voice_rest(args, deps, store, project)

    if args.speaker is None or args.clip is None:
        raise CliError(
            "assign-voice requires SPEAKER and CLIP (or pass --rest to voice the remaining "
            "cast by category)",
            code=_EXIT_ERROR,
        )
    speaker = _resolve_speaker(project, args.speaker)

    svc = ReviewService(store, project)
    try:
        clip = svc.register_voice_clip(args.clip, label=speaker.name)
    except ValueError as exc:
        raise CliError(str(exc), code=_EXIT_ERROR) from exc
    svc.assign_voice(speaker, clip)
    print(f"assigned {Path(args.clip).name} to {speaker.name}")
    return _EXIT_OK


#: Per-category ``--rest`` flags (the ``VoiceCategory`` members with a dedicated flag). The
#: ``unknown`` category has no flag — it resolves straight to ``--default``.
_REST_CATEGORY_FLAGS = ("man", "woman", "boy", "girl")


def _resolve_category_clip(
    category: str, args: argparse.Namespace, config: AppConfig
) -> str | None:
    """Resolve one voice category to a clip path: flag > env default > ``--default`` fallback.

    ``--man/--woman/...`` win over the matching ``CASTTROPHIZER_VOICE_*`` env default, which
    wins over the shared ``--default`` (which itself wins over ``CASTTROPHIZER_VOICE_DEFAULT``).
    A category with no flag (``unknown``) or no per-category value falls back to the default;
    returns ``None`` when nothing resolves (an uncovered category).
    """
    default_path = args.default or config.voice_defaults.get("default")
    if category in _REST_CATEGORY_FLAGS:
        flag_value = getattr(args, category)
        return flag_value or config.voice_defaults.get(category) or default_path
    return default_path  # unknown / any future flagless category


def _cmd_assign_voice_rest(
    args: argparse.Namespace,
    deps: CliDeps,
    store: WorkspaceStore,
    project: Project,
) -> int:
    """Bulk-assign category default clips to every referenced, still-unvoiced speaker.

    Targets exactly the speakers the review gate flags (``unresolved_speakers`` — shares the
    synthesize precheck predicate). A renderable ``speaker_id=None`` segment resolves to the
    reserved narrator, so an unvoiced narrator is a valid target here and ``--rest`` voices it.
    Validates full coverage up front — if any target's
    category resolves to no clip, fails without assigning anything (never leaves a referenced
    speaker unvoiced, which would re-block the gate). Registers one shared ``VoiceClip`` per
    distinct path (validating each path exists) and persists all assignments in a single save.
    """
    if args.speaker is not None or args.clip is not None:
        raise CliError(
            "assign-voice --rest takes no positional SPEAKER/CLIP arguments", code=_EXIT_ERROR
        )
    config = deps.config or AppConfig.from_env()

    targets = unresolved_speakers(project)
    if not targets:
        print("all referenced speakers already voiced; nothing to assign")
        return _EXIT_OK

    # Resolve each target's clip and collect any uncovered categories (dry run, no mutation).
    resolved: list[tuple[Speaker, str]] = []  # (speaker, clip path)
    uncovered: dict[str, list[str]] = {}  # category -> speaker names with no clip
    for speaker in targets:
        category = speaker.category.value
        path = _resolve_category_clip(category, args, config)
        if path is None:
            uncovered.setdefault(category, []).append(speaker.name)
        else:
            resolved.append((speaker, path))

    if uncovered:
        detail = "; ".join(
            f"{category} (speakers: {', '.join(names)})" for category, names in uncovered.items()
        )
        hint_flags = sorted(
            f"--{category}" for category in uncovered if category in _REST_CATEGORY_FLAGS
        )
        hint = " / ".join([*hint_flags, "--default"])  # --default always applies (covers unknown)
        raise CliError(
            f"no clip for categories: {detail}; pass {hint}",
            code=_EXIT_ERROR,
        )

    # Coverage is complete: register one shared VoiceClip per distinct path (validates each
    # path exists, raising ValueError before anything is persisted), then batch-assign + save.
    svc = ReviewService(store, project)
    clips_by_path: dict[str, VoiceClip] = {}
    counts: dict[str, int] = {}
    pairs: list[tuple[Speaker, VoiceClip]] = []
    for speaker, path in resolved:
        clip = clips_by_path.get(path)
        if clip is None:
            try:
                clip = actions.register_voice_clip(
                    project, path, label=f"{speaker.category.value} (default)"
                )
            except ValueError as exc:
                raise CliError(str(exc), code=_EXIT_ERROR) from exc
            clips_by_path[path] = clip
        pairs.append((speaker, clip))
        counts[speaker.category.value] = counts.get(speaker.category.value, 0) + 1

    svc.assign_voices(pairs)

    breakdown = ", ".join(f"{category} x{n}" for category, n in sorted(counts.items()))
    print(f"assigned defaults to {len(pairs)} speaker(s): {breakdown}")
    return _EXIT_OK


# --------------------------------------------------------------------------- #
# argparse wiring + entry point
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="castrun",
        description="Drive an ebook through the Cast-trophizer pipeline from the terminal.",
    )
    parser.add_argument(
        "--workdir",
        default=".",
        help="workspace directory the command operates on (default: current directory)",
    )
    sub = parser.add_subparsers(dest="command")

    p_new = sub.add_parser("new", help="create a project from an EPUB")
    p_new.add_argument("--epub", required=True, help="path to the source EPUB (read-only input)")
    p_new.add_argument("--name", default=None, help="project name (default: the EPUB filename)")
    p_new.add_argument("--force", action="store_true", help="overwrite an existing project")
    p_new.set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="advance the pipeline (resumes where it left off)")
    p_run.add_argument(
        "--until", choices=[s.value for s in StageName], default=None, help="stop after this stage"
    )
    p_run.add_argument(
        "--auto-accept",
        dest="auto_accept",
        action="store_true",
        help="approve NEEDS_REVIEW attributions and reject PENDING text suggestions, then continue",
    )
    p_run.add_argument(
        "--provider", choices=["claude", "lmstudio"], default=None, help="override the LLM provider"
    )
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="show per-stage status and review blockers")
    p_status.set_defaults(func=cmd_status)

    p_speakers = sub.add_parser("speakers", help="list discovered speakers and their voice state")
    p_speakers.set_defaults(func=cmd_speakers)

    p_assign = sub.add_parser(
        "assign-voice",
        help="assign a voice clip to a speaker, or --rest to voice the remaining cast",
    )
    p_assign.add_argument(
        "speaker",
        nargs="?",
        default=None,
        help="speaker list index or name (case-insensitive); omit with --rest",
    )
    p_assign.add_argument(
        "clip",
        nargs="?",
        default=None,
        help="path to the reference voice clip (read-only input); omit with --rest",
    )
    p_assign.add_argument(
        "--rest",
        action="store_true",
        help="assign category default clips to every still-unvoiced referenced speaker",
    )
    p_assign.add_argument("--man", default=None, help="default clip for `man` speakers (--rest)")
    p_assign.add_argument(
        "--woman", default=None, help="default clip for `woman` speakers (--rest)"
    )
    p_assign.add_argument("--boy", default=None, help="default clip for `boy` speakers (--rest)")
    p_assign.add_argument("--girl", default=None, help="default clip for `girl` speakers (--rest)")
    p_assign.add_argument(
        "--default",
        default=None,
        help="fallback clip for `unknown`/uncovered speakers (--rest)",
    )
    p_assign.set_defaults(func=cmd_assign_voice)

    return parser


def main(argv: list[str] | None = None, *, deps: CliDeps | None = None) -> int:
    """Parse ``argv``, dispatch to the subcommand, and return a process exit code."""
    deps = deps or CliDeps()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        parser.print_help()
        return _EXIT_ERROR

    try:
        return int(args.func(args, deps))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except CasttrophizerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())

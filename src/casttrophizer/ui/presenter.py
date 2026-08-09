"""The Qt-free presenter and the view/executor protocols it drives.

Model-View-Presenter split so the UI orchestration is unit-testable **without a Qt event
loop**. :class:`ProjectPresenter` imports no PySide6: it talks to the widget layer through the
:class:`ProjectView` protocol and to the background thread through the :class:`RunExecutor`
protocol. The real Qt implementations live in ``ui/main_window.py`` and ``ui/run_executor.py``;
tests substitute in-memory fakes for both.

All the domain work is delegated to :mod:`casttrophizer.app_service` (project open/create,
provider build + preflight, stage-status rows, run-outcome classification), so the presenter
is thin: it wires intents to service calls and pushes the results at the view.

Threading contract: every :class:`RunExecutor` callback (progress, finished, failed) is
invoked on the **main thread** (the Qt executor connects signals with the default queued/auto
connection), so the presenter never touches Qt and never races the worker — it only re-reads
the store *after* a terminal ``on_finished`` / ``on_failed``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from casttrophizer.app_service import (
    AppServiceDeps,
    RunOutcome,
    RunOutcomeKind,
    build_pipeline,
    build_providers,
    create_project,
    interpret_result,
    next_stage_name,
    open_project,
    stage_status_rows,
)
from casttrophizer.app_service.status import StageRow
from casttrophizer.audio.synthesize import RENDERED_STATUSES
from casttrophizer.config import AppConfig
from casttrophizer.domain.enums import StageName
from casttrophizer.domain.models import Project
from casttrophizer.errors import CasttrophizerError, ConfigError, PreconditionError
from casttrophizer.pipeline.runner import Pipeline
from casttrophizer.pipeline.stage import StageResult
from casttrophizer.providers import LLMProvider, TTSProvider
from casttrophizer.review.gate import describe_blockers, review_blockers
from casttrophizer.review.service import ReviewService
from casttrophizer.workspace.audio_cache import AudioCache
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["ProjectView", "RunExecutor", "ProjectPresenter"]


def _has_segments(project: Project) -> bool:
    """True iff any line carries at least one segment (attribution has produced review work)."""
    return any(ln.segments for ch in project.book.chapters for ln in ch.lines)


def _has_rendered_audio(project: Project) -> bool:
    """True iff any segment is in :data:`RENDERED_STATUSES` (synthesize produced a reviewable take).

    Gates the 'Review audio' entry point: audio review only opens once at least one segment has
    been synthesized (COMPLETED or already-APPROVED). A FAILED-only project has nothing to audition
    yet, so it stays closed until a re-run renders something.
    """
    return any(
        seg.audio_status in RENDERED_STATUSES
        for ch in project.book.chapters
        for ln in ch.lines
        for seg in ln.segments
    )


def _has_suggestions(project: Project) -> bool:
    """True iff any line carries a text suggestion (text review has work to surface).

    Suggestions appear after the correct stage — **before** attribute produces segments — so
    this (not ``_has_segments``) gates the 'Review text' entry: the user can triage text as
    soon as corrections land. Resolved suggestions still count so the panel stays reachable for
    a free-text line edit.
    """
    return any(ln.suggestions for ch in project.book.chapters for ln in ch.lines)


@runtime_checkable
class ProjectView(Protocol):
    """What the presenter needs from the window — a dumb, render-only surface.

    The view holds no business logic; it only renders what the presenter pushes and emits
    intent callbacks (Open / New / Run / Stop) the app wires to presenter methods.
    """

    def show_project_loaded(self, name: str, workspace: str) -> None:
        """Show the loaded project's name + workspace path in the header."""
        ...

    def show_stage_rows(self, rows: list[StageRow]) -> None:
        """Render the six per-stage status rows."""
        ...

    def show_next_stage(self, name: StageName | None) -> None:
        """Show which stage the next run would execute (``None`` => complete)."""
        ...

    def set_review_available(self, available: bool) -> None:
        """Enable/disable the 'Review attributions' entry point (true once segments exist)."""
        ...

    def set_voice_available(self, available: bool) -> None:
        """Enable/disable the 'Assign voices' entry point (true once referenced speakers exist)."""
        ...

    def set_text_available(self, available: bool) -> None:
        """Enable/disable the 'Review text' entry point (true once suggestions exist)."""
        ...

    def set_audio_available(self, available: bool) -> None:
        """Enable/disable the 'Review audio' entry point (true once a segment is rendered)."""
        ...

    def set_running(self, running: bool) -> None:
        """Toggle the running state (enables Stop, disables Run/Open/New, etc.)."""
        ...

    def set_progress(self, done: int, total: int, message: str) -> None:
        """Update the progress bar (``done``/``total``) and append a status ``message``."""
        ...

    def show_outcome(self, outcome: RunOutcome) -> None:
        """Render a terminal run outcome (completed / needs-review / stopped / failed)."""
        ...

    def show_error(self, title: str, message: str) -> None:
        """Surface a recoverable error (bad open, unavailable provider, worker crash)."""
        ...


@runtime_checkable
class RunExecutor(Protocol):
    """Runs one pipeline pass off the main thread, delivering results via main-thread callbacks.

    :meth:`start` builds the reporter + :class:`StageContext` and drives the worker; every
    callback it invokes (the three progress hooks plus ``on_finished`` / ``on_failed``) fires
    on the **main thread**. :meth:`request_stop` asks the running stage to stop cooperatively.
    """

    def start(
        self,
        pipeline: Pipeline,
        store: WorkspaceStore,
        llm: LLMProvider,
        tts: TTSProvider,
        config: AppConfig,
        *,
        on_total: Callable[[int], None],
        on_advance: Callable[[int, str], None],
        on_message: Callable[[str], None],
        on_finished: Callable[[StageResult], None],
        on_failed: Callable[[str], None],
    ) -> None:
        """Start a run; providers + config are built on the main thread and handed to the worker."""
        ...

    def request_stop(self) -> None:
        """Request a cooperative stop of the in-flight run (thread-safe)."""
        ...


class ProjectPresenter:
    """Orchestrates open/create/run/stop against a :class:`ProjectView` + :class:`RunExecutor`.

    Holds the loaded ``(store, project)`` and the pipeline; derives view-models via
    :mod:`casttrophizer.app_service`. Contains no Qt and no threading primitives — the injected
    executor owns the thread, and its callbacks land on the main thread.
    """

    def __init__(
        self,
        view: ProjectView,
        executor: RunExecutor,
        deps: AppServiceDeps,
    ) -> None:
        self._view = view
        self._executor = executor
        self._deps = deps
        self._store: WorkspaceStore | None = None
        # The single, shared review service (over one in-memory Project) handed to all three
        # review panels. Rebuilt from disk on project load / run finish so a snapshot never
        # diverges — one Project, one save path, clobber impossible by construction.
        self._service: ReviewService | None = None
        self._pipeline: Pipeline = build_pipeline(deps)
        self._final: StageName = self._pipeline.stages[-1].name
        # Progress accumulation (advance emits deltas; the bar needs a running total).
        self._done = 0
        self._total = 0
        # The kind of the last terminal outcome, so ``refresh_after_review`` knows whether the
        # shell is currently showing a NEEDS_REVIEW blocker summary to re-render live.
        self._last_outcome_kind: RunOutcomeKind | None = None
        # Optional "a project (re)loaded" hook — ``ui/app.py`` wires it to attach the shared
        # ``ReviewService`` to all three review panels so they edit one Project by reference.
        self.on_project_loaded: Callable[[ReviewService], None] | None = None

    @property
    def service(self) -> ReviewService | None:
        """The shared :class:`ReviewService`, or ``None`` before a project is opened/created.

        All three review panels edit this one service (by reference), so there are no per-panel
        snapshots to diverge and no whole-project write can clobber another panel's edit. It is
        rebuilt from disk at the two authoritative points (project load, run finish).
        """
        return self._service

    @property
    def audio_cache(self) -> AudioCache | None:
        """The workspace :class:`AudioCache`, or ``None`` before a project is opened/created.

        Built from ``store.layout`` (the store exposes it), so the audio-review panel can resolve
        per-segment WAV paths + existence for audition and the regenerate render target. ``None``
        until a store is adopted; ``ui/app.py`` re-passes it on every project load / run finish.
        """
        return AudioCache(self._store.layout) if self._store is not None else None

    # -- intents ------------------------------------------------------------ #
    def open(self, workspace_dir: str | Path) -> None:
        """Open an existing project from ``workspace_dir``; error if none is present."""
        try:
            store, _ = open_project(Path(workspace_dir))
        except CasttrophizerError as exc:
            self._view.show_error("Open failed", str(exc))
            return
        self._adopt(store)

    def create(
        self,
        epub: str | Path,
        name: str | None = None,
        *,
        overwrite: bool = False,
    ) -> None:
        """Create a new project from ``epub`` under the configured workspaces root.

        The workspace directory is ``config.workspaces_root / <name-or-epub-stem>``. Validation
        (missing file, unsupported format, existing project) surfaces as a
        :meth:`ProjectView.show_error`; a duplicate is recoverable by re-invoking with
        ``overwrite=True``.
        """
        epub_path = Path(epub)
        config = self._deps.resolved_config()
        workspace_dir = config.workspaces_root / (name or epub_path.stem)
        try:
            store, _ = create_project(epub_path, name, workspace_dir, config, overwrite=overwrite)
        except CasttrophizerError as exc:
            self._view.show_error("New project failed", str(exc))
            return
        self._adopt(store)

    def start_run(self) -> None:
        """Build providers (with the LLM preflight), then start the worker; refuse if unloaded.

        The UI always runs the full pipeline (``until=None``), so the LLM key is preflighted
        whenever attribution is still pending. An unavailable/misconfigured provider surfaces as
        a friendly error and the worker is **not** started.
        """
        if self._store is None:
            self._view.show_error("No project", "Open or create a project first.")
            return

        config = self._deps.resolved_config()
        project = self._store.load()
        by_name = {stage.name: stage for stage in self._pipeline.stages}
        attribute_pending = not by_name[StageName.ATTRIBUTE].is_complete(project)

        try:
            llm, tts = build_providers(
                self._deps, config, provider=None, preflight=attribute_pending
            )
        except (PreconditionError, ConfigError) as exc:
            self._view.show_error("Provider unavailable", str(exc))
            return

        self._done = 0
        self._total = 0
        self._view.set_running(True)
        self._view.set_progress(0, 0, "")
        self._executor.start(
            self._pipeline,
            self._store,
            llm,
            tts,
            config,
            on_total=self._on_total,
            on_advance=self._on_advance,
            on_message=self._on_message,
            on_finished=self._on_finished,
            on_failed=self._on_failed,
        )

    def stop_run(self) -> None:
        """Request a cooperative stop of the in-flight run (the stage saves partial state)."""
        self._executor.request_stop()

    # -- executor callbacks (main thread) ----------------------------------- #
    def _on_total(self, total: int) -> None:
        self._total = total
        self._done = 0
        self._view.set_progress(self._done, self._total, "")

    def _on_advance(self, delta: int, message: str) -> None:
        self._done += delta
        self._view.set_progress(self._done, self._total, message)

    def _on_message(self, message: str) -> None:
        self._view.set_progress(self._done, self._total, message)

    def _on_finished(self, result: StageResult) -> None:
        """A run pass finished: refresh status and render the classified outcome."""
        self._view.set_running(False)
        self._refresh_status()
        assert self._store is not None  # a run cannot start without a loaded store
        outcome = interpret_result(result, self._store, until=None, final=self._final)
        self._last_outcome_kind = outcome.kind
        self._view.show_outcome(outcome)
        # A run can add segments (attribute) or resolve blockers; rebuild the shared service from
        # disk and re-attach every review panel so none edits pre-run state.
        self._rebuild_service()
        self._notify_project_loaded()

    def _on_failed(self, message: str) -> None:
        """The worker raised (not a stage FAILED result): refresh status and show the error.

        A failed run may have partially mutated ``project.json`` before raising, so — like
        ``_on_finished`` — rebuild the shared service from disk and re-attach the panels; leaving
        the pre-run snapshot in place would let a later edit clobber whatever the run did persist.
        """
        self._view.set_running(False)
        self._refresh_status()
        self._view.show_error("Run failed", message)
        self._rebuild_service()
        self._notify_project_loaded()

    # -- review-panel integration ------------------------------------------- #
    def refresh_after_review(self) -> None:
        """Re-read the store after an attribution edit and tick the shell live.

        Wired as ``AttributionPresenter.on_reviewed``: it re-pushes the stage rows / next-stage
        pointer / review availability, and — if the shell is currently showing a NEEDS_REVIEW
        outcome — re-renders the blocker summary from the freshly-saved project so the "N
        attributions" line drops as the user resolves items (even while page 1 is showing).
        """
        if self._service is None:
            return
        self._refresh_status()
        if self._last_outcome_kind == RunOutcomeKind.NEEDS_REVIEW:
            blockers = review_blockers(self._service.project)
            self._view.show_outcome(
                RunOutcome(
                    kind=RunOutcomeKind.NEEDS_REVIEW,
                    blockers=blockers,
                    summary=describe_blockers(blockers),
                )
            )

    # -- helpers ------------------------------------------------------------ #
    def _adopt(self, store: WorkspaceStore) -> None:
        """Adopt a freshly opened/created project and push its header + status to the view."""
        self._store = store
        self._last_outcome_kind = None
        project = store.load()
        self._view.show_project_loaded(project.name, project.workspace_dir)
        self._refresh_status()
        self._rebuild_service()
        self._notify_project_loaded()

    def _rebuild_service(self) -> None:
        """Rebuild the shared :class:`ReviewService` from a freshly-loaded snapshot.

        Called at the authoritative points (open/create in :meth:`_adopt`, and both terminal
        run callbacks). The pre-run in-memory ``Project`` is stale after a run mutates
        ``project.json``, so rebuilding from disk is mandatory before re-attaching the panels.
        """
        assert self._store is not None
        self._service = ReviewService(self._store, self._store.load())

    def _notify_project_loaded(self) -> None:
        """Hand the shared service to the review panels (if the app wired the hook)."""
        if self.on_project_loaded is not None and self._service is not None:
            self.on_project_loaded(self._service)

    def _refresh_status(self) -> None:
        """Re-read the project and push its stage rows + next-stage + review/voice/text avail."""
        assert self._store is not None
        project = self._store.load()
        self._view.show_stage_rows(stage_status_rows(project, self._pipeline))
        self._view.show_next_stage(next_stage_name(project, self._pipeline))
        # A project with segments always has a referenced narrator, so the same predicate gates
        # both entry points: once attribution has produced review work, there is a cast to voice.
        has_segments = _has_segments(project)
        self._view.set_review_available(has_segments)
        self._view.set_voice_available(has_segments)
        # Text review opens earlier — as soon as the correct stage lands suggestions.
        self._view.set_text_available(_has_suggestions(project))
        # Audio review opens later — only once synthesize has produced a reviewable take.
        self._view.set_audio_available(_has_rendered_audio(project))

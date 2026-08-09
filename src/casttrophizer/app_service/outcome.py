"""Classify a finished run into a structured, front-end-neutral :class:`RunOutcome`.

This is the classification half of the old ``cli._report_run`` — pure, with no printing and
no exit codes. Both front-ends call :func:`interpret_result` and then present the result their
own way (CLI: stdout + exit code; UI: an outcome panel). The distinction the CLI cared about —
a ``--until`` early stop looks like COMPLETED but is not a full-pipeline completion — is kept
here for parity, mapped to :attr:`RunOutcomeKind.STOPPED`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from casttrophizer.domain.enums import ReviewStatus, StageName
from casttrophizer.pipeline.stage import StageResult
from casttrophizer.review.gate import ReviewBlockers, describe_blockers, review_blockers
from casttrophizer.workspace.store import WorkspaceStore

__all__ = ["RunOutcomeKind", "RunOutcome", "interpret_result"]


class RunOutcomeKind(StrEnum):
    """The four terminal shapes a run pass can land in."""

    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class RunOutcome:
    """A classified run result, with only the fields relevant to its :attr:`kind` populated."""

    kind: RunOutcomeKind
    output_path: Path | None = None  # COMPLETED: the produced .m4b, if one is on disk
    blockers: ReviewBlockers | None = None  # NEEDS_REVIEW: what still blocks review
    summary: str = ""  # one-line human summary (blockers / stopped / failed text)
    message: str = ""  # FAILED: the stage's failure message


def interpret_result(
    result: StageResult,
    store: WorkspaceStore,
    *,
    until: StageName | None,
    final: StageName,
) -> RunOutcome:
    """Map a stage :class:`StageResult` to a :class:`RunOutcome`.

    A COMPLETED status is a *full-pipeline* completion only when the finished stage is the
    final stage. With ``until`` set to a pre-synthesize stage the runner returns COMPLETED for
    the stage it stopped at — that is an early stop, not "the audiobook is done", so it maps to
    :attr:`RunOutcomeKind.STOPPED`. The UI always runs with ``until=None``, so it never hits the
    early-stop branch; it is kept for CLI parity.
    """
    if result.status == ReviewStatus.COMPLETED:
        if until is not None and result.stage != final:
            return RunOutcome(
                kind=RunOutcomeKind.STOPPED,
                summary=f"stopped after {result.stage.value}",
            )
        m4bs = sorted(store.layout.output_dir.glob("*.m4b"))
        return RunOutcome(
            kind=RunOutcomeKind.COMPLETED,
            output_path=m4bs[0] if m4bs else None,
            summary="complete",
        )

    if result.status == ReviewStatus.NEEDS_REVIEW:
        blockers = review_blockers(store.load())
        return RunOutcome(
            kind=RunOutcomeKind.NEEDS_REVIEW,
            blockers=blockers,
            summary=describe_blockers(blockers),
        )

    if result.status == ReviewStatus.STOPPED:
        return RunOutcome(kind=RunOutcomeKind.STOPPED, summary="stopped")

    return RunOutcome(
        kind=RunOutcomeKind.FAILED,
        message=result.message,
        summary=f"failed: {result.message}",
    )

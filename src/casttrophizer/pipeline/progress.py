"""Framework-agnostic progress reporting.

Stages report progress through a :class:`ProgressReporter` injected via the
``StageContext`` — they never import Qt. The Qt adapter
(:class:`casttrophizer.ui.workers.QtProgressReporter`) implements this same protocol by
emitting signals, so the pipeline stays headless-testable while the UI still gets live
updates. ``should_stop`` is how a stage learns a cooperative stop was requested.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["ProgressReporter", "NullProgressReporter"]


@runtime_checkable
class ProgressReporter(Protocol):
    """A sink for progress events plus a cooperative-stop flag.

    Stages call :meth:`set_total` once they know the unit count, :meth:`advance` as work
    completes, :meth:`message` for status text, and poll :meth:`should_stop` at safe
    checkpoints.
    """

    def set_total(self, total: int) -> None:
        """Declare the total number of work units for the current stage."""
        ...

    def advance(self, n: int = 1, *, message: str | None = None) -> None:
        """Advance progress by ``n`` units, optionally with a status ``message``."""
        ...

    def message(self, text: str) -> None:
        """Emit a status message without advancing progress."""
        ...

    def should_stop(self) -> bool:
        """True if the caller requested a cooperative stop; stages poll this."""
        ...


class NullProgressReporter:
    """No-op reporter for headless/non-interactive runs and tests. Never stops."""

    def set_total(self, total: int) -> None:  # noqa: D102 - trivial no-op
        pass

    def advance(self, n: int = 1, *, message: str | None = None) -> None:  # noqa: D102
        pass

    def message(self, text: str) -> None:  # noqa: D102 - trivial no-op
        pass

    def should_stop(self) -> bool:  # noqa: D102 - never requests a stop
        return False

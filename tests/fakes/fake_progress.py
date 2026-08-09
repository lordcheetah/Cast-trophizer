"""Recording progress reporter for tests.

Captures ``advance``/``message`` calls and exposes a controllable ``should_stop`` so
stop/resume behaviour can be tested without Qt.
"""

from __future__ import annotations

__all__ = ["RecordingProgressReporter"]


class RecordingProgressReporter:
    """Records progress calls and lets a test toggle the cooperative-stop flag."""

    def __init__(self, *, stop: bool = False) -> None:
        self.total: int | None = None
        self.advances: list[tuple[int, str | None]] = []
        self.messages: list[str] = []
        self._stop = stop

    def set_total(self, total: int) -> None:
        self.total = total

    def advance(self, n: int = 1, *, message: str | None = None) -> None:
        self.advances.append((n, message))

    def message(self, text: str) -> None:
        self.messages.append(text)

    def should_stop(self) -> bool:
        return self._stop

    def request_stop(self) -> None:
        """Set the stop flag so the next ``should_stop`` poll returns ``True``."""
        self._stop = True

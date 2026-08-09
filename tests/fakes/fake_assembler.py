"""Offline fake M4B assembler for tests.

Records the :class:`~casttrophizer.audio.assembler.AssemblyRequest` it was handed (ordered
segment WAV paths, chapter markers, cover path, out_path) and writes a tiny stub output file
**without invoking ffmpeg or mutagen**. This is the seam the assemble-stage tests assert
against — the real ffmpeg/mutagen path is ``# VERIFY:`` and never runs in CI.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.audio.assembler import AssemblyRequest
from casttrophizer.errors import AssemblyError

__all__ = ["FakeM4BAssembler"]


class FakeM4BAssembler:
    """Records requests and writes a placeholder M4B instead of shelling out to ffmpeg.

    ``requests`` exposes every :class:`AssemblyRequest` (in order) so tests can assert on
    ordering / markers / cover / out_path. ``available`` drives :meth:`is_available` (the
    "ffmpeg missing" path); ``fail`` makes :meth:`assemble` raise :class:`AssemblyError`
    (the assembler-failure path) without writing an output file.
    """

    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
        self._available = available
        self._fail = fail
        self.requests: list[AssemblyRequest] = []

    def is_available(self) -> bool:
        return self._available

    def assemble(self, request: AssemblyRequest) -> Path:
        self.requests.append(request)
        if self._fail:
            # Mirror the real assembler: a failure leaves no file at out_path.
            raise AssemblyError("fake assembly failure")
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        request.out_path.write_bytes(b"FAKE-M4B")
        return request.out_path

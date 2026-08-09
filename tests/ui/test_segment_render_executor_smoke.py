"""Offscreen smoke tests for :class:`QtSegmentRenderExecutor` — mock the render, no GPU/TTS.

The actual :func:`~casttrophizer.audio.synthesize.render_segment` is monkeypatched (CI has no
Chatterbox/audio device), so these assert the *threading contract* only: a fresh single-shot thread
per render, both callbacks delivered on the **main thread**, clean teardown (no orphaned thread), a
failure forwarded to ``on_failed``, and the in-flight guard. Queued signals are pumped with
``QApplication.processEvents`` since there is no running event loop.
"""

from __future__ import annotations

import threading

import pytest

from casttrophizer.errors import TTSProviderError
from casttrophizer.ui import segment_render_executor as executor_module
from casttrophizer.ui.segment_render_executor import QtSegmentRenderExecutor

pytestmark = pytest.mark.usefixtures("qapp")


def _pump(predicate: object, *, timeout_ms: int = 5000) -> None:
    """Pump the event loop until ``predicate()`` is truthy or the timeout elapses."""
    from PySide6.QtCore import QDeadlineTimer
    from PySide6.QtWidgets import QApplication

    deadline = QDeadlineTimer(timeout_ms)
    while not predicate() and not deadline.hasExpired():  # type: ignore[operator]
        QApplication.processEvents()


def _run_one(
    monkeypatch: pytest.MonkeyPatch,
    render: object,
) -> tuple[list[str], list[str], list[int]]:
    """Start one render with ``render`` swapped in; return (finished, failed, main-thread flags).

    ``failed`` holds ``(message, provider_unavailable)`` tuples so tests can assert the failure-kind
    flag the worker carries across the signal.
    """
    monkeypatch.setattr(executor_module, "render_segment", render)
    executor = QtSegmentRenderExecutor()
    finished: list[str] = []
    failed: list[tuple[str, bool]] = []
    main_thread = threading.get_ident()
    on_main: list[int] = []

    def on_finished() -> None:
        on_main.append(threading.get_ident() == main_thread)
        finished.append("ok")

    def on_failed(msg: str, provider_unavailable: bool) -> None:
        on_main.append(threading.get_ident() == main_thread)
        failed.append((msg, provider_unavailable))

    executor.start(
        object(),  # tts (unused — render is mocked)
        object(),  # project
        object(),  # segment
        object(),  # cache
        loudness=None,
        on_finished=on_finished,
        on_failed=on_failed,
    )
    _pump(lambda: bool(finished) or bool(failed))
    return finished, failed, on_main


def test_success_fires_on_finished_on_the_main_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    finished, failed, on_main = _run_one(monkeypatch, lambda *a, **k: None)
    assert finished == ["ok"]
    assert failed == []
    assert on_main == [True]  # callback ran on the main thread


def test_provider_error_fires_on_failed_with_unavailable_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise TTSProviderError("model load failed")

    finished, failed, on_main = _run_one(monkeypatch, _boom)
    assert finished == []
    assert failed == [("model load failed", True)]  # provider unavailable -> permanent degrade
    assert on_main == [True]


def test_transient_error_fires_on_failed_with_unavailable_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("CUDA out of memory")

    finished, failed, _ = _run_one(monkeypatch, _boom)
    assert finished == []
    assert failed == [("CUDA out of memory", False)]  # retryable -> Regenerate stays enabled


def test_thread_is_torn_down_after_each_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_module, "render_segment", lambda *a, **k: None)
    executor = QtSegmentRenderExecutor()
    done: list[str] = []

    def start_once() -> None:
        executor.start(
            object(),
            object(),
            object(),
            object(),
            loudness=None,
            on_finished=lambda: done.append("ok"),
            on_failed=lambda _m, _u: done.append("fail"),
        )

    start_once()
    _pump(lambda: len(done) == 1)
    assert executor._thread is None  # torn down: a fresh thread is built per render
    # A second render on the same executor works (proves the single-shot teardown was clean).
    start_once()
    _pump(lambda: len(done) == 2)
    assert done == ["ok", "ok"]
    assert executor._thread is None


def test_second_start_while_in_flight_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A render that blocks until released, so the executor stays in-flight for the guard assertion.
    gate = threading.Event()

    def _blocking(*_a: object, **_k: object) -> None:
        gate.wait(timeout=5)

    monkeypatch.setattr(executor_module, "render_segment", _blocking)
    executor = QtSegmentRenderExecutor()
    executor.start(
        object(),
        object(),
        object(),
        object(),
        loudness=None,
        on_finished=lambda: None,
        on_failed=lambda _m, _u: None,
    )
    try:
        with pytest.raises(RuntimeError):
            executor.start(
                object(),
                object(),
                object(),
                object(),
                loudness=None,
                on_finished=lambda: None,
                on_failed=lambda _m, _u: None,
            )
    finally:
        gate.set()
        _pump(lambda: executor._thread is None)

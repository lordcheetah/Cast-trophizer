"""Unit tests for the graceful-degradation probe ``app_service.tts_extra_available``.

It must return a bool via ``importlib.util.find_spec`` without importing Chatterbox/torch, and
degrade to ``False`` (never raise) when the probe itself errors on a broken partial install.
"""

from __future__ import annotations

import pytest

from casttrophizer.app_service import deps as deps_module
from casttrophizer.app_service import tts_extra_available


def test_returns_bool() -> None:
    assert isinstance(tts_extra_available(), bool)


def test_true_when_spec_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps_module.importlib.util, "find_spec", lambda _name: object())
    assert tts_extra_available() is True


def test_false_when_spec_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps_module.importlib.util, "find_spec", lambda _name: None)
    assert tts_extra_available() is False


def test_false_when_find_spec_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_name: str) -> object:
        raise ValueError("broken partial install")

    monkeypatch.setattr(deps_module.importlib.util, "find_spec", _boom)
    assert tts_extra_available() is False

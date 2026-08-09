"""A manual, session-scoped offscreen ``QApplication`` fixture for the Qt smoke tests.

PySide6 is already a core dependency, so we avoid adding ``pytest-qt`` to the ``dev`` extra:
one headless ``QApplication`` (``QT_QPA_PLATFORM=offscreen``) is enough for the handful of
widget smoke tests. The behavioral coverage lives in the loop-free presenter tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    """Yield a single offscreen :class:`QApplication` for the whole test session."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()

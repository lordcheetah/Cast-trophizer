"""PySide6 UI layer — the ONLY package that imports Qt.

Keeping Qt confined here is what lets the pipeline, providers, domain, and workspace
packages stay headless-testable. A lint-guard test asserts no ``PySide6``/``PyQt``
import appears outside this package.
"""

from __future__ import annotations

__all__: list[str] = []

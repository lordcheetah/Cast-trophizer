"""Guard the threading rule: only ``ui/`` may import Qt.

Scans every ``.py`` file under ``src/casttrophizer`` and asserts that no module outside
the ``ui`` package imports ``PySide6`` or ``PyQt``. This keeps the pipeline, providers,
domain, and workspace packages headless-testable (no display, no ``QApplication``).
"""

from __future__ import annotations

import re
from pathlib import Path

import casttrophizer

_SRC_ROOT = Path(casttrophizer.__file__).resolve().parent
_QT_IMPORT = re.compile(r"^\s*(?:import|from)\s+(PySide6|PyQt5|PyQt6)\b", re.MULTILINE)


def _python_files_outside_ui() -> list[Path]:
    files: list[Path] = []
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT)
        if rel.parts and rel.parts[0] == "ui":
            continue
        files.append(path)
    return files


def test_no_qt_import_outside_ui() -> None:
    offenders: list[str] = []
    for path in _python_files_outside_ui():
        text = path.read_text(encoding="utf-8")
        if _QT_IMPORT.search(text):
            offenders.append(str(path.relative_to(_SRC_ROOT)))
    assert offenders == [], f"Qt imported outside ui/: {offenders}"


def test_importing_pipeline_does_not_load_qt() -> None:
    import sys

    import casttrophizer.pipeline  # noqa: F401

    assert "PySide6" not in sys.modules

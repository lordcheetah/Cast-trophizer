"""Guard the threading rule: only ``ui/`` may import Qt.

Scans every ``.py`` file under ``src/casttrophizer`` and asserts that no module outside
the ``ui`` package imports ``PySide6`` or ``PyQt``. This keeps the pipeline, providers,
domain, and workspace packages headless-testable (no display, no ``QApplication``).
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
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
    """Importing the headless pipeline must not pull in Qt.

    Runs in a **fresh interpreter** (not an in-process ``sys.modules`` check): now that the
    UI smoke tests legitimately load ``PySide6`` into the shared pytest session, an in-process
    assertion would be order-dependent. A clean child process removes that coupling — the same
    subprocess rationale documented in ``test_lazy_imports.py`` (which also asserts this exact
    ``pipeline`` -> no ``PySide6`` case).
    """
    code = textwrap.dedent("""
        import sys
        import casttrophizer.pipeline  # noqa: F401
        if "PySide6" in sys.modules:
            sys.stderr.write("PySide6 was imported as a side effect of importing pipeline\\n")
            raise SystemExit(1)
        raise SystemExit(0)
        """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"pipeline import must not load PySide6.\nstderr:\n{proc.stderr}"

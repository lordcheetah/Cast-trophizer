"""Import-cheapness / Qt-isolation for the CLI module.

Importing ``casttrophizer.cli`` must not drag in Qt or any heavy/optional provider SDK —
the CLI builds providers lazily, only when a stage actually needs one. The existing
``tests/test_qt_isolation.py`` already scans all of ``src`` for Qt imports; this adds the
torch/anthropic/openai/chatterbox guarantees the source scan does not assert, each in a
fresh interpreter (mirrors ``tests/test_lazy_imports.py``).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

_FORBIDDEN = ["PySide6", "torch", "anthropic", "openai", "chatterbox"]


def _import_cli_then_check(forbidden: str) -> subprocess.CompletedProcess[str]:
    code = textwrap.dedent(f"""
        import sys
        import importlib
        importlib.import_module("casttrophizer.cli")
        if {forbidden!r} in sys.modules:
            sys.stderr.write("{forbidden} was imported by casttrophizer.cli\\n")
            raise SystemExit(1)
        raise SystemExit(0)
        """)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


@pytest.mark.parametrize("forbidden", _FORBIDDEN)
def test_importing_cli_loads_no_heavy_dep(forbidden: str) -> None:
    proc = _import_cli_then_check(forbidden)
    assert proc.returncode == 0, (
        f"importing casttrophizer.cli must not load {forbidden}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

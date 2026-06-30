"""Airtight lazy-load guarantees for the provider/pipeline/domain layers (scaffold §9.1).

The central architectural rule (CLAUDE.md + scaffold §1/§4): importing a concrete
provider module, or any headless package, must **not** drag in the heavy/optional SDKs
(``torch``, ``chatterbox``, ``anthropic``, ``openai``) or Qt (``PySide6``). Those are
loaded lazily — only when a provider actually runs.

Why a subprocess per import (and not just ``assert "torch" not in sys.modules``):

* ``anthropic``/``openai``/``PySide6`` are *installed* in this environment, so a stray
  top-level import would really land in ``sys.modules`` — but only a **fresh**
  interpreter proves the target module itself is responsible. In a shared pytest
  session another test could have already imported the SDK, masking a regression (false
  pass) or, conversely, an unrelated import could trip an in-process assertion (false
  fail). A clean child process removes that ordering coupling entirely.
* ``torch``/``chatterbox`` are *not* installed here, so an in-process
  ``not in sys.modules`` check passes trivially and proves nothing. The subprocess still
  imports the target module successfully (proving it does not hard-require torch) and
  then asserts the SDK never entered ``sys.modules`` — meaningful regardless of install
  state.

Each case runs ``python -c`` in an isolated interpreter; the child asserts and exits
nonzero on violation, and the parent surfaces the child's stderr on failure.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# (module-to-import, SDK-that-must-stay-absent) — the load-bearing guarantees.
_LAZY_IMPORT_CASES: list[tuple[str, str]] = [
    # Concrete LLM providers must not import their SDK at module load.
    ("casttrophizer.providers.llm.claude", "anthropic"),
    ("casttrophizer.providers.llm.lmstudio", "openai"),
    # The TTS provider must not import torch/chatterbox at module load.
    ("casttrophizer.providers.tts.chatterbox", "torch"),
    ("casttrophizer.providers.tts.chatterbox", "chatterbox"),
    # The factory package re-exports ABCs only — no concrete SDK should appear.
    ("casttrophizer.providers", "anthropic"),
    ("casttrophizer.providers", "openai"),
    ("casttrophizer.providers", "torch"),
    ("casttrophizer.providers", "chatterbox"),
    # Headless packages stay free of every heavy dep *and* of Qt.
    ("casttrophizer.pipeline", "torch"),
    ("casttrophizer.pipeline", "PySide6"),
    ("casttrophizer.pipeline.stages", "anthropic"),
    ("casttrophizer.pipeline.stages", "openai"),
    # The text-correction package must not load the spellcheck dictionary at import time
    # (the heavy ``spellchecker`` import is deferred into the corrector, like ebooklib).
    ("casttrophizer.text", "spellchecker"),
    ("casttrophizer.text.spelling", "spellchecker"),  # the module that *wraps* it
    ("casttrophizer.text.base", "spellchecker"),  # the policy seam stays dict-free
    ("casttrophizer.text.ocr", "spellchecker"),  # OCR heuristics take an injected is_word
    ("casttrophizer.pipeline.stages.correct", "spellchecker"),
    ("casttrophizer.workspace", "PySide6"),
    ("casttrophizer.domain", "PySide6"),
    ("casttrophizer.domain", "torch"),
]


def _import_in_clean_subprocess(module: str, forbidden: str) -> subprocess.CompletedProcess[str]:
    """Import ``module`` in a fresh interpreter and assert ``forbidden`` never loads."""
    code = textwrap.dedent(f"""
        import sys
        import importlib
        importlib.import_module({module!r})
        if {forbidden!r} in sys.modules:
            sys.stderr.write(
                "{forbidden} was imported as a side effect of importing {module}\\n"
            )
            raise SystemExit(1)
        raise SystemExit(0)
        """)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("module", "forbidden"),
    _LAZY_IMPORT_CASES,
    ids=[f"{m}__no__{f}" for m, f in _LAZY_IMPORT_CASES],
)
def test_module_import_does_not_load_heavy_dep(module: str, forbidden: str) -> None:
    proc = _import_in_clean_subprocess(module, forbidden)
    assert proc.returncode == 0, (
        f"importing {module} must not load {forbidden}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_subprocess_harness_actually_detects_a_violation() -> None:
    """Meta-check: the harness reports failure when the forbidden dep IS imported.

    Guards against a false-green harness (e.g. a typo making every case pass). We
    deliberately import a stdlib module (``json``) that the target genuinely loads, and
    confirm the child process flags it as present — proving the detection path works.
    Uses ``json`` rather than an optional SDK so this stays offline and install-agnostic.
    """
    proc = _import_in_clean_subprocess("casttrophizer.workspace.store", "json")
    assert proc.returncode == 1, (
        "harness failed to detect a dep that the target really imports; "
        "the lazy-import assertions may be silently passing.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

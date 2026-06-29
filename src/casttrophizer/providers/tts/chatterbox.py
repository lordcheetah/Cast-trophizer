"""Chatterbox TTS provider.

Chatterbox is gated behind the ``tts`` optional-extra and pulls heavy torch deps. Those
imports MUST stay out of module load: ``torch`` and ``chatterbox`` are imported
**lazily inside methods** (``_load_model`` / ``synthesize`` / ``is_available``), so
importing this module is cheap and CI — which installs *without* the ``tts`` extra —
never loads torch.

Distribution: ``chatterbox-tts``. Import path: ``from chatterbox.tts import ChatterboxTTS``.

Feature bodies are stubs (``raise NotImplementedError``) in this skeleton.
"""

from __future__ import annotations

from pathlib import Path

from casttrophizer.providers.base import SynthesisRequest, SynthesisResult, TTSProvider

__all__ = ["ChatterboxProvider"]


class ChatterboxProvider(TTSProvider):
    """TTS provider backed by Chatterbox voice cloning."""

    name = "chatterbox"

    def __init__(self, device: str | None = None) -> None:
        self._device = device
        self._model: object | None = None  # lazily loaded ChatterboxTTS instance

    def _load_model(self) -> object:
        """Lazily import + load Chatterbox (and torch). Heavy; only called on demand."""
        # ``chatterbox-tts`` is the optional ``tts`` extra; absent in dev/CI installs.
        from chatterbox.tts import ChatterboxTTS  # type: ignore[import-not-found]

        # Real implementation will load + cache the model here, e.g.
        # ``self._model = ChatterboxTTS.from_pretrained(device=self._device)``.
        del ChatterboxTTS
        raise NotImplementedError("ChatterboxProvider model loading is not yet implemented")

    def synthesize(self, request: SynthesisRequest, out_path: Path) -> SynthesisResult:
        raise NotImplementedError("ChatterboxProvider.synthesize is not yet implemented")

    def is_available(self) -> bool:
        """Report whether Chatterbox/torch are importable, without loading the model."""
        from importlib.util import find_spec

        return find_spec("chatterbox") is not None and find_spec("torch") is not None

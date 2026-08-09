"""Chatterbox TTS provider.

Chatterbox is gated behind the ``tts`` optional-extra and pulls heavy torch deps. Those
imports MUST stay out of module load: ``torch`` and ``chatterbox`` are imported
**lazily inside methods** (``_load_model`` / ``synthesize`` / ``is_available``), so
importing this module is cheap and CI — which installs *without* the ``tts`` extra —
never loads torch.

Distribution: ``chatterbox-tts``. Import path: ``from chatterbox.tts import ChatterboxTTS``.

Real implementation. Confirmed at runtime via ``scripts/smoke_test.py``:
``ChatterboxTTS.from_pretrained(device=...)`` model load, ``model.generate(text,
audio_prompt_path=<wav>)`` returning a float32 ``[1, N]`` tensor, and ``model.sr`` (24000).
The WAV is written with the stdlib ``wave`` module (NOT ``torchaudio.save``, which now
dispatches to TorchCodec — an extra native dep not in the ``tts`` extra). Remaining
``# VERIFY:`` items are the parts the smoke run did not exercise: the ``exaggeration`` /
``cfg_weight`` generate keywords (defaults only were used), seed determinism, and non-CUDA
device selection (MPS). The fake provider covers all stage tests; this code runs only on a
real render.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from casttrophizer.audio.wavfile import write_wav_mono16
from casttrophizer.errors import TTSProviderError
from casttrophizer.providers.base import SynthesisRequest, SynthesisResult, TTSProvider

__all__ = ["ChatterboxProvider"]


class ChatterboxProvider(TTSProvider):
    """TTS provider backed by Chatterbox voice cloning.

    The model is large (GPU/CPU memory) and is loaded **once** on first ``synthesize`` and
    cached on the instance for the life of the provider — correct for a single-process render.
    Construct one provider per render; dispose of it to release the model.
    """

    name = "chatterbox"

    def __init__(self, device: str | None = None) -> None:
        self._device = device  # None => auto (CUDA if available, else CPU); see _resolve_device
        self._model: Any | None = None  # lazily loaded ChatterboxTTS instance

    def _resolve_device(self) -> str:
        """Pick the torch device: explicit override, else CUDA when available, else CPU."""
        if self._device is not None:
            return self._device
        import torch  # lazy: torch must never load at module import

        # VERIFY: device selection — Chatterbox accepts a ``device`` string ("cuda"/"cpu").
        # Confirm CUDA availability detection and whether MPS (Apple) should be preferred.
        return "cuda" if torch.cuda.is_available() else "cpu"

    def _load_model(self) -> Any:
        """Lazily import + load Chatterbox (and torch). Heavy; loaded once, then cached."""
        if self._model is not None:
            return self._model
        # ``chatterbox-tts`` is the optional ``tts`` extra; absent in dev/CI installs.
        from chatterbox.tts import ChatterboxTTS

        device = self._resolve_device()
        try:
            # VERIFY: model load — ``ChatterboxTTS.from_pretrained(device=...)`` downloads/loads
            # the pretrained weights and returns the model. Confirm the classmethod name and the
            # ``device`` keyword against the installed package.
            self._model = ChatterboxTTS.from_pretrained(device=device)
        except Exception as exc:  # noqa: BLE001 - normalize any load failure to our error type
            raise TTSProviderError(f"failed to load Chatterbox model: {exc}") from exc
        return self._model

    def synthesize(self, request: SynthesisRequest, out_path: Path) -> SynthesisResult:
        """Render ``request.text`` in the reference voice to a WAV at ``out_path``.

        Maps ``request.params`` (``exaggeration``, ``cfg_weight``, ``seed``) onto Chatterbox's
        generate call and writes the resulting waveform to ``out_path``. Deterministic given
        the same ``seed`` so the audio cache key uniquely identifies the audio. Any failure is
        normalized to :class:`~casttrophizer.errors.TTSProviderError`.
        """
        import torch  # lazy

        model = self._load_model()
        params = request.params

        seed = params.get("seed")
        if seed is not None:
            # VERIFY: determinism — seeding torch (and CUDA) before generate. Confirm Chatterbox
            # has no separate RNG that also needs seeding for byte-identical output.
            torch.manual_seed(int(seed))
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(int(seed))

        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Confirmed at runtime (smoke test): ``model.generate(text, audio_prompt_path=<wav>)``
            # returns a float32 [1, N] torch tensor; the positional text arg and the
            # ``audio_prompt_path`` voice-prompt keyword are correct.
            # VERIFY (still unexercised): the ``exaggeration`` / ``cfg_weight`` keyword names —
            # they were not passed in the smoke run (only defaults). Confirm these keywords before
            # relying on those params.
            generate_kwargs: dict[str, Any] = {"audio_prompt_path": str(request.voice_clip_path)}
            if "exaggeration" in params:
                generate_kwargs["exaggeration"] = float(params["exaggeration"])
            if "cfg_weight" in params:
                generate_kwargs["cfg_weight"] = float(params["cfg_weight"])

            wav = model.generate(request.text, **generate_kwargs)

            # Confirmed at runtime: Chatterbox exposes the output sample rate as ``model.sr``
            # (24000) and ``generate`` returns a float32 [1, N] torch tensor.
            sample_rate = int(model.sr)

            # Flatten to a mono float list and write a 16-bit PCM WAV with the stdlib (see
            # audio.wavfile.write_wav_mono16 for why torchaudio.save is avoided).
            tensor = wav if hasattr(wav, "detach") else torch.as_tensor(wav)
            samples: list[float] = tensor.detach().cpu().reshape(-1).tolist()
            write_wav_mono16(out_path, samples, sample_rate)

            num_samples = len(samples)
        except TTSProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize any synth failure to our error type
            raise TTSProviderError(f"Chatterbox synthesis failed: {exc}") from exc

        duration_s = num_samples / sample_rate if sample_rate else 0.0
        return SynthesisResult(
            audio_path=out_path,
            sample_rate=sample_rate,
            duration_s=duration_s,
        )

    def is_available(self) -> bool:
        """Report whether Chatterbox/torch are importable, without loading the model."""
        from importlib.util import find_spec

        return find_spec("chatterbox") is not None and find_spec("torch") is not None

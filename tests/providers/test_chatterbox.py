"""ChatterboxProvider offline unit tests.

The real Chatterbox ``generate`` call and model load are ``# VERIFY:``-marked and can only
be confirmed against the installed ``tts`` extra on a torch/GPU machine — they are **never**
exercised against a real model here. What *is* deterministically testable offline is the
provider's:

* **lazy-import discipline** — ``is_available`` uses ``find_spec`` and never loads torch;
  ``synthesize``/``_load_model`` import torch/chatterbox only when called;
* **param mapping** — ``request.params`` (``exaggeration``/``cfg_weight``/``seed``) is mapped
  onto the generate call and the seed is honored;
* **WAV write + result population** — a waveform is written to ``out_path`` and
  ``sample_rate``/``duration_s`` are computed from the model output;
* **error normalization** — a model/generate failure becomes :class:`TTSProviderError`.

These are verified by injecting *fake* ``chatterbox.tts`` / ``torch`` / ``torchaudio`` modules
into ``sys.modules`` for the duration of the test, so the provider's own logic runs while the
heavy SDKs never load. The fakes are removed afterwards. No real model, torch, or network.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from casttrophizer.errors import TTSProviderError
from casttrophizer.providers.base import SynthesisRequest
from casttrophizer.providers.tts.chatterbox import ChatterboxProvider


# --------------------------------------------------------------------------- #
# fake torch / torchaudio / chatterbox.tts modules (offline stand-ins)
# --------------------------------------------------------------------------- #
class _FakeTensor:
    """Minimal tensor stand-in supporting the ops ``synthesize`` uses on the waveform."""

    def __init__(self, samples: list[float], dims: int = 1) -> None:
        self._samples = samples
        self._dims = dims

    def dim(self) -> int:
        return self._dims

    def unsqueeze(self, _axis: int) -> _FakeTensor:
        return _FakeTensor(self._samples, dims=self._dims + 1)

    def cpu(self) -> _FakeTensor:
        return self

    @property
    def shape(self) -> tuple[int, ...]:
        # last dim is sample count, matching ``tensor.shape[-1]`` in synthesize
        return (1, len(self._samples)) if self._dims == 2 else (len(self._samples),)


class _FakeModel:
    """Stand-in ChatterboxTTS: records the generate call and returns a canned waveform."""

    def __init__(self, sr: int = 24000, *, raise_on_generate: bool = False) -> None:
        self.sr = sr
        self._raise = raise_on_generate
        self.generate_calls: list[tuple[str, dict[str, Any]]] = []

    def generate(self, text: str, **kwargs: Any) -> _FakeTensor:
        self.generate_calls.append((text, kwargs))
        if self._raise:
            raise RuntimeError("model exploded")
        return _FakeTensor([0.0, 0.1, -0.1, 0.0])  # 4 samples, 1-D


def _install_fake_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: _FakeModel,
    cuda_available: bool = False,
    from_pretrained_raises: bool = False,
) -> dict[str, Any]:
    """Inject fake ``chatterbox.tts``, ``torch``, ``torchaudio`` modules; return recorders."""
    recorder: dict[str, Any] = {"saved": None, "manual_seed": None, "cuda_seed": None}

    # --- fake torch ---
    fake_torch = types.ModuleType("torch")

    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return cuda_available

        @staticmethod
        def manual_seed_all(seed: int) -> None:
            recorder["cuda_seed"] = seed

    fake_torch.cuda = _Cuda()  # type: ignore[attr-defined]

    def _manual_seed(seed: int) -> None:
        recorder["manual_seed"] = seed

    fake_torch.manual_seed = _manual_seed  # type: ignore[attr-defined]
    fake_torch.as_tensor = lambda x: _FakeTensor(list(x))  # type: ignore[attr-defined]

    # --- fake torchaudio ---
    fake_torchaudio = types.ModuleType("torchaudio")

    def _save(path: str, tensor: Any, sample_rate: int) -> None:
        recorder["saved"] = (path, tensor, sample_rate)
        Path(path).write_bytes(b"RIFF....WAVEfake")  # write *something* so is_file() passes

    fake_torchaudio.save = _save  # type: ignore[attr-defined]

    # --- fake chatterbox.tts ---
    fake_chatterbox = types.ModuleType("chatterbox")
    fake_chatterbox_tts = types.ModuleType("chatterbox.tts")

    class _ChatterboxTTS:
        @staticmethod
        def from_pretrained(device: str) -> _FakeModel:
            recorder["device"] = device
            if from_pretrained_raises:
                raise RuntimeError("download failed")
            return model

    fake_chatterbox_tts.ChatterboxTTS = _ChatterboxTTS  # type: ignore[attr-defined]
    fake_chatterbox.tts = fake_chatterbox_tts  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "torchaudio", fake_torchaudio)
    monkeypatch.setitem(sys.modules, "chatterbox", fake_chatterbox)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", fake_chatterbox_tts)
    return recorder


# --------------------------------------------------------------------------- #
# synthesize: param mapping + WAV write + result population
# --------------------------------------------------------------------------- #
def test_synthesize_writes_wav_and_maps_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _FakeModel(sr=24000)
    rec = _install_fake_modules(monkeypatch, model=model)

    provider = ChatterboxProvider(device="cpu")
    out = tmp_path / "audio" / "seg.wav"
    req = SynthesisRequest(
        text="Hello there.",
        voice_clip_path=tmp_path / "voice.wav",
        params={"exaggeration": 0.5, "cfg_weight": 0.3, "seed": 7},
    )
    result = provider.synthesize(req, out)

    # WAV written to out_path (parent created).
    assert out.is_file()
    assert rec["saved"] is not None
    saved_path, _tensor, saved_rate = rec["saved"]
    assert saved_path == str(out)
    assert saved_rate == 24000

    # Param mapping onto the generate call.
    assert len(model.generate_calls) == 1
    text, kwargs = model.generate_calls[0]
    assert text == "Hello there."
    assert kwargs["audio_prompt_path"] == str(req.voice_clip_path)
    assert kwargs["exaggeration"] == 0.5
    assert kwargs["cfg_weight"] == 0.3

    # Seed honored (determinism contract).
    assert rec["manual_seed"] == 7

    # Result populated from the model output: 4 samples / 24000 Hz.
    assert result.audio_path == out
    assert result.sample_rate == 24000
    assert result.duration_s == pytest.approx(4 / 24000)


def test_synthesize_omits_absent_optional_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _FakeModel()
    _install_fake_modules(monkeypatch, model=model)

    provider = ChatterboxProvider(device="cpu")
    # No exaggeration / cfg_weight / seed in params.
    provider.synthesize(
        SynthesisRequest(text="x", voice_clip_path=tmp_path / "v.wav", params={}),
        tmp_path / "o.wav",
    )
    _text, kwargs = model.generate_calls[0]
    assert "exaggeration" not in kwargs
    assert "cfg_weight" not in kwargs
    assert set(kwargs) == {"audio_prompt_path"}  # only the voice prompt is always passed


def test_model_is_loaded_once_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = _FakeModel()
    rec = _install_fake_modules(monkeypatch, model=model)
    provider = ChatterboxProvider(device="cpu")

    for i in range(3):
        provider.synthesize(
            SynthesisRequest(text=f"line {i}", voice_clip_path=tmp_path / "v.wav", params={}),
            tmp_path / f"o{i}.wav",
        )
    # Three synth calls but the provider holds one cached model instance.
    assert len(model.generate_calls) == 3
    assert provider._model is model
    assert rec["device"] == "cpu"


def test_generate_failure_is_normalized_to_tts_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _FakeModel(raise_on_generate=True)
    _install_fake_modules(monkeypatch, model=model)
    provider = ChatterboxProvider(device="cpu")
    with pytest.raises(TTSProviderError):
        provider.synthesize(
            SynthesisRequest(text="boom", voice_clip_path=tmp_path / "v.wav", params={}),
            tmp_path / "o.wav",
        )


def test_model_load_failure_is_normalized_to_tts_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _FakeModel()
    _install_fake_modules(monkeypatch, model=model, from_pretrained_raises=True)
    provider = ChatterboxProvider(device="cpu")
    with pytest.raises(TTSProviderError):
        provider.synthesize(
            SynthesisRequest(text="x", voice_clip_path=tmp_path / "v.wav", params={}),
            tmp_path / "o.wav",
        )


def test_device_auto_selects_cuda_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _FakeModel()
    rec = _install_fake_modules(monkeypatch, model=model, cuda_available=True)
    provider = ChatterboxProvider()  # device=None -> auto
    provider.synthesize(
        SynthesisRequest(text="x", voice_clip_path=tmp_path / "v.wav", params={}),
        tmp_path / "o.wav",
    )
    assert rec["device"] == "cuda"


def test_device_auto_falls_back_to_cpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = _FakeModel()
    rec = _install_fake_modules(monkeypatch, model=model, cuda_available=False)
    provider = ChatterboxProvider()  # device=None -> auto
    provider.synthesize(
        SynthesisRequest(text="x", voice_clip_path=tmp_path / "v.wav", params={}),
        tmp_path / "o.wav",
    )
    assert rec["device"] == "cpu"


# --------------------------------------------------------------------------- #
# is_available: spec-based, no model/torch load
# --------------------------------------------------------------------------- #
def test_is_available_false_when_chatterbox_absent() -> None:
    # torch/chatterbox are not installed in CI, so find_spec returns None -> unavailable.
    # (No fake modules injected here: this is the real environment check.)
    assert ChatterboxProvider().is_available() is False

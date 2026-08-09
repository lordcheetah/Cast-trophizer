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
import wave
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
    """Minimal tensor stand-in supporting the ops ``synthesize`` uses on the waveform.

    ``synthesize`` does ``tensor.detach().cpu().reshape(-1).tolist()`` to get a flat mono
    float list, then writes a 16-bit PCM WAV with the stdlib ``wave`` module.
    """

    def __init__(self, samples: list[float]) -> None:
        self._samples = samples

    def detach(self) -> _FakeTensor:
        return self

    def cpu(self) -> _FakeTensor:
        return self

    def reshape(self, *_shape: int) -> _FakeTensor:
        return self  # the fake waveform is already flat/mono

    def tolist(self) -> list[float]:
        return list(self._samples)


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
        return _FakeTensor([0.0, 0.1, -0.1, 0.0])  # 4 mono samples


def _install_fake_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: _FakeModel,
    cuda_available: bool = False,
    from_pretrained_raises: bool = False,
) -> dict[str, Any]:
    """Inject fake ``chatterbox.tts`` and ``torch`` modules; return recorders.

    ``synthesize`` writes the WAV with the stdlib ``wave`` module (not ``torchaudio``), so no
    fake ``torchaudio`` is needed — tests assert on the real WAV file written to ``out_path``.
    """
    recorder: dict[str, Any] = {"manual_seed": None, "cuda_seed": None}

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

    # A real 16-bit PCM mono WAV was written to out_path (parent created), at model.sr, with
    # one frame per model output sample.
    assert out.is_file()
    with wave.open(str(out), "rb") as written:
        assert written.getnchannels() == 1
        assert written.getsampwidth() == 2
        assert written.getframerate() == 24000
        assert written.getnframes() == 4

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
def test_is_available_false_when_deps_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the "not installed" case regardless of whether the `tts` extra is present in
    # this environment, so the test is hermetic (CI has no torch/chatterbox; a dev machine
    # with the `tts` extra does). is_available() imports find_spec from importlib.util lazily.
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
    assert ChatterboxProvider().is_available() is False


def test_is_available_true_when_deps_present(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: object())
    assert ChatterboxProvider().is_available() is True

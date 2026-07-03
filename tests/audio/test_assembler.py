"""Unit tests for the mockable parts of ``M4BAssembler`` (no real ffmpeg / mutagen).

``is_available`` is exercised via monkeypatching ``shutil.which``. The ``assemble`` path is
exercised with a mocked ``subprocess.run`` (rc=0 / rc!=0) and a stubbed lazily-imported
``mutagen.mp4.MP4`` / ``MP4Cover`` — asserting the concat-list + ffmetadata files are written
correctly, that a non-zero ffmpeg rc raises ``AssemblyError``, and that the cover atom is set
only when a cover is provided. The **real** ffmpeg/mutagen calls stay ``# VERIFY:`` and never
run in CI.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

from casttrophizer.audio.assembler import AssemblyRequest, ChapterMarker, M4BAssembler
from casttrophizer.errors import AssemblyError


# --------------------------------------------------------------------------- #
# is_available
# --------------------------------------------------------------------------- #
def test_is_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ffmpeg")
    assert M4BAssembler().is_available() is True


def test_is_available_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert M4BAssembler().is_available() is False


# --------------------------------------------------------------------------- #
# mutagen stub
# --------------------------------------------------------------------------- #
class _StubMP4Cover:
    FORMAT_PNG = 14
    FORMAT_JPEG = 13

    def __init__(self, data: bytes, imageformat: int) -> None:
        self.data = data
        self.imageformat = imageformat


class _StubMP4(dict):
    saved: bool = False
    last_path: str = ""

    def __init__(self, path: str) -> None:
        super().__init__()
        _StubMP4.last_path = path

    def save(self) -> None:
        _StubMP4.saved = True


@pytest.fixture
def stub_mutagen(monkeypatch: pytest.MonkeyPatch) -> type[_StubMP4]:
    """Install a fake ``mutagen.mp4`` module so the lazy import inside assemble() finds it."""
    _StubMP4.saved = False
    module = types.ModuleType("mutagen.mp4")
    module.MP4 = _StubMP4  # type: ignore[attr-defined]
    module.MP4Cover = _StubMP4Cover  # type: ignore[attr-defined]
    parent = types.ModuleType("mutagen")
    parent.mp4 = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mutagen", parent)
    monkeypatch.setitem(sys.modules, "mutagen.mp4", module)
    return _StubMP4


def _request(out_path: Path, *, cover: Path | None = None) -> AssemblyRequest:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    a = out_path.parent / "a.wav"
    b = out_path.parent / "b.wav"
    a.write_bytes(b"AWAV")
    b.write_bytes(b"BWAV")
    return AssemblyRequest(
        segment_audio_paths=[a, b],
        chapters=[
            ChapterMarker(title="One", start_s=0.0, end_s=1.5),
            ChapterMarker(title='Two: "quotes" #2', start_s=1.5, end_s=3.0),
        ],
        out_path=out_path,
        title="My Book",
        author="Me",
        cover_image_path=cover,
    )


# --------------------------------------------------------------------------- #
# assemble: success path writes intermediates + output, tags via mutagen
# --------------------------------------------------------------------------- #
def test_assemble_success_writes_output_and_intermediates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        # The concat list is the input after "-i"; the ffmeta file after the second "-i".
        i_indexes = [i for i, tok in enumerate(cmd) if tok == "-i"]
        concat_list = Path(cmd[i_indexes[0] + 1])
        ffmeta = Path(cmd[i_indexes[1] + 1])
        out = Path(cmd[-1])
        captured["concat_list"] = concat_list.read_text(encoding="utf-8")
        captured["ffmeta"] = ffmeta.read_text(encoding="utf-8")
        out.write_bytes(b"ENCODED")  # simulate ffmpeg producing the temp .m4b
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path = tmp_path / "output" / "book.m4b"
    req = _request(out_path)
    result = M4BAssembler().assemble(req)

    assert result == out_path
    assert out_path.read_bytes() == b"ENCODED"
    # Concat list has one entry per WAV in order.
    concat = captured["concat_list"]
    assert isinstance(concat, str)
    assert concat.splitlines() == [
        f"file '{(tmp_path / 'output' / 'a.wav').resolve().as_posix()}'",
        f"file '{(tmp_path / 'output' / 'b.wav').resolve().as_posix()}'",
    ]
    # ffmetadata has one [CHAPTER] block per marker with escaped title + ms START/END.
    ffmeta = captured["ffmeta"]
    assert isinstance(ffmeta, str)
    assert ffmeta.startswith(";FFMETADATA1")
    assert ffmeta.count("[CHAPTER]") == 2
    assert "TIMEBASE=1/1000" in ffmeta
    assert "START=0" in ffmeta and "END=1500" in ffmeta
    assert "START=1500" in ffmeta and "END=3000" in ffmeta
    assert "title=Two: \\#2" not in ffmeta  # sanity: quotes are not stripped
    assert 'title=Two: "quotes" \\#2' in ffmeta  # '#' escaped, quotes kept
    # Tags were written via the mutagen stub.
    assert stub_mutagen.saved is True
    # The temp build dir is cleaned up (no stray .assemble-* dirs left in output/).
    assert not list((tmp_path / "output").glob(".assemble-*"))


def test_assemble_ffmpeg_failure_raises_and_leaves_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom: bad codec")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path = tmp_path / "output" / "book.m4b"
    req = _request(out_path)
    with pytest.raises(AssemblyError, match="ffmpeg failed"):
        M4BAssembler().assemble(req)
    assert not out_path.exists()  # no half-written M4B
    assert not list((tmp_path / "output").glob(".assemble-*"))  # build dir cleaned


def test_assemble_embeds_cover_only_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Capture the MP4 instance so we can inspect the atoms that were set.
    instances: list[_StubMP4] = []
    orig_init = _StubMP4.__init__

    def tracking_init(self: _StubMP4, path: str) -> None:
        orig_init(self, path)
        instances.append(self)

    monkeypatch.setattr(_StubMP4, "__init__", tracking_init)

    # No cover -> no 'covr' atom.
    M4BAssembler().assemble(_request(tmp_path / "output" / "no_cover.m4b"))
    assert "covr" not in instances[-1]
    assert instances[-1]["\xa9nam"] == ["My Book"]
    assert instances[-1]["\xa9ART"] == ["Me"]

    # With a PNG cover -> 'covr' atom set with FORMAT_PNG.
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n")
    M4BAssembler().assemble(_request(tmp_path / "output" / "with_cover.m4b", cover=cover))
    covr = instances[-1]["covr"]
    assert len(covr) == 1
    assert covr[0].imageformat == _StubMP4Cover.FORMAT_PNG


def test_assemble_skips_bad_format_cover_but_still_produces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    instances: list[_StubMP4] = []
    orig_init = _StubMP4.__init__

    def tracking_init(self: _StubMP4, path: str) -> None:
        orig_init(self, path)
        instances.append(self)

    monkeypatch.setattr(_StubMP4, "__init__", tracking_init)

    bad_cover = tmp_path / "cover.gif"  # unsupported format -> skipped, still produce M4B
    bad_cover.write_bytes(b"GIF89a")
    out_path = tmp_path / "output" / "book.m4b"
    result = M4BAssembler().assemble(_request(out_path, cover=bad_cover))
    assert result.exists()
    assert "covr" not in instances[-1]  # cover skipped, not fatal


def test_assemble_empty_paths_raises(tmp_path: Path) -> None:
    req = AssemblyRequest(
        segment_audio_paths=[],
        chapters=[],
        out_path=tmp_path / "output" / "book.m4b",
        title="X",
        author="Y",
    )
    with pytest.raises(AssemblyError, match="no audio segments"):
        M4BAssembler().assemble(req)


# --------------------------------------------------------------------------- #
# concat list is a real FILE passed to ffmpeg (not a command-line arg list)
# --------------------------------------------------------------------------- #
def test_concat_input_is_a_file_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """The concat demuxer input after ``-i`` is a real file (avoids OS arg-length limits)."""
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        i = cmd.index("-i")
        concat_arg = Path(cmd[i + 1])
        seen["is_file"] = concat_arg.is_file()
        seen["has_concat_demuxer"] = "concat" in cmd and cmd[cmd.index("-f") + 1] == "concat"
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    M4BAssembler().assemble(_request(tmp_path / "output" / "book.m4b"))
    assert seen["is_file"] is True
    assert seen["has_concat_demuxer"] is True


# --------------------------------------------------------------------------- #
# concat-list path escaping (single quotes in a path)
# --------------------------------------------------------------------------- #
def test_concat_list_escapes_single_quote_in_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """A path containing a single quote is escaped ``'\\''`` per the concat-demuxer rule."""
    captured: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        concat_list = Path(cmd[cmd.index("-i") + 1])
        captured["concat"] = concat_list.read_text(encoding="utf-8")
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path = tmp_path / "output" / "book.m4b"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    quoted = tmp_path / "output" / "O'Brien.wav"
    quoted.write_bytes(b"WAV")
    req = AssemblyRequest(
        segment_audio_paths=[quoted],
        chapters=[ChapterMarker(title="One", start_s=0.0, end_s=1.0)],
        out_path=out_path,
        title="B",
        author="A",
    )
    M4BAssembler().assemble(req)
    # The embedded single-quote becomes '\'' inside the single-quoted concat entry.
    assert "O'\\''Brien.wav" in captured["concat"]


def test_concat_list_entries_are_absolute_for_relative_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """Relative segment paths (e.g. a relative --workdir) must be written ABSOLUTE.

    Regression: ffmpeg's concat demuxer resolves a relative entry against the list file's
    own directory (the temp build dir), not the process CWD — so a relative
    ``.smoke-out/audio/x.wav`` became a bogus ``<build_dir>/.smoke-out/audio/x.wav`` and
    ffmpeg failed with "No such file or directory". Entries are now ``resolve()``-d.
    """
    captured: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        concat_list = Path(cmd[cmd.index("-i") + 1])
        captured["concat"] = concat_list.read_text(encoding="utf-8")
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)  # so a relative segment path resolves under tmp_path

    seg_dir = tmp_path / "ws" / "audio"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "x.wav").write_bytes(b"WAV")
    relative_seg = Path("ws") / "audio" / "x.wav"  # relative to CWD (tmp_path)
    assert not relative_seg.is_absolute()

    req = AssemblyRequest(
        segment_audio_paths=[relative_seg],
        chapters=[ChapterMarker(title="One", start_s=0.0, end_s=1.0)],
        out_path=Path("ws") / "output" / "book.m4b",  # relative out too, like --workdir .smoke-out
        title="B",
        author="A",
    )
    M4BAssembler().assemble(req)

    expected = (tmp_path / "ws" / "audio" / "x.wav").resolve().as_posix()
    assert captured["concat"].splitlines() == [f"file '{expected}'"]
    assert "file 'ws/audio/x.wav'" not in captured["concat"]  # never the bare relative path


# --------------------------------------------------------------------------- #
# ffmetadata: a zero-length (empty) chapter still writes a well-formed block
# --------------------------------------------------------------------------- #
def test_ffmetadata_zero_length_chapter_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """An empty chapter (start_s == end_s) emits START == END in its ``[CHAPTER]`` block."""
    captured: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ffmeta = Path(cmd[[i for i, t in enumerate(cmd) if t == "-i"][1] + 1])
        captured["ffmeta"] = ffmeta.read_text(encoding="utf-8")
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path = tmp_path / "output" / "book.m4b"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    a = out_path.parent / "a.wav"
    a.write_bytes(b"WAV")
    req = AssemblyRequest(
        segment_audio_paths=[a],
        chapters=[
            ChapterMarker(title="Content", start_s=0.0, end_s=2.0),
            ChapterMarker(title="Empty", start_s=2.0, end_s=2.0),
        ],
        out_path=out_path,
        title="B",
        author="A",
    )
    M4BAssembler().assemble(req)
    ffmeta = captured["ffmeta"]
    assert ffmeta.count("[CHAPTER]") == 2
    # The empty chapter carries identical START/END (2000 ms).
    assert "START=2000\nEND=2000" in ffmeta


# --------------------------------------------------------------------------- #
# JPEG cover branch of _read_cover
# --------------------------------------------------------------------------- #
def test_assemble_jpeg_cover_uses_jpeg_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """A ``.jpg`` cover is embedded with ``FORMAT_JPEG`` (the JPEG detection branch)."""

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    instances: list[_StubMP4] = []
    orig_init = _StubMP4.__init__

    def tracking_init(self: _StubMP4, path: str) -> None:
        orig_init(self, path)
        instances.append(self)

    monkeypatch.setattr(_StubMP4, "__init__", tracking_init)

    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0JFIF")
    M4BAssembler().assemble(_request(tmp_path / "output" / "book.m4b", cover=cover))
    covr = instances[-1]["covr"]
    assert covr[0].imageformat == _StubMP4Cover.FORMAT_JPEG


def test_assemble_unreadable_cover_skipped_but_produces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """A supported-extension cover that cannot be read is skipped (no ``covr``), M4B produced.

    Exercises the ``_read_cover`` OSError branch: the extension passes format detection but the
    byte read raises, so the cover is dropped with a warning rather than failing the render.
    """

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    instances: list[_StubMP4] = []
    orig_init = _StubMP4.__init__

    def tracking_init(self: _StubMP4, path: str) -> None:
        orig_init(self, path)
        instances.append(self)

    monkeypatch.setattr(_StubMP4, "__init__", tracking_init)

    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG")

    real_read_bytes = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self == cover:
            raise OSError("permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", boom)

    out_path = tmp_path / "output" / "book.m4b"
    result = M4BAssembler().assemble(_request(out_path, cover=cover))
    assert result.exists()
    assert "covr" not in instances[-1]  # unreadable cover dropped, not fatal


# --------------------------------------------------------------------------- #
# ffmpeg launch failure (OSError) -> AssemblyError, no output
# --------------------------------------------------------------------------- #
def test_assemble_ffmpeg_launch_oserror_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """If ffmpeg vanishes between ``is_available`` and launch, wrap the OSError uniformly."""

    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("ffmpeg disappeared")

    monkeypatch.setattr(subprocess, "run", boom)

    out_path = tmp_path / "output" / "book.m4b"
    with pytest.raises(AssemblyError, match="failed to launch ffmpeg"):
        M4BAssembler().assemble(_request(out_path))
    assert not out_path.exists()
    assert not list((tmp_path / "output").glob(".assemble-*"))


# --------------------------------------------------------------------------- #
# mutagen open / save failures -> AssemblyError, no half-written output
# --------------------------------------------------------------------------- #
def test_assemble_mutagen_open_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """A mutagen ``MP4(path)`` failure is wrapped in ``AssemblyError`` and leaves no output."""

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def boom_init(self: _StubMP4, path: str) -> None:
        raise ValueError("not an MP4")

    monkeypatch.setattr(_StubMP4, "__init__", boom_init)

    out_path = tmp_path / "output" / "book.m4b"
    with pytest.raises(AssemblyError, match="failed to open"):
        M4BAssembler().assemble(_request(out_path))
    assert not out_path.exists()  # no atomic move happened
    assert not list((tmp_path / "output").glob(".assemble-*"))


def test_assemble_mutagen_save_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_mutagen: type[_StubMP4]
) -> None:
    """A mutagen ``save()`` failure is wrapped in ``AssemblyError`` and leaves no output."""

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[-1]).write_bytes(b"ENCODED")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def boom_save(self: _StubMP4) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_StubMP4, "save", boom_save)

    out_path = tmp_path / "output" / "book.m4b"
    with pytest.raises(AssemblyError, match="failed to write tags"):
        M4BAssembler().assemble(_request(out_path))
    assert not out_path.exists()
    assert not list((tmp_path / "output").glob(".assemble-*"))

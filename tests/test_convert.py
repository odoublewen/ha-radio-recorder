"""Converting finished streams with ffmpeg. Most tests need ffmpeg on PATH."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import mutagen
import pytest

from custom_components.radio_recorder.convert import conversion_for, convert

from .test_tagging import OGG_FLAC

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def part(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "abc.part"
    path.write_bytes(data)
    return path


def test_conversion_for(tmp_path) -> None:
    ogg_flac = part(tmp_path, OGG_FLAC.read_bytes())
    assert conversion_for(ogg_flac, ".ogg")[0] == ".flac"
    vorbis = part(tmp_path, b"OggS" + b"\x00" * 24 + b"\x01vorbis" + b"\x00" * 30)
    assert conversion_for(vorbis, ".ogg") is None
    assert conversion_for(vorbis, ".flac")[0] == ".flac"
    assert conversion_for(vorbis, ".aac")[0] == ".m4a"
    assert conversion_for(vorbis, ".mp3") is None


@needs_ffmpeg
async def test_ogg_flac_with_a_reconnect_becomes_one_flac(tmp_path) -> None:
    # Each reconnect appends a new Ogg FLAC stream ("chained Ogg").
    src = part(tmp_path, OGG_FLAC.read_bytes() * 2)
    ext, options = conversion_for(src, ".ogg")
    dest = await convert(FFMPEG, src, ext, options)
    assert dest == tmp_path / "abc.flac"
    assert src.exists()
    audio = mutagen.File(dest)
    assert type(audio).__name__ == "FLAC"
    assert audio.info.length == pytest.approx(0.5, abs=0.01)


@needs_ffmpeg
async def test_aac_becomes_m4a(tmp_path) -> None:
    src = tmp_path / "abc.part"
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.5", "-c:a", "aac",
        "-f", "adts", str(src),
    )  # fmt: skip
    assert await proc.wait() == 0
    ext, options = conversion_for(src, ".aac")
    dest = await convert(FFMPEG, src, ext, options)
    assert dest == tmp_path / "abc.m4a"
    assert type(mutagen.File(dest)).__name__ == "MP4"


@needs_ffmpeg
async def test_failed_conversion_keeps_the_recording(tmp_path) -> None:
    src = part(tmp_path, b"not audio" * 100)
    assert await convert(FFMPEG, src, ".flac", ("-c:a", "flac", "-f", "flac")) is None
    assert src.read_bytes() == b"not audio" * 100
    assert not (tmp_path / "abc.flac").exists()


async def test_missing_ffmpeg(tmp_path) -> None:
    src = part(tmp_path, OGG_FLAC.read_bytes())
    missing = str(tmp_path / "no-ffmpeg")
    assert await convert(missing, src, ".flac", ("-c:a", "flac", "-f", "flac")) is None
    assert src.exists()

"""Tagging and moving finished recordings."""

from __future__ import annotations

from datetime import datetime

import pytest
from mutagen.easyid3 import EasyID3

from custom_components.radio_recorder.tagging import finalize, parse_owner, safe_name

START = datetime(2026, 10, 14, 18, 15, 0)


def write_part(tmp_path, data: bytes = b"\xff\xfb" + b"\x00" * 4000):
    part = tmp_path / ".incoming" / "abc.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(data)
    return part


def test_tags_and_moves_an_mp3(tmp_path) -> None:
    part = write_part(tmp_path)
    dest = finalize(
        part, tmp_path, station="KALX", show="Madame X", start=START, content_type="audio/mpeg"
    )
    assert dest == tmp_path / "KALX" / "Madame X - 2026_10_14_18_15_00.mp3"
    assert not part.exists()
    tags = EasyID3(dest)
    assert tags["artist"] == ["KALX"]
    assert tags["album"] == ["Madame X"]
    assert tags["title"] == ["2026_10_14_18_15_00"]
    assert tags["date"] == ["2026-10-14"]
    assert tags["genre"] == ["Radio"]


def test_never_overwrites(tmp_path) -> None:
    first = finalize(
        write_part(tmp_path), tmp_path, station="KALX", show="X", start=START, content_type=None
    )
    first.write_bytes(b"keep me")
    second = finalize(
        write_part(tmp_path), tmp_path, station="KALX", show="X", start=START, content_type=None
    )
    assert first.read_bytes() == b"keep me"
    assert second.name == "X - 2026_10_14_18_15_00 (2).mp3"


def test_non_mp3_keeps_its_extension_untagged(tmp_path) -> None:
    dest = finalize(
        write_part(tmp_path, b"aac"),
        tmp_path,
        station="KALX",
        show="X",
        start=START,
        content_type="audio/aacp",
    )
    assert dest.suffix == ".aac"
    assert dest.read_bytes() == b"aac"


def test_unsafe_names_stay_in_their_folder(tmp_path) -> None:
    dest = finalize(
        write_part(tmp_path),
        tmp_path,
        station="../KALX",
        show="AC/DC hour",
        start=START,
        content_type=None,
    )
    assert dest.parent.parent == tmp_path
    assert dest.name.startswith("AC_DC hour - ")


def test_safe_name() -> None:
    assert safe_name("  ..  ") == "untitled"
    assert safe_name("a/b\\c") == "a_b_c"


def test_parse_owner() -> None:
    assert parse_owner("568:568") == (568, 568)
    assert parse_owner("") is None
    assert parse_owner(None) is None
    with pytest.raises(ValueError):
        parse_owner("apps")

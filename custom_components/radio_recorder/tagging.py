"""Tag a finished recording and move it into the library. Blocking; run in an executor."""

from __future__ import annotations

import errno
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import mutagen
from mutagen.easyid3 import EasyID3

_LOGGER = logging.getLogger(__name__)

# The song title and the file name both use the recording's start time in this form.
TIMESTAMP_FORMAT = "%Y_%m_%d_%H_%M_%S"

_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/aac": ".aac",
    "audio/aacp": ".aac",
    "audio/x-aac": ".aac",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "audio/flac": ".flac",
}


def extension_for(content_type: str | None) -> str:
    """File extension for a stream's content type; MP3 if unknown."""
    return _EXTENSIONS.get((content_type or "").lower(), ".mp3")


def safe_name(text: str) -> str:
    """Make `text` usable as one path component."""
    name = re.sub(r"[/\\\x00-\x1f]", "_", text).strip().strip(".")
    return name or "untitled"


def parse_owner(text: str | None) -> tuple[int, int] | None:
    """Parse "uid:gid". Raises ValueError if malformed; None if empty."""
    if not text or not text.strip():
        return None
    uid, sep, gid = text.strip().partition(":")
    if not sep or not uid.isdigit() or not gid.isdigit():
        raise ValueError(f"expected uid:gid, got {text!r}")
    return int(uid), int(gid)


def tag_file(path: Path, ext: str, *, station: str, show: str, title: str, date: str) -> None:
    """Station as artist, show as album, timestamp as title, in the file's own tag format.

    `ext` is the format's extension (`path` may be a `.part` file). Formats that can't
    hold tags (raw AAC) are left untagged.
    """
    try:
        audio = mutagen.File(path, easy=True)
    except mutagen.MutagenError:
        audio = None
    if audio is None:
        if ext != ".mp3":
            _LOGGER.debug("Not tagging %s: mutagen can't read it", path.name)
            return
        # mutagen can't always find the first frame of a raw MP3 stream; an ID3
        # header can still go in front of it.
        audio = EasyID3()
    elif audio.tags is None:
        try:
            audio.add_tags()
        except mutagen.MutagenError as err:
            _LOGGER.debug("Not tagging %s: %s", path.name, err)
            return
    audio["artist"] = station
    audio["album"] = show
    audio["title"] = title
    audio["date"] = date
    audio["genre"] = "Radio"
    audio.save(path)


def _move_no_clobber(src: Path, dest: Path) -> Path:
    """Move `src` to `dest`, or to `dest (2)` etc. if taken. Never overwrites."""
    for n in range(1, 1000):
        target = dest if n == 1 else dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        try:
            os.link(src, target)
        except FileExistsError:
            continue
        except OSError as err:
            # Hard links unsupported (some network shares): check, then rename.
            if err.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK):
                raise
            if target.exists():
                continue
            os.rename(src, target)
            return target
        os.unlink(src)
        return target
    raise FileExistsError(f"Too many recordings named {dest}")


def finalize(
    incoming: Path,
    output_dir: Path,
    *,
    station: str,
    show: str,
    start: datetime,
    ext: str,
    owner: tuple[int, int] | None = None,
) -> Path:
    """Tag `incoming` and move it to `<output_dir>/<station>/<show> - <timestamp><ext>`."""
    timestamp = start.strftime(TIMESTAMP_FORMAT)
    tag_file(
        incoming, ext, station=station, show=show, title=timestamp, date=start.strftime("%Y-%m-%d")
    )
    station_dir = output_dir / safe_name(station)
    station_dir.mkdir(parents=True, exist_ok=True)
    dest = _move_no_clobber(incoming, station_dir / f"{safe_name(show)} - {timestamp}{ext}")
    if owner is not None:
        os.chown(station_dir, *owner)
        os.chown(dest, *owner)
    return dest

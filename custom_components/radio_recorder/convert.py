"""Turn a finished stream into a normal audio file with ffmpeg.

A live FLAC stream, Ogg-wrapped or not, carries the broadcast's running sample count, so
players show a duration of days. Each reconnect also adds a new set of FLAC headers.
Decoding and re-encoding (lossless) gives a plain `.flac` with the right duration and
the segments joined. Raw AAC (ADTS) can't hold tags, so it's copied into `.m4a`.

Nothing here imports Home Assistant, so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Output extension and ffmpeg output options.
_FLAC = (".flac", ("-c:a", "flac", "-f", "flac"))
_M4A = (".m4a", ("-c:a", "copy", "-bsf:a", "aac_adtstoasc", "-f", "mp4"))


def _is_ogg_flac(path: Path) -> bool:
    # An Ogg FLAC stream's first packet starts with "\x7fFLAC"; Vorbis and Opus don't.
    with path.open("rb") as f:
        head = f.read(64)
    return head.startswith(b"OggS") and b"\x7fFLAC" in head


def conversion_for(path: Path, ext: str) -> tuple[str, tuple[str, ...]] | None:
    """The output extension and ffmpeg options for a stream saved as `ext`, if any.

    Blocking (it reads the file's first bytes).
    """
    if ext == ".flac" or (ext == ".ogg" and _is_ogg_flac(path)):
        return _FLAC
    if ext == ".aac":
        return _M4A
    return None


async def convert(ffmpeg: str, src: Path, ext: str, options: tuple[str, ...]) -> Path | None:
    """Write `src` converted to `<src without suffix><ext>`. None if ffmpeg failed.

    `src` is left alone, so the recording is never lost.
    """
    dest = src.with_suffix(ext)
    cmd = (ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a", *options)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, str(dest), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except OSError as err:
        _LOGGER.warning("Could not run %s to convert %s: %s", ffmpeg, src.name, err)
        return None
    try:
        _, stderr = await proc.communicate()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            dest.unlink(missing_ok=True)
    # ffmpeg complains about the live stream's timestamps but converts fine.
    if stderr:
        _LOGGER.debug("ffmpeg on %s: %s", src.name, stderr.decode(errors="replace").strip())
    if proc.returncode != 0 or not dest.exists() or not dest.stat().st_size:
        _LOGGER.warning("ffmpeg could not convert %s (exit %s)", src.name, proc.returncode)
        dest.unlink(missing_ok=True)
        return None
    return dest

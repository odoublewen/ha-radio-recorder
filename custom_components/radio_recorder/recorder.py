"""Save an internet radio stream to a file until an end time, reconnecting as needed.

Nothing here imports Home Assistant, so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

USER_AGENT = "ha-radio-recorder"

# Content types that are plainly not an audio stream. Anything else is saved as is.
_NOT_AUDIO = ("text/", "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/x-mpegurl")


class UnsupportedStream(Exception):
    """The URL serves something other than a plain audio stream (a web page, a playlist)."""


@dataclass
class Recording:
    """One recording, in progress or about to start. Serializable for restarts."""

    id: str
    station: str
    show: str
    url: str
    start: datetime
    end: datetime
    incoming: Path
    # Calendar event key, or None for a recording started by the record action.
    event_key: str | None = None
    content_type: str | None = None
    bytes: int = 0
    reconnects: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        data["incoming"] = str(self.incoming)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recording:
        return cls(
            **{
                **data,
                "start": datetime.fromisoformat(data["start"]),
                "end": datetime.fromisoformat(data["end"]),
                "incoming": Path(data["incoming"]),
            }
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _write(f: IO[bytes], data: bytes) -> None:
    f.write(data)
    f.flush()


class StreamRecorder:
    """Appends a stream's bytes to `recording.incoming` until `recording.end`.

    A dropped or stalled connection is retried with backoff until the end time, and the
    new bytes are appended (MP3 frames concatenate cleanly). `run` returns at the end
    time or after `stop`; it raises `UnsupportedStream` if the URL isn't audio.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        recording: Recording,
        *,
        on_progress: Callable[[], None] | None = None,
        now: Callable[[], datetime] = _utcnow,
        stall_timeout: float = 30,
        max_backoff: float = 30,
        flush_bytes: int = 256 * 1024,
        progress_interval: float = 300,
    ) -> None:
        self.recording = recording
        self._session = session
        self._on_progress = on_progress
        self._now = now
        self._stall_timeout = stall_timeout
        self._max_backoff = max_backoff
        self._flush_bytes = flush_bytes
        self._progress_interval = progress_interval
        self._stop = asyncio.Event()
        self._buffer = bytearray()
        self._got_data = False
        self._last_progress = 0.0

    def stop(self) -> None:
        """End the recording early. `run` returns once the buffer is written."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _remaining(self) -> float:
        return (self.recording.end - self._now()).total_seconds()

    async def run(self) -> None:
        rec = self.recording
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: rec.incoming.parent.mkdir(parents=True, exist_ok=True)
        )
        f = await loop.run_in_executor(None, open, rec.incoming, "ab")
        stream: asyncio.Task | None = None
        stopper: asyncio.Task | None = None
        _LOGGER.info(
            "Recording %s %r from %s until %s",
            rec.station,
            rec.show,
            rec.url,
            rec.end.astimezone().strftime("%H:%M"),
        )
        try:
            backoff = 1.0
            while not self._stop.is_set() and (remaining := self._remaining()) > 0:
                self._got_data = False
                stream = asyncio.ensure_future(self._stream(f))
                stopper = asyncio.ensure_future(self._stop.wait())
                done, _ = await asyncio.wait(
                    {stream, stopper}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                stopper.cancel()
                if stream not in done:
                    break  # end time or stop; `finally` cancels the stream
                if isinstance(err := stream.exception(), UnsupportedStream):
                    raise err
                stream = None
                if self._got_data:
                    backoff = 1.0
                rec.reconnects += 1
                _LOGGER.debug(
                    "%s stream dropped (%s); reconnecting in %.0f s",
                    rec.station,
                    err or "server closed the connection",
                    backoff,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), min(backoff, self._remaining()))
                backoff = min(backoff * 2, self._max_backoff)
        finally:
            if stopper is not None:
                stopper.cancel()
            if stream is not None and not stream.done():
                stream.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stream
            data = bytes(self._buffer)
            self._buffer.clear()
            if data:
                await loop.run_in_executor(None, _write, f, data)
            await loop.run_in_executor(None, f.close)
        _LOGGER.info(
            "Finished recording %s %r: %.1f MB, %d reconnects",
            rec.station,
            rec.show,
            rec.bytes / 1e6,
            rec.reconnects,
        )

    async def _stream(self, f: IO[bytes]) -> None:
        """Read one connection until it closes or fails."""
        rec = self.recording
        loop = asyncio.get_running_loop()
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=self._stall_timeout)
        # No Icy-MetaData header, so the server sends plain audio without song titles mixed in.
        async with self._session.get(
            rec.url, headers={"User-Agent": USER_AGENT}, timeout=timeout
        ) as resp:
            resp.raise_for_status()
            content_type = resp.content_type
            if content_type.startswith(_NOT_AUDIO):
                raise UnsupportedStream(f"{rec.url} serves {content_type}, not an audio stream")
            if rec.content_type is None:
                rec.content_type = content_type
            async for chunk in resp.content.iter_any():
                self._got_data = True
                self._buffer += chunk
                rec.bytes += len(chunk)
                if len(self._buffer) >= self._flush_bytes:
                    data = bytes(self._buffer)
                    self._buffer.clear()
                    await loop.run_in_executor(None, _write, f, data)
                if loop.time() - self._last_progress >= self._progress_interval:
                    self._last_progress = loop.time()
                    self._report_progress()

    def _report_progress(self) -> None:
        rec = self.recording
        _LOGGER.info(
            "%s %r: %.1f MB so far, %d reconnects, %.0f min left",
            rec.station,
            rec.show,
            rec.bytes / 1e6,
            rec.reconnects,
            max(self._remaining(), 0) / 60,
        )
        if self._on_progress is not None:
            self._on_progress()

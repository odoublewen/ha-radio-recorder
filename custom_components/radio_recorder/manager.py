"""Runs recordings, remembers them across restarts, and tells entities what changed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_FAILED,
    EVENT_FINISHED,
    INCOMING_DIR,
    SIGNAL_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .convert import conversion_for, convert
from .recorder import Recording, StreamRecorder, UnsupportedStream
from .tagging import extension_for, finalize

_LOGGER = logging.getLogger(__name__)

# Calendar events already recorded (or skipped) are remembered this long after they end.
HANDLED_RETENTION = timedelta(days=2)


@dataclass
class _Active:
    recording: Recording
    recorder: StreamRecorder
    task: asyncio.Task | None = None
    path: Path | None = None


@dataclass
class Finished:
    """The last recording a station finished."""

    path: str
    show: str
    finished: datetime
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "finished": self.finished.isoformat()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finished:
        return cls(**{**data, "finished": datetime.fromisoformat(data["finished"])})


@dataclass
class RecordingManager:
    hass: HomeAssistant
    entry: ConfigEntry
    output_dir: Path
    owner: tuple[int, int] | None = None
    active: dict[str, _Active] = field(default_factory=dict)
    last: dict[str, Finished] = field(default_factory=dict)
    # Calendar event key -> end of the event, for events already started or skipped.
    handled: dict[str, datetime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._store: Store[dict[str, Any]] = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)

    # -- persistence -----------------------------------------------------------

    async def async_load(self) -> None:
        """Restore state, then resume recordings that were cut short by a restart."""
        data = await self._store.async_load() or {}
        self.last = {k: Finished.from_dict(v) for k, v in data.get("last", {}).items()}
        self.handled = {k: datetime.fromisoformat(v) for k, v in data.get("handled", {}).items()}
        now = dt_util.utcnow()
        for raw in data.get("active", []):
            rec = Recording.from_dict(raw)
            if rec.end > now:
                _LOGGER.info("Resuming %s %r after a restart", rec.station, rec.show)
                self._launch(rec)
            else:
                _LOGGER.info("Finishing %s %r, which ended during a restart", rec.station, rec.show)
                self._launch(rec, resume=False)

    def _data(self) -> dict[str, Any]:
        cutoff = dt_util.utcnow() - HANDLED_RETENTION
        self.handled = {k: end for k, end in self.handled.items() if end > cutoff}
        return {
            "active": [a.recording.as_dict() for a in self.active.values()],
            "last": {k: v.as_dict() for k, v in self.last.items()},
            "handled": {k: v.isoformat() for k, v in self.handled.items()},
        }

    @callback
    def _changed(self) -> None:
        self._store.async_delay_save(self._data, 1)
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    # -- recordings ------------------------------------------------------------

    @callback
    def async_start(
        self,
        *,
        station: str,
        show: str,
        url: str,
        end: datetime,
        event_key: str | None = None,
    ) -> Recording:
        """Start recording now, in the background."""
        rec_id = uuid.uuid4().hex[:12]
        rec = Recording(
            id=rec_id,
            station=station,
            show=show,
            url=url,
            start=dt_util.now(),
            end=dt_util.as_utc(end),
            incoming=self.output_dir / INCOMING_DIR / f"{rec_id}.part",
            event_key=event_key,
        )
        if event_key is not None:
            self.handled[event_key] = rec.end
        self._launch(rec)
        return rec

    @callback
    def _launch(self, rec: Recording, *, resume: bool = True) -> None:
        recorder = StreamRecorder(
            async_get_clientsession(self.hass), rec, on_progress=self._progress
        )
        active = _Active(rec, recorder)
        self.active[rec.id] = active
        active.task = self.entry.async_create_background_task(
            self.hass, self._run(active, resume), f"{DOMAIN} {rec.station} {rec.show}"
        )
        self._changed()

    @callback
    def _progress(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    async def _run(self, active: _Active, resume: bool) -> None:
        rec = active.recording
        error: str | None = None
        # A resumed recording appends to what was saved before the restart.
        rec.bytes = await self.hass.async_add_executor_job(_size, rec.incoming)
        try:
            if resume:
                await active.recorder.run()
        except asyncio.CancelledError:
            # HA is stopping or the entry is reloading. Keep the stored state so the
            # recording resumes next time.
            self._store.async_delay_save(self._data, 0)
            raise
        except UnsupportedStream as err:
            error = str(err)
        except Exception as err:
            _LOGGER.exception("Recording %s %r failed", rec.station, rec.show)
            error = f"{type(err).__name__}: {err}"

        path: Path | None = None
        if rec.bytes:
            try:
                path = await self._async_save(rec)
            except Exception as err:
                _LOGGER.exception("Could not save %s", rec.incoming)
                error = f"{type(err).__name__}: {err}"
        elif error is None:
            error = "no audio received"
        if path is None:
            await self.hass.async_add_executor_job(_remove, rec.incoming)

        active.path = path
        del self.active[rec.id]
        event = {
            "recording_id": rec.id,
            "station": rec.station,
            "show": rec.show,
            "bytes": rec.bytes,
            "reconnects": rec.reconnects,
        }
        if path is not None:
            _LOGGER.info("Saved %s", path)
            self.last[rec.station] = Finished(str(path), rec.show, dt_util.utcnow(), rec.bytes)
            self.hass.bus.async_fire(EVENT_FINISHED, {**event, "path": str(path)})
        else:
            _LOGGER.warning("Recording %s %r failed: %s", rec.station, rec.show, error)
            self.hass.bus.async_fire(EVENT_FAILED, {**event, "error": error})
        self._changed()

    async def _async_save(self, rec: Recording) -> Path:
        """Convert the stream if it needs it, then tag it and move it into the library.

        If the conversion fails, the stream is saved as it was recorded.
        """
        src, ext = rec.incoming, extension_for(rec.content_type)
        if conversion := await self.hass.async_add_executor_job(conversion_for, src, ext):
            new_ext, options = conversion
            if converted := await convert(_ffmpeg_binary(self.hass), src, new_ext, options):
                src, ext = converted, new_ext
        if src == rec.incoming:
            return await self.hass.async_add_executor_job(self._finalize, rec, src, ext)
        try:
            path = await self.hass.async_add_executor_job(self._finalize, rec, src, ext)
        except Exception:
            await self.hass.async_add_executor_job(_remove, src)
            raise
        await self.hass.async_add_executor_job(_remove, rec.incoming)
        return path

    def _finalize(self, rec: Recording, src: Path, ext: str) -> Path:
        return finalize(
            src,
            self.output_dir,
            station=rec.station,
            show=rec.show,
            start=dt_util.as_local(rec.start),
            ext=ext,
            owner=self.owner,
        )

    async def async_stop(self, rec_id: str) -> Path | None:
        """Stop a recording and wait for it to be saved. Returns the saved path."""
        active = self.active.get(rec_id)
        if active is None:
            return None
        active.recorder.stop()
        if active.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await active.task
        return active.path

    @callback
    def async_skip_event(self, event_key: str, end: datetime, station: str, show: str, error: str):
        """Mark a calendar event handled without recording it, and report why."""
        self.handled[event_key] = dt_util.as_utc(end)
        _LOGGER.warning("Not recording %s %r: %s", station, show, error)
        self.hass.bus.async_fire(
            EVENT_FAILED, {"recording_id": None, "station": station, "show": show, "error": error}
        )
        self._changed()

    async def async_unload(self) -> None:
        """Cancel recordings without finishing them, and save them for the next setup."""
        tasks = [a.task for a in self.active.values() if a.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._store.async_save(self._data())

    def recording_for_event(self, event_key: str) -> Recording | None:
        for active in self.active.values():
            if active.recording.event_key == event_key:
                return active.recording
        return None

    def recordings_for_station(self, station: str) -> list[Recording]:
        return [
            a.recording
            for a in self.active.values()
            if a.recording.station.casefold() == station.casefold()
        ]


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    """The ffmpeg set in HA's ffmpeg integration if it's loaded, else the one on PATH."""
    if "ffmpeg" in hass.config.components:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    return "ffmpeg"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)

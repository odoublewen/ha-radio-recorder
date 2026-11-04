"""Turn events on a calendar into recordings.

Each event is one recording: the summary is the show, the location is the station (a
configured station's name, or a stream URL), and the event's start and end, widened by
the padding, are when to record. The calendar is re-read every minute and whenever its
state changes, and a timer is kept for each upcoming event.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import SIGNAL_UPDATED
from .manager import RecordingManager

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(minutes=1)
LOOKAHEAD = timedelta(hours=25)


@dataclass(frozen=True)
class ScheduledEvent:
    """A timed calendar event, with the padding applied."""

    key: str
    show: str
    location: str
    start: datetime
    end: datetime

    @classmethod
    def from_calendar(
        cls, event: dict[str, Any], pad_before: timedelta, pad_after: timedelta
    ) -> ScheduledEvent | None:
        raw_start, raw_end = str(event.get("start", "")), str(event.get("end", ""))
        if "T" not in raw_start or "T" not in raw_end:
            return None  # all-day event
        start = dt_util.parse_datetime(raw_start)
        end = dt_util.parse_datetime(raw_end)
        if start is None or end is None:
            return None
        show = (event.get("summary") or "").strip()
        location = (event.get("location") or "").strip()
        return cls(
            key=f"{start.isoformat()}|{show}|{location}",
            show=show,
            location=location,
            start=dt_util.as_utc(start) - pad_before,
            end=dt_util.as_utc(end) + pad_after,
        )


def station_for_location(location: str, stations: dict[str, str]) -> tuple[str, str] | None:
    """(station name, stream URL) for an event location, or None if it matches nothing."""
    for name, url in stations.items():
        if name.casefold() == location.casefold():
            return name, url
    parsed = urlparse(location)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        return parsed.hostname, location
    return None


class CalendarScheduler:
    def __init__(
        self,
        hass: HomeAssistant,
        manager: RecordingManager,
        calendar_entity_id: str,
        stations: Callable[[], dict[str, str]],
        pad_before: timedelta,
        pad_after: timedelta,
    ) -> None:
        self.hass = hass
        self.manager = manager
        self.calendar_entity_id = calendar_entity_id
        self._stations = stations
        self._pad_before = pad_before
        self._pad_after = pad_after
        self._timers: dict[str, tuple[datetime, CALLBACK_TYPE]] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self.next_event: ScheduledEvent | None = None
        self._warned_unavailable = False

    async def async_start(self) -> None:
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_refresh_cb, REFRESH_INTERVAL)
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, self.calendar_entity_id, self._async_state_changed
            )
        )
        await self.async_refresh()

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for _, cancel in self._timers.values():
            cancel()
        self._timers.clear()

    async def _async_refresh_cb(self, _now: datetime) -> None:
        await self.async_refresh()

    async def _async_state_changed(self, _event: Event) -> None:
        await self.async_refresh()

    async def _async_fetch(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        response = await self.hass.services.async_call(
            "calendar",
            "get_events",
            {
                "entity_id": self.calendar_entity_id,
                "start_date_time": start,
                "end_date_time": end,
            },
            blocking=True,
            return_response=True,
        )
        return response[self.calendar_entity_id]["events"]

    async def async_refresh(self) -> None:
        now = dt_util.utcnow()
        # Query wide enough to see events whose padding reaches now.
        try:
            raw = await self._async_fetch(now - self._pad_after, now + self._pad_before + LOOKAHEAD)
        except (HomeAssistantError, KeyError) as err:
            if not self._warned_unavailable:
                _LOGGER.warning("Cannot read %s: %s", self.calendar_entity_id, err)
                self._warned_unavailable = True
            return
        self._warned_unavailable = False

        events = [
            e
            for e in (
                ScheduledEvent.from_calendar(r, self._pad_before, self._pad_after) for r in raw
            )
            if e is not None and e.end > now
        ]
        upcoming: dict[str, ScheduledEvent] = {}
        for event in events:
            if event.key in self.manager.handled or self.manager.recording_for_event(event.key):
                continue
            if event.start <= now:
                self._async_begin(event)
            else:
                upcoming[event.key] = event

        for key in list(self._timers):
            when, cancel = self._timers[key]
            if key not in upcoming or upcoming[key].start != when:
                cancel()
                del self._timers[key]
        for key, event in upcoming.items():
            if key not in self._timers:
                self._timers[key] = (
                    event.start,
                    async_track_point_in_time(self.hass, self._make_timer_cb(event), event.start),
                )

        next_event = min(upcoming.values(), key=lambda e: e.start, default=None)
        if next_event != self.next_event:
            self.next_event = next_event
            async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    def _make_timer_cb(self, event: ScheduledEvent) -> Callable[[datetime], None]:
        @callback
        def fire(_now: datetime) -> None:
            self._timers.pop(event.key, None)
            if event.key in self.manager.handled or self.manager.recording_for_event(event.key):
                return
            self._async_begin(event)
            self.hass.async_create_task(self.async_refresh())

        return fire

    @callback
    def _async_begin(self, event: ScheduledEvent) -> None:
        resolved = station_for_location(event.location, self._stations())
        if resolved is None:
            self.manager.async_skip_event(
                event.key,
                event.end,
                event.location,
                event.show,
                f"event location {event.location!r} is not a configured station or a URL",
            )
            return
        station, url = resolved
        self.manager.async_start(
            station=station,
            show=event.show or "Untitled",
            url=url,
            end=event.end,
            event_key=event.key,
        )

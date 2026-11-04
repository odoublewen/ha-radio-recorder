"""Shared fixtures: a fake stream recorder and a calendar whose events tests control."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radio_recorder.const import (
    CONF_CALENDAR,
    CONF_OUTPUT_DIR,
    CONF_PAD_AFTER,
    CONF_PAD_BEFORE,
    DOMAIN,
    SUBENTRY_STATION,
)
from custom_components.radio_recorder.recorder import Recording

KALX_URL = "http://stream.kalx.berkeley.edu:8000/kalx-256.mp3"
CALENDAR = "calendar.radio"
AUDIO = b"\xff\xfb" + b"\x00" * 1022


class FakeRecorder:
    """Writes one chunk, then waits for the end time or `stop`.

    Set `FakeRecorder.error` to make the next run raise it after writing nothing.
    """

    instances: list[FakeRecorder] = []
    error: Exception | None = None

    def __init__(self, session, recording: Recording, **kwargs) -> None:
        self.recording = recording
        self._stop = asyncio.Event()
        self.ran = False
        FakeRecorder.instances.append(self)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        rec = self.recording
        self.ran = True
        if FakeRecorder.error is not None:
            error, FakeRecorder.error = FakeRecorder.error, None
            raise error
        rec.incoming.parent.mkdir(parents=True, exist_ok=True)
        with rec.incoming.open("ab") as f:
            f.write(AUDIO)
        rec.bytes += len(AUDIO)
        rec.content_type = "audio/mpeg"
        remaining = (rec.end - dt_util.utcnow()).total_seconds()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), max(remaining, 0))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def recorder():
    FakeRecorder.instances = []
    FakeRecorder.error = None
    with patch("custom_components.radio_recorder.manager.StreamRecorder", FakeRecorder):
        yield FakeRecorder


@pytest.fixture
def calendar_events():
    """Events the schedule calendar returns, as calendar.get_events would."""
    events: list[dict[str, Any]] = []

    async def fetch(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return list(events)

    with patch("custom_components.radio_recorder.scheduler.CalendarScheduler._async_fetch", fetch):
        yield events


def calendar_event(start: datetime, end: datetime, show: str, location: str) -> dict[str, Any]:
    return {
        "start": dt_util.as_local(start).isoformat(),
        "end": dt_util.as_local(end).isoformat(),
        "summary": show,
        "location": location,
    }


@pytest.fixture
def entry(tmp_path) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Radio Recorder",
        data={
            CONF_OUTPUT_DIR: str(tmp_path),
            CONF_CALENDAR: CALENDAR,
            CONF_PAD_BEFORE: 1,
            CONF_PAD_AFTER: 2,
        },
        subentries_data=[
            ConfigSubentryData(
                data={CONF_NAME: "KALX", CONF_URL: KALX_URL},
                subentry_type=SUBENTRY_STATION,
                title="KALX",
                unique_id=None,
            )
        ],
    )

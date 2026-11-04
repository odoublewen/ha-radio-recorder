"""Setup, the record and stop actions, restarts, and calendar scheduling."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from mutagen.easyid3 import EasyID3
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.radio_recorder.const import (
    DOMAIN,
    EVENT_FAILED,
    EVENT_FINISHED,
    STORAGE_KEY,
)
from custom_components.radio_recorder.recorder import Recording, UnsupportedStream

from .conftest import AUDIO, KALX_URL, calendar_event

RECORDING = "binary_sensor.kalx_recording"
LAST = "sensor.kalx_last_recording"
NEXT = "sensor.radio_recorder_next_recording"


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def loaded(hass: HomeAssistant, entry, calendar_events) -> MockConfigEntry:
    return await setup(hass, entry)


async def record(hass: HomeAssistant, **data) -> dict:
    data = {"station": "KALX", "show": "Madame X", "duration": {"hours": 1}, **data}
    data = {k: v for k, v in data.items() if v is not None}
    return await hass.services.async_call(
        DOMAIN, "record", data, blocking=True, return_response=True
    )


async def finish(entry: MockConfigEntry) -> None:
    """Wait for every recording in progress to end and be saved."""
    await asyncio.gather(*(a.task for a in entry.runtime_data.manager.active.values()))


async def stop(hass: HomeAssistant, **data) -> dict:
    return await hass.services.async_call(DOMAIN, "stop", data, blocking=True, return_response=True)


# -- setup ---------------------------------------------------------------------


async def test_setup_and_unload(hass: HomeAssistant, loaded) -> None:
    assert loaded.state is ConfigEntryState.LOADED
    assert hass.states.get(RECORDING).state == STATE_OFF
    assert hass.states.get(LAST).state == STATE_UNKNOWN
    assert hass.states.get(NEXT).state == STATE_UNKNOWN
    assert await hass.config_entries.async_unload(loaded.entry_id)


async def test_no_calendar_means_no_next_recording_sensor(
    hass: HomeAssistant, entry, tmp_path
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, data={"output_dir": str(tmp_path)})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(NEXT) is None
    assert hass.states.get(RECORDING).state == STATE_OFF


# -- record and stop -----------------------------------------------------------


async def test_record_then_stop_saves_a_tagged_file(
    hass: HomeAssistant, loaded, recorder, tmp_path
) -> None:
    finished = async_capture_events(hass, EVENT_FINISHED)
    response = await record(hass)
    await hass.async_block_till_done()
    (fake,) = recorder.instances
    assert fake.recording.url == KALX_URL
    assert response["recording_id"] == fake.recording.id
    state = hass.states.get(RECORDING)
    assert state.state == STATE_ON
    assert state.attributes["show"] == "Madame X"

    response = await stop(hass, station="kalx")
    await hass.async_block_till_done()
    (stopped,) = response["stopped"]
    path = Path(stopped["path"])
    assert path.parent == tmp_path / "KALX"
    assert path.name.startswith("Madame X - ")
    assert path.read_bytes().endswith(AUDIO)
    tags = EasyID3(path)
    assert tags["artist"] == ["KALX"]
    assert tags["album"] == ["Madame X"]
    assert not list((tmp_path / ".incoming").iterdir())

    assert hass.states.get(RECORDING).state == STATE_OFF
    last = hass.states.get(LAST)
    assert last.state != STATE_UNKNOWN
    assert last.attributes["path"] == str(path)
    assert [e.data["path"] for e in finished] == [str(path)]


async def test_recording_stops_at_its_end_time(hass: HomeAssistant, loaded, recorder) -> None:
    finished = async_capture_events(hass, EVENT_FINISHED)
    await record(hass, duration={"seconds": 0.2})
    await finish(loaded)
    await hass.async_block_till_done()
    assert len(finished) == 1
    assert hass.states.get(RECORDING).state == STATE_OFF


async def test_record_any_url_with_a_new_station_name(
    hass: HomeAssistant, loaded, recorder
) -> None:
    await record(hass, station="WFMU", url="http://stream0.wfmu.org/freeform-128k")
    (fake,) = recorder.instances
    assert fake.recording.station == "WFMU"
    await stop(hass, station="WFMU")


async def test_record_unknown_station_without_url(hass: HomeAssistant, loaded) -> None:
    with pytest.raises(ServiceValidationError, match="No station named"):
        await record(hass, station="WFMU")


async def test_record_end_in_the_past(hass: HomeAssistant, loaded) -> None:
    with pytest.raises(ServiceValidationError, match="already passed"):
        await record(hass, duration=None, end=dt_util.now() - timedelta(minutes=1))


async def test_stop_with_nothing_recording(hass: HomeAssistant, loaded) -> None:
    with pytest.raises(ServiceValidationError):
        await stop(hass, recording_id="nope")


async def test_failed_recording_fires_an_event_and_leaves_no_file(
    hass: HomeAssistant, loaded, recorder, tmp_path
) -> None:
    failed = async_capture_events(hass, EVENT_FAILED)
    recorder.error = UnsupportedStream("serves text/html")
    await record(hass)
    await finish(loaded)
    await hass.async_block_till_done()
    assert [e.data["error"] for e in failed] == ["serves text/html"]
    assert hass.states.get(RECORDING).state == STATE_OFF
    assert not (tmp_path / "KALX").exists()


# -- restarts ------------------------------------------------------------------


def stored_recording(tmp_path: Path, rec_id: str, end_offset: timedelta) -> Recording:
    now = dt_util.utcnow()
    part = tmp_path / ".incoming" / f"{rec_id}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(AUDIO)
    return Recording(
        id=rec_id,
        station="KALX",
        show="Madame X",
        url=KALX_URL,
        start=now - timedelta(minutes=30),
        end=now + end_offset,
        incoming=part,
        content_type="audio/mpeg",
    )


async def test_restart_resumes_unfinished_and_saves_finished(
    hass: HomeAssistant, hass_storage, entry, calendar_events, recorder, tmp_path
) -> None:
    ongoing = stored_recording(tmp_path, "ongoing", timedelta(minutes=30))
    ended = stored_recording(tmp_path, "ended", timedelta(minutes=-5))
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {"active": [ongoing.as_dict(), ended.as_dict()]},
    }
    finished = async_capture_events(hass, EVENT_FINISHED)
    await setup(hass, entry)
    await hass.async_block_till_done()

    assert [f.recording.id for f in recorder.instances if f.ran] == ["ongoing"]
    assert hass.states.get(RECORDING).state == STATE_ON
    assert ongoing.incoming.read_bytes() == AUDIO * 2  # appended
    assert [e.data["recording_id"] for e in finished] == ["ended"]
    assert Path(finished[0].data["path"]).exists()
    await stop(hass, station="KALX")


async def test_unload_keeps_unfinished_recordings_for_next_time(
    hass: HomeAssistant, hass_storage, loaded, recorder
) -> None:
    response = await record(hass)
    (fake,) = recorder.instances
    async with asyncio.timeout(2):
        while not fake.recording.bytes:
            await asyncio.sleep(0.01)
    assert await hass.config_entries.async_unload(loaded.entry_id)
    (stored,) = hass_storage[STORAGE_KEY]["data"]["active"]
    assert stored["id"] == response["recording_id"]
    assert Path(stored["incoming"]).exists()


# -- calendar ------------------------------------------------------------------


async def test_event_in_progress_starts_recording_once(
    hass: HomeAssistant, entry, calendar_events, recorder, freezer
) -> None:
    now = dt_util.utcnow()
    calendar_events.append(
        calendar_event(now - timedelta(minutes=5), now + timedelta(hours=1), "Madame X", "kalx")
    )
    await setup(hass, entry)
    (fake,) = recorder.instances
    rec = fake.recording
    assert (rec.station, rec.show, rec.url) == ("KALX", "Madame X", KALX_URL)
    assert rec.end == now + timedelta(hours=1, minutes=2)  # end padding

    await stop(hass, station="KALX")
    freezer.tick(timedelta(minutes=1, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(recorder.instances) == 1  # stopped early: not restarted


async def test_upcoming_event_starts_on_time(
    hass: HomeAssistant, entry, calendar_events, recorder, freezer
) -> None:
    start = dt_util.utcnow().replace(microsecond=0) + timedelta(minutes=10)
    calendar_events.append(calendar_event(start, start + timedelta(hours=2), "Morning", "KALX"))
    await setup(hass, entry)
    assert recorder.instances == []
    next_state = hass.states.get(NEXT)
    assert dt_util.parse_datetime(next_state.state) == start - timedelta(minutes=1)
    assert next_state.attributes["show"] == "Morning"

    freezer.move_to(start - timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    (fake,) = recorder.instances
    assert fake.recording.show == "Morning"
    assert hass.states.get(NEXT).state == STATE_UNKNOWN
    await stop(hass, station="KALX")


async def test_deleted_event_is_not_recorded(
    hass: HomeAssistant, entry, calendar_events, recorder, freezer
) -> None:
    now = dt_util.utcnow()
    start = now + timedelta(minutes=10)
    calendar_events.append(calendar_event(start, start + timedelta(hours=1), "Gone", "KALX"))
    await setup(hass, entry)
    calendar_events.clear()
    freezer.tick(timedelta(minutes=1, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(NEXT).state == STATE_UNKNOWN

    freezer.move_to(start)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert recorder.instances == []


async def test_event_with_a_url_location(
    hass: HomeAssistant, entry, calendar_events, recorder
) -> None:
    now = dt_util.utcnow()
    url = "http://stream0.wfmu.org/freeform-128k"
    calendar_events.append(calendar_event(now, now + timedelta(hours=1), "Freeform", url))
    await setup(hass, entry)
    (fake,) = recorder.instances
    assert (fake.recording.station, fake.recording.url) == ("stream0.wfmu.org", url)
    await stop(hass, recording_id=fake.recording.id)


async def test_event_for_an_unknown_station_is_reported_once(
    hass: HomeAssistant, entry, calendar_events, recorder, freezer
) -> None:
    failed = async_capture_events(hass, EVENT_FAILED)
    now = dt_util.utcnow()
    calendar_events.append(calendar_event(now, now + timedelta(hours=1), "Show", "WXYZ"))
    await setup(hass, entry)
    freezer.tick(timedelta(minutes=1, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert recorder.instances == []
    assert len(failed) == 1
    assert "WXYZ" in failed[0].data["error"]


async def test_all_day_events_are_ignored(
    hass: HomeAssistant, entry, calendar_events, recorder
) -> None:
    today = dt_util.now().date()
    calendar_events.append(
        {
            "start": today.isoformat(),
            "end": (today + timedelta(days=1)).isoformat(),
            "summary": "Pledge drive",
            "location": "KALX",
        }
    )
    await setup(hass, entry)
    assert recorder.instances == []

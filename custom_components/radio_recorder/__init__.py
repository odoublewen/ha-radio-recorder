"""Radio Recorder: record internet radio shows, scheduled from a calendar."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_NAME, CONF_URL, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DURATION,
    ATTR_END,
    ATTR_RECORDING_ID,
    ATTR_SHOW,
    ATTR_STATION,
    ATTR_URL,
    CONF_CALENDAR,
    CONF_FILE_OWNER,
    CONF_OUTPUT_DIR,
    CONF_PAD_AFTER,
    CONF_PAD_BEFORE,
    DEFAULT_PAD_AFTER,
    DEFAULT_PAD_BEFORE,
    DOMAIN,
    SERVICE_RECORD,
    SERVICE_STOP,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_STATION,
)
from .manager import RecordingManager
from .scheduler import CalendarScheduler
from .tagging import parse_owner

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class RadioRecorderData:
    manager: RecordingManager
    scheduler: CalendarScheduler | None


type RadioRecorderConfigEntry = ConfigEntry[RadioRecorderData]

RECORD_SCHEMA = vol.All(
    cv.has_at_least_one_key(ATTR_DURATION, ATTR_END),
    cv.has_at_most_one_key(ATTR_DURATION, ATTR_END),
    vol.Schema(
        {
            vol.Required(ATTR_STATION): cv.string,
            vol.Required(ATTR_SHOW): cv.string,
            vol.Optional(ATTR_DURATION): vol.All(cv.time_period, cv.positive_timedelta),
            vol.Optional(ATTR_END): cv.datetime,
            vol.Optional(ATTR_URL): cv.url,
        }
    ),
)
STOP_SCHEMA = vol.All(
    cv.has_at_least_one_key(ATTR_RECORDING_ID, ATTR_STATION),
    vol.Schema(
        {
            vol.Optional(ATTR_RECORDING_ID): cv.string,
            vol.Optional(ATTR_STATION): cv.string,
        }
    ),
)


def settings(entry: ConfigEntry) -> dict[str, Any]:
    """The current settings. The options flow saves a full set, replacing the setup data."""
    return dict(entry.options or entry.data)


def stations(entry: ConfigEntry) -> dict[str, str]:
    """Configured stations: name -> stream URL."""
    return {
        sub.data[CONF_NAME]: sub.data[CONF_URL]
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_STATION
    }


def _loaded_data(hass: HomeAssistant) -> RadioRecorderData:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    raise ServiceValidationError("Radio Recorder is not set up")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the record and stop actions."""

    async def record(call: ServiceCall) -> ServiceResponse:
        data = _loaded_data(hass)
        configured = stations(data.manager.entry)
        station = call.data[ATTR_STATION]
        url = call.data.get(ATTR_URL)
        match = next((n for n in configured if n.casefold() == station.casefold()), None)
        if match is not None:
            station = match
            url = url or configured[match]
        if url is None:
            raise ServiceValidationError(
                f"No station named {station!r}; add it to Radio Recorder or pass a url"
            )
        if ATTR_END in call.data:
            end = dt_util.as_local(call.data[ATTR_END])
        else:
            end = dt_util.now() + call.data[ATTR_DURATION]
        if end <= dt_util.now():
            raise ServiceValidationError("The end time has already passed")
        rec = data.manager.async_start(station=station, show=call.data[ATTR_SHOW], url=url, end=end)
        return {"recording_id": rec.id, "station": rec.station, "end": rec.end.isoformat()}

    async def stop(call: ServiceCall) -> ServiceResponse:
        manager = _loaded_data(hass).manager
        if ATTR_RECORDING_ID in call.data:
            ids = (
                [call.data[ATTR_RECORDING_ID]]
                if call.data[ATTR_RECORDING_ID] in manager.active
                else []
            )
        else:
            ids = [r.id for r in manager.recordings_for_station(call.data[ATTR_STATION])]
        if not ids:
            raise ServiceValidationError("No matching recording in progress")
        stopped = []
        for rec_id in ids:
            path = await manager.async_stop(rec_id)
            stopped.append({"recording_id": rec_id, "path": str(path) if path else None})
        return {"stopped": stopped}

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD,
        record,
        schema=RECORD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP,
        stop,
        schema=STOP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RadioRecorderConfigEntry) -> bool:
    """Resume interrupted recordings, then start following the calendar."""
    config = settings(entry)
    manager = RecordingManager(
        hass,
        entry,
        Path(config[CONF_OUTPUT_DIR]),
        owner=parse_owner(config.get(CONF_FILE_OWNER)),
    )
    await manager.async_load()

    scheduler = None
    if calendar := config.get(CONF_CALENDAR):
        scheduler = CalendarScheduler(
            hass,
            manager,
            calendar,
            lambda: stations(entry),
            timedelta(minutes=config.get(CONF_PAD_BEFORE, DEFAULT_PAD_BEFORE)),
            timedelta(minutes=config.get(CONF_PAD_AFTER, DEFAULT_PAD_AFTER)),
        )
    entry.runtime_data = RadioRecorderData(manager, scheduler)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if scheduler is not None:
        entry.async_on_unload(scheduler.async_stop)
        await scheduler.async_start()
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: RadioRecorderConfigEntry) -> None:
    """Options or stations changed. Recordings in progress resume after the reload."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RadioRecorderConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.manager.async_unload()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget stored recordings. Partial files stay in the output dir's .incoming folder."""
    await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()

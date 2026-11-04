"""Base entities: one device per station, plus one for the recorder itself."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import RadioRecorderConfigEntry, RadioRecorderData
from .const import DOMAIN, SIGNAL_UPDATED


class RadioRecorderEntity(Entity):
    """Updates whenever a recording starts, progresses or ends, or the schedule changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: RadioRecorderConfigEntry, key: str) -> None:
        self.data: RadioRecorderData = entry.runtime_data
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self._update()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self._update()
        self.async_write_ha_state()

    @callback
    def _update(self) -> None:
        """Set the _attr_ values from the manager or scheduler."""


class StationEntity(RadioRecorderEntity):
    """An entity on a station's device."""

    def __init__(self, entry: RadioRecorderConfigEntry, subentry: ConfigSubentry, key: str) -> None:
        super().__init__(entry, key)
        self.station: str = subentry.data[CONF_NAME]
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=self.station,
            entry_type=DeviceEntryType.SERVICE,
        )

"""Each station's last recording, and the next scheduled one."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RadioRecorderConfigEntry
from .const import SUBENTRY_STATION
from .entity import RadioRecorderEntity, StationEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RadioRecorderConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.runtime_data.scheduler is not None:
        async_add_entities([NextRecordingSensor(entry)])
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_STATION:
            async_add_entities(
                [LastRecordingSensor(entry, subentry)],
                config_subentry_id=subentry.subentry_id,
            )


class LastRecordingSensor(StationEntity, SensorEntity):
    """When the station's last recording finished; the file is in the attributes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry, subentry) -> None:
        super().__init__(entry, subentry, "last_recording")

    @callback
    def _update(self) -> None:
        last = self.data.manager.last.get(self.station)
        if last is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        self._attr_native_value = last.finished
        self._attr_extra_state_attributes = {
            "path": last.path,
            "show": last.show,
            "bytes": last.bytes,
        }


class NextRecordingSensor(RadioRecorderEntity, SensorEntity):
    """When the next calendar recording starts (padding included)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry) -> None:
        super().__init__(entry, "next_recording")

    @callback
    def _update(self) -> None:
        event = self.data.scheduler.next_event
        if event is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        self._attr_native_value = event.start
        self._attr_extra_state_attributes = {
            "show": event.show,
            "station": event.location,
            "ends": event.end.isoformat(),
        }

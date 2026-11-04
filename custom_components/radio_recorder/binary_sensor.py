"""Whether each station is recording right now."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RadioRecorderConfigEntry
from .const import SUBENTRY_STATION
from .entity import StationEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RadioRecorderConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_STATION:
            async_add_entities(
                [RecordingBinarySensor(entry, subentry)],
                config_subentry_id=subentry.subentry_id,
            )


class RecordingBinarySensor(StationEntity, BinarySensorEntity):
    """On while any recording of the station is in progress."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    # Progress updates change these every few minutes; keep them out of history.
    _unrecorded_attributes = frozenset({"bytes", "reconnects"})

    def __init__(self, entry, subentry) -> None:
        super().__init__(entry, subentry, "recording")

    @callback
    def _update(self) -> None:
        recordings = self.data.manager.recordings_for_station(self.station)
        self._attr_is_on = bool(recordings)
        if not recordings:
            self._attr_extra_state_attributes = {}
            return
        rec = max(recordings, key=lambda r: r.start)
        self._attr_extra_state_attributes = {
            "recording_id": rec.id,
            "show": rec.show,
            "started": rec.start.isoformat(),
            "ends": rec.end.isoformat(),
            "bytes": rec.bytes,
            "reconnects": rec.reconnects,
        }

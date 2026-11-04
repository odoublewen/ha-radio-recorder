"""Constants for the Radio Recorder integration."""

from typing import Final

DOMAIN: Final = "radio_recorder"

CONF_OUTPUT_DIR: Final = "output_dir"
CONF_CALENDAR: Final = "calendar_entity_id"
CONF_PAD_BEFORE: Final = "pad_before"
CONF_PAD_AFTER: Final = "pad_after"
CONF_FILE_OWNER: Final = "file_owner"

DEFAULT_OUTPUT_DIR: Final = "/media/music"
DEFAULT_PAD_BEFORE: Final = 1
DEFAULT_PAD_AFTER: Final = 2

SUBENTRY_STATION: Final = "station"

# Partial recordings live here, inside the output dir so the final move is a rename.
INCOMING_DIR: Final = ".incoming"

SERVICE_RECORD: Final = "record"
SERVICE_STOP: Final = "stop"
ATTR_STATION: Final = "station"
ATTR_SHOW: Final = "show"
ATTR_DURATION: Final = "duration"
ATTR_END: Final = "end"
ATTR_URL: Final = "url"
ATTR_RECORDING_ID: Final = "recording_id"

EVENT_FINISHED: Final = f"{DOMAIN}_finished"
EVENT_FAILED: Final = f"{DOMAIN}_failed"

SIGNAL_UPDATED: Final = f"{DOMAIN}_updated"

STORAGE_KEY: Final = f"{DOMAIN}.recordings"
STORAGE_VERSION: Final = 1

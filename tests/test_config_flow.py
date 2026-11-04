"""Config, options and station flows."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.radio_recorder.const import (
    CONF_CALENDAR,
    CONF_FILE_OWNER,
    CONF_OUTPUT_DIR,
    CONF_PAD_AFTER,
    CONF_PAD_BEFORE,
    DOMAIN,
    SUBENTRY_STATION,
)

from .conftest import CALENDAR, KALX_URL


def settings(tmp_path, **overrides):
    return {
        CONF_OUTPUT_DIR: str(tmp_path),
        CONF_CALENDAR: CALENDAR,
        CONF_PAD_BEFORE: 1,
        CONF_PAD_AFTER: 2,
        **overrides,
    }


async def test_user_flow(hass: HomeAssistant, tmp_path) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], settings(tmp_path, **{CONF_FILE_OWNER: " 568:568 "})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_OUTPUT_DIR: str(tmp_path),
        CONF_CALENDAR: CALENDAR,
        CONF_PAD_BEFORE: 1,
        CONF_PAD_AFTER: 2,
        CONF_FILE_OWNER: "568:568",
    }


async def test_user_flow_rejects_bad_dir_and_owner(hass: HomeAssistant, tmp_path) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        settings(tmp_path / "missing", **{CONF_FILE_OWNER: "apps"}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_OUTPUT_DIR: "output_dir_invalid",
        CONF_FILE_OWNER: "invalid_owner",
    }


async def test_only_one_entry(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_can_drop_the_calendar(
    hass: HomeAssistant, entry, calendar_events, tmp_path
) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    data = settings(tmp_path, **{CONF_PAD_AFTER: 5})
    del data[CONF_CALENDAR]
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_PAD_AFTER] == 5
    assert CONF_CALENDAR not in entry.options
    assert entry.runtime_data.scheduler is None


async def test_add_station(hass: HomeAssistant, entry, calendar_events) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_STATION), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "kalx", CONF_URL: "ftp://nope"}
    )
    assert result["errors"] == {CONF_NAME: "name_exists", CONF_URL: "invalid_url"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: " WFMU ", CONF_URL: "http://stream0.wfmu.org/freeform-128k"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    names = sorted(s.data[CONF_NAME] for s in entry.subentries.values())
    assert names == ["KALX", "WFMU"]
    assert hass.states.get("binary_sensor.wfmu_recording") is not None


async def test_reconfigure_station(hass: HomeAssistant, entry, calendar_events) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    (subentry,) = entry.subentries.values()
    result = await entry.start_subentry_reconfigure_flow(hass, subentry.subentry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "KALX", CONF_URL: KALX_URL.replace("256", "128")}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry.subentry_id].data[CONF_URL].endswith("kalx-128.mp3")

"""Config flow: where recordings go, which calendar schedules them, and the stations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME, CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import settings
from .const import (
    CONF_CALENDAR,
    CONF_FILE_OWNER,
    CONF_OUTPUT_DIR,
    CONF_PAD_AFTER,
    CONF_PAD_BEFORE,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PAD_AFTER,
    DEFAULT_PAD_BEFORE,
    DOMAIN,
    SUBENTRY_STATION,
)
from .tagging import parse_owner

PAD_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0, max=60, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX
    )
)


def _settings_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    calendar = defaults.get(CONF_CALENDAR)
    owner = defaults.get(CONF_FILE_OWNER)
    return vol.Schema(
        {
            vol.Required(
                CONF_OUTPUT_DIR, default=defaults.get(CONF_OUTPUT_DIR, DEFAULT_OUTPUT_DIR)
            ): str,
            vol.Optional(
                CONF_CALENDAR, description={"suggested_value": calendar} if calendar else None
            ): EntitySelector(EntitySelectorConfig(domain="calendar")),
            vol.Required(
                CONF_PAD_BEFORE, default=defaults.get(CONF_PAD_BEFORE, DEFAULT_PAD_BEFORE)
            ): PAD_SELECTOR,
            vol.Required(
                CONF_PAD_AFTER, default=defaults.get(CONF_PAD_AFTER, DEFAULT_PAD_AFTER)
            ): PAD_SELECTOR,
            vol.Optional(
                CONF_FILE_OWNER, description={"suggested_value": owner} if owner else None
            ): str,
        }
    )


def _writable_dir(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)


async def _validate_settings(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    data = {
        CONF_OUTPUT_DIR: str(Path(user_input[CONF_OUTPUT_DIR].strip())),
        CONF_PAD_BEFORE: int(user_input[CONF_PAD_BEFORE]),
        CONF_PAD_AFTER: int(user_input[CONF_PAD_AFTER]),
    }
    if calendar := user_input.get(CONF_CALENDAR):
        data[CONF_CALENDAR] = calendar
    if owner := (user_input.get(CONF_FILE_OWNER) or "").strip():
        data[CONF_FILE_OWNER] = owner
    errors: dict[str, str] = {}
    if not await hass.async_add_executor_job(_writable_dir, data[CONF_OUTPUT_DIR]):
        errors[CONF_OUTPUT_DIR] = "output_dir_invalid"
    try:
        parse_owner(owner)
    except ValueError:
        errors[CONF_FILE_OWNER] = "invalid_owner"
    return data, errors


class RadioRecorderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Radio Recorder."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate_settings(self.hass, user_input)
            if not errors:
                return self.async_create_entry(title="Radio Recorder", data=data)
        return self.async_show_form(
            step_id="user", data_schema=_settings_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RadioRecorderOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_STATION: StationSubentryFlow}


class RadioRecorderOptionsFlow(OptionsFlow):
    """Change the settings chosen at setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate_settings(self.hass, user_input)
            if not errors:
                return self.async_create_entry(data=data)
        current = user_input or settings(self.config_entry)
        return self.async_show_form(
            step_id="init", data_schema=_settings_schema(current), errors=errors
        )


class StationSubentryFlow(ConfigSubentryFlow):
    """A station: a name for event locations and actions, and its stream URL."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_step_form("user", user_input, None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        return await self._async_step_form("reconfigure", user_input, subentry.subentry_id)

    async def _async_step_form(
        self, step_id: str, user_input: dict[str, Any] | None, editing: str | None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_NAME: user_input[CONF_NAME].strip(),
                CONF_URL: user_input[CONF_URL].strip(),
            }
            errors = self._validate(data, editing)
            if not errors:
                if editing is None:
                    return self.async_create_entry(title=data[CONF_NAME], data=data)
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    title=data[CONF_NAME],
                    data=data,
                )
        if user_input is None and editing is not None:
            user_input = dict(self._get_reconfigure_subentry().data)
        defaults = user_input or {}
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, vol.UNDEFINED)): str,
                    vol.Required(
                        CONF_URL, default=defaults.get(CONF_URL, vol.UNDEFINED)
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                }
            ),
            errors=errors,
        )

    def _validate(self, data: dict[str, Any], editing: str | None) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not data[CONF_NAME]:
            errors[CONF_NAME] = "invalid_name"
        elif any(
            sub.subentry_type == SUBENTRY_STATION
            and sub.data.get(CONF_NAME, "").casefold() == data[CONF_NAME].casefold()
            and sub.subentry_id != editing
            for sub in self._get_entry().subentries.values()
        ):
            errors[CONF_NAME] = "name_exists"
        parsed = urlparse(data[CONF_URL])
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            errors[CONF_URL] = "invalid_url"
        return errors

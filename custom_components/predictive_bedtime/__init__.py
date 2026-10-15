"""Predictive Sleep: learns when each person goes to sleep from their work calendars."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ASLEEP,
    CONF_ASLEEP_STATES,
    CONF_BED_SENSOR,
    CONF_CALENDAR_RULES,
    CONF_CALENDARS,
    CONF_KEYWORDS,
    CONF_IN_BED,
    DOMAIN,
    SIGNAL_UPDATED,
    TITLE_SUFFIX,
)
from .coordinator import BedtimeConfigEntry, BedtimeCoordinator
from .household import is_household
from .model import DEFAULT_ASLEEP_VALUES, MODE_MIXED, MODE_WORK

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
HOUSEHOLD_PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

FORGET_SCHEMA = vol.Schema({vol.Required("config_entry_id"): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def forget_last_night(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or not isinstance(getattr(entry, "runtime_data", None), BedtimeCoordinator)
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        entry.runtime_data.async_forget_night()

    hass.services.async_register(DOMAIN, "forget_last_night", forget_last_night, FORGET_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BedtimeConfigEntry) -> bool:
    if is_household(entry):
        entry.runtime_data = None
        await hass.config_entries.async_forward_entry_setups(entry, HOUSEHOLD_PLATFORMS)
        return True

    coordinator = BedtimeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_tracking()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    # Let household sensors pick up the new person straight away.
    async_dispatcher_send(hass, SIGNAL_UPDATED)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: BedtimeConfigEntry) -> bool:
    if entry.version == 1:
        # One bed sensor became lists of in-bed signals and sleep trackers.
        options = dict(entry.options)
        bed = options.pop(CONF_BED_SENSOR, None)
        options[CONF_IN_BED] = [bed] if bed else []
        options.setdefault(CONF_ASLEEP, [])
        options.setdefault(CONF_ASLEEP_STATES, list(DEFAULT_ASLEEP_VALUES))
        hass.config_entries.async_update_entry(entry, options=options, version=2)
    if entry.version in (2, 3):
        # Titles became "<first name> <suffix>". A title the user chose is kept.
        title = entry.title.strip()
        if entry.version == 2:
            title = f"{title.split()[0]} {TITLE_SUFFIX}" if title else title
        hass.config_entries.async_update_entry(entry, title=title, version=4)
    if entry.version == 4:
        # The display name became Predictive Sleep.
        title = entry.title
        if title.endswith(" Predictive Bedtime"):
            title = title.removesuffix(" Predictive Bedtime") + f" {TITLE_SUFFIX}"
        hass.config_entries.async_update_entry(entry, title=title, version=5)
    if entry.version == 5:
        # Each calendar gets its own rule instead of one keyword list for all of them.
        options = dict(entry.options)
        words = options.pop(CONF_KEYWORDS, None) or []
        options[CONF_CALENDAR_RULES] = {
            calendar: (
                {"mode": MODE_MIXED, "words": words, "require_name": False}
                if words
                else {"mode": MODE_WORK}
            )
            for calendar in options.get(CONF_CALENDARS, [])
        }
        hass.config_entries.async_update_entry(entry, options=options, version=6)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BedtimeConfigEntry) -> bool:
    platforms = HOUSEHOLD_PLATFORMS if is_household(entry) else PLATFORMS
    unloaded = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unloaded and not is_household(entry):
        # The person is gone; household sensors recompute without them.
        hass.loop.call_soon(async_dispatcher_send, hass, SIGNAL_UPDATED)
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: BedtimeConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

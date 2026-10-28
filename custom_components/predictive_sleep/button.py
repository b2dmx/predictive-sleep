"""Forget-last-night button, for a night that was not normal."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BedtimeConfigEntry
from .entity import BedtimeEntity

FORGET = ButtonEntityDescription(key="forget_last_night", translation_key="forget_last_night")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedtimeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ForgetLastNightButton(entry.runtime_data, FORGET)])


class ForgetLastNightButton(BedtimeEntity, ButtonEntity):
    async def async_press(self) -> None:
        self.coordinator.async_forget_night()

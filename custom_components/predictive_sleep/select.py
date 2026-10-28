"""Skip a night: pick any night from the last week to leave out of learning."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import BedtimeConfigEntry
from .entity import BedtimeEntity

SKIP = SelectEntityDescription(
    key="skip_night", translation_key="skip_night", entity_category=EntityCategory.CONFIG
)
# Shown when nothing is being skipped; picking a night skips it and returns here.
NONE = "—"
WINDOW = timedelta(days=7)


def _label(onset: datetime) -> str:
    """'Sat Oct 17, 11:46 PM': the night by when it began, in local time."""
    t = dt_util.as_local(onset)
    hour = t.hour % 12 or 12
    return f"{t:%a %b} {t.day}, {hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedtimeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([SkipNightSelect(entry.runtime_data, SKIP)])


class SkipNightSelect(BedtimeEntity, SelectEntity):
    _attr_current_option = NONE

    def _nights(self) -> dict[str, datetime]:
        cutoff = dt_util.utcnow() - WINDOW
        recent = [e for e in self.coordinator.episodes if e.onset >= cutoff]
        return {_label(e.onset): e.onset for e in sorted(recent, key=lambda e: e.onset, reverse=True)}

    @property
    def options(self) -> list[str]:
        return [NONE, *self._nights()]

    async def async_select_option(self, option: str) -> None:
        if (onset := self._nights().get(option)) is not None:
            self.coordinator.async_forget_night(onset)
        self._attr_current_option = NONE
        self.async_write_ha_state()

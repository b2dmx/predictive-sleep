"""Switches: Learning, for nights that should not count, and In bed for the night, for anyone
without a sensor (or to confirm one)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BedtimeConfigEntry
from .entity import BedtimeEntity

LEARNING = SwitchEntityDescription(
    key="learning", translation_key="learning", entity_category=EntityCategory.CONFIG
)
IN_BED = SwitchEntityDescription(key="in_bed", translation_key="in_bed")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedtimeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [LearningSwitch(entry.runtime_data, LEARNING), InBedSwitch(entry.runtime_data, IN_BED)]
    )


class LearningSwitch(BedtimeEntity, SwitchEntity):
    @property
    def is_on(self) -> bool:
        return self.coordinator.learning_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        paused_by = self.coordinator.paused_by
        return {
            "learning_now": paused_by is None,
            "paused_by": None if paused_by == "switch" else paused_by,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_set_learning(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_set_learning(False)


class InBedSwitch(BedtimeEntity, SwitchEntity):
    """On when going to bed, off when getting up. Counts like a sleep tracker."""

    @property
    def is_on(self) -> bool:
        return self.coordinator.manual_in_bed

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_set_in_bed(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_set_in_bed(False)

"""Binary sensors: expected asleep and wind-down window."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BedtimeConfigEntry
from .entity import BedtimeEntity
from .household import binary_sensors, is_household

EXPECTED_ASLEEP = BinarySensorEntityDescription(key="expected_asleep", translation_key="expected_asleep")
WIND_DOWN = BinarySensorEntityDescription(key="wind_down", translation_key="wind_down")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedtimeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if is_household(entry):
        async_add_entities(binary_sensors(entry))
        return
    coordinator = entry.runtime_data
    async_add_entities([ExpectedAsleepSensor(coordinator, EXPECTED_ASLEEP), WindDownSensor(coordinator, WIND_DOWN)])


class ExpectedAsleepSensor(BedtimeEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool:
        return self.coordinator.expected_asleep

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        return {"detected": self.coordinator.detector.asleep}


class WindDownSensor(BedtimeEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool:
        return self.coordinator.winding_down

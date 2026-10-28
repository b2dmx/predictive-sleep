"""Shared entity base."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import BedtimeCoordinator

# Entity IDs are fixed as "<device>_<this>", e.g. sensor.sam_predictive_sleep_next_bedtime,
# so putting the device in a room doesn't add the room to entities created later.
OBJECT_IDS = {
    "predicted_bedtime": "next_bedtime",
    "expected_wake": "next_wake_up",
    "confidence": "prediction_confidence",
    "accuracy": "prediction_error",
    "last_sleep": "last_bedtime",
    "last_wake": "last_wake_up",
    "last_sleep_duration": "last_sleep_duration",
    "expected_asleep": "expected_asleep",
    "wind_down": "wind_down",
    "learning": "learning",
    "in_bed": "in_bed_for_the_night",
    "forget_last_night": "forget_last_sleep",
    "skip_night": "skip_a_night",
    "household_expected_asleep": "everyone_expected_asleep",
    "anyone_winding_down": "anyone_winding_down",
    "first_bedtime": "earliest_bedtime",
    "last_bedtime": "latest_bedtime",
}
_DOMAINS = (
    (BinarySensorEntity, "binary_sensor"),
    (SensorEntity, "sensor"),
    (SwitchEntity, "switch"),
    (ButtonEntity, "button"),
    (SelectEntity, "select"),
)


def pin_entity_id(entity: Entity, device_name: str, key: str) -> None:
    """Only applies to new entities: ones already registered keep their ID."""
    domain = next((d for cls, d in _DOMAINS if isinstance(entity, cls)), None)
    if domain and key in OBJECT_IDS:
        entity.entity_id = f"{domain}.{slugify(device_name)}_{OBJECT_IDS[key]}"


class BedtimeEntity(CoordinatorEntity[BedtimeCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BedtimeCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )
        pin_entity_id(self, entry.title, description.key)

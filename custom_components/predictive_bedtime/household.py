"""Household sensors: whole-home views across everyone who is set up.

Optional, for homes without their own home-mode logic. They read the person entries'
predictions and update whenever any of them, or anyone's presence, changes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN, KIND_HOUSEHOLD, SIGNAL_UPDATED
from .coordinator import BedtimeCoordinator


def is_household(entry: ConfigEntry) -> bool:
    return entry.data.get("kind") == KIND_HOUSEHOLD


def people(hass: HomeAssistant) -> list[BedtimeCoordinator]:
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and isinstance(getattr(entry, "runtime_data", None), BedtimeCoordinator)
    ]


class HouseholdEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, description: EntityDescription) -> None:
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._async_refresh)
        )
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._async_refresh, event_filter=self._is_presence
            )
        )

    @callback
    def _is_presence(self, event_data: Any) -> bool:
        return str(event_data.get("entity_id", "")).startswith("person.")

    @callback
    def _async_refresh(self, *_: Event | None) -> None:
        self.async_write_ha_state()


# --- binary sensors -----------------------------------------------------

EVERYONE_ASLEEP = BinarySensorEntityDescription(
    key="household_expected_asleep", translation_key="household_expected_asleep"
)
ANYONE_WINDING_DOWN = BinarySensorEntityDescription(
    key="anyone_winding_down", translation_key="anyone_winding_down"
)


class EveryoneAsleepSensor(HouseholdEntity, BinarySensorEntity):
    """On when everyone who is home is expected asleep."""

    @property
    def is_on(self) -> bool:
        home = [c for c in people(self.hass) if c.is_home]
        return bool(home) and all(c.expected_asleep for c in home)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        home = [c for c in people(self.hass) if c.is_home]
        return {
            "asleep": [c.first_name for c in home if c.expected_asleep],
            "awake": [c.first_name for c in home if not c.expected_asleep],
        }


class AnyoneWindingDownSensor(HouseholdEntity, BinarySensorEntity):
    """On when anyone who is home is winding down."""

    @property
    def is_on(self) -> bool:
        return any(c.winding_down for c in people(self.hass) if c.is_home)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "people": [c.first_name for c in people(self.hass) if c.is_home and c.winding_down]
        }


# --- sensors --------------------------------------------------------------

FIRST_BEDTIME = SensorEntityDescription(
    key="first_bedtime", translation_key="first_bedtime", device_class=SensorDeviceClass.TIMESTAMP
)
LAST_BEDTIME = SensorEntityDescription(
    key="last_bedtime", translation_key="last_bedtime", device_class=SensorDeviceClass.TIMESTAMP
)


class BedtimeOrderSensor(HouseholdEntity, SensorEntity):
    """Earliest or latest predicted bedtime across everyone."""

    def __init__(self, entry: ConfigEntry, description: SensorEntityDescription, last: bool) -> None:
        super().__init__(entry, description)
        self._last = last

    def _pick(self) -> BedtimeCoordinator | None:
        known = [c for c in people(self.hass) if c.data is not None]
        if not known:
            return None
        return (max if self._last else min)(known, key=lambda c: c.data.bedtime)

    @property
    def native_value(self) -> datetime | None:
        picked = self._pick()
        return picked.data.bedtime if picked else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        picked = self._pick()
        return {"person": picked.first_name if picked else None}


def binary_sensors(entry: ConfigEntry) -> list[Entity]:
    return [EveryoneAsleepSensor(entry, EVERYONE_ASLEEP), AnyoneWindingDownSensor(entry, ANYONE_WINDING_DOWN)]


def sensors(entry: ConfigEntry) -> list[Entity]:
    return [
        BedtimeOrderSensor(entry, FIRST_BEDTIME, last=False),
        BedtimeOrderSensor(entry, LAST_BEDTIME, last=True),
    ]

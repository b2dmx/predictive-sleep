"""Sensors: next and last bedtime and wake-up, sleep length, prediction quality."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BedtimeConfigEntry, BedtimeCoordinator
from .entity import BedtimeEntity
from .household import sensors, is_household
from .model import accuracy


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _bedtime_attrs(c: BedtimeCoordinator) -> dict[str, Any]:
    p = c.data
    if p is None:
        return {}
    return {
        "schedule_bedtime": _iso(p.schedule_bedtime),
        "previous_shift_end": _iso(p.prev_end),
        "next_shift_start": _iso(p.next_start),
        "wake_by": _iso(p.next_start - p.prep) if p.next_start else None,
        # Learned habits (setup values until enough nights accumulate).
        "usual_bedtime": f"{int(p.usual_bedtime):02d}:{round(p.usual_bedtime % 1 * 60) % 60:02d}",
        "sleep_need_hours": round(p.target_sleep.total_seconds() / 3600, 2),
        "unwind_minutes": round(p.unwind.total_seconds() / 60),
        "get_ready_minutes": round(p.prep.total_seconds() / 60),
        "nights_learned": len(c.episodes),
        # Held from wind-down until the expected wake-up, rather than drifting.
        "locked": c.committed is not None,
    }


def _hours(c: BedtimeCoordinator) -> float | None:
    if not c.episodes:
        return None
    e = c.episodes[-1]
    return round((e.wake - e.onset).total_seconds() / 3600, 2)


def _last_sleep_attrs(c: BedtimeCoordinator) -> dict[str, Any]:
    if not c.episodes:
        return {}
    e = c.episodes[-1]
    return {"source": e.source}


def _accuracy_value(c: BedtimeCoordinator) -> float | None:
    result = accuracy(c.episodes)
    return round(result[0]) if result else None


def _accuracy_attrs(c: BedtimeCoordinator) -> dict[str, Any]:
    result = accuracy(c.episodes)
    last = next((e for e in reversed(c.episodes) if e.predicted is not None), None)
    return {
        "nights_measured": result[1] if result else 0,
        # Positive: went to bed later than predicted.
        "previous_error_minutes": (
            round((last.onset - last.predicted).total_seconds() / 60) if last else None
        ),
    }


@dataclass(frozen=True, kw_only=True)
class BedtimeSensorDescription(SensorEntityDescription):
    value_fn: Callable[[BedtimeCoordinator], Any]
    attrs_fn: Callable[[BedtimeCoordinator], dict[str, Any]] | None = None


SENSORS = (
    BedtimeSensorDescription(
        key="predicted_bedtime",
        translation_key="predicted_bedtime",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.data.bedtime if c.data else None,
        attrs_fn=_bedtime_attrs,
    ),
    BedtimeSensorDescription(
        key="expected_wake",
        translation_key="expected_wake",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.data.wake if c.data else None,
    ),
    BedtimeSensorDescription(
        key="confidence",
        translation_key="confidence",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: round(c.data.confidence * 100) if c.data else None,
    ),
    BedtimeSensorDescription(
        key="accuracy",
        translation_key="accuracy",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_accuracy_value,
        attrs_fn=_accuracy_attrs,
    ),
    BedtimeSensorDescription(
        key="last_sleep",
        translation_key="last_sleep",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.episodes[-1].onset if c.episodes else None,
        attrs_fn=_last_sleep_attrs,
    ),
    BedtimeSensorDescription(
        key="last_wake",
        translation_key="last_wake",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.episodes[-1].wake if c.episodes else None,
    ),
    BedtimeSensorDescription(
        key="last_sleep_duration",
        translation_key="last_sleep_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_hours,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedtimeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if is_household(entry):
        async_add_entities(sensors(entry))
        return
    async_add_entities(BedtimeSensor(entry.runtime_data, d) for d in SENSORS)


class BedtimeSensor(BedtimeEntity, SensorEntity):
    entity_description: BedtimeSensorDescription

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator) if fn else None

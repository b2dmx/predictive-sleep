"""Diagnostics: everything the model has learned, in local time, for checking its numbers."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import BedtimeConfigEntry, BedtimeCoordinator
from .model import accuracy, is_free_night, is_get_ready_morning, learn


def _local(value: datetime | None) -> str | None:
    return dt_util.as_local(value).strftime("%a %Y-%m-%d %H:%M") if value else None


def _minutes(delta) -> int | None:
    return round(delta.total_seconds() / 60) if delta is not None else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BedtimeConfigEntry
) -> dict[str, Any]:
    coordinator = getattr(entry, "runtime_data", None)
    if not isinstance(coordinator, BedtimeCoordinator):
        return {"kind": entry.data.get("kind"), "options": dict(entry.options)}

    now = dt_util.utcnow()
    p = coordinator.params
    habits = learn(now, coordinator.episodes, p, dt_util.get_default_time_zone())
    usual = f"{int(habits.usual_bedtime):02d}:{round(habits.usual_bedtime % 1 * 60) % 60:02d}"
    prediction = coordinator.data
    result = accuracy(coordinator.episodes)

    nights = []
    for e in coordinator.episodes:
        nights.append(
            {
                "bedtime": _local(e.onset),
                "wake_up": _local(e.wake),
                "hours": round((e.wake - e.onset).total_seconds() / 3600, 2),
                "source": e.source,
                "previous_shift_ended": _local(e.prev_end),
                "next_commitment": _local(e.next_start),
                "left_home": _local(e.left_home),
                "counts_for_get_ready": is_get_ready_morning(e),
                "free_night": is_free_night(e),
                "wake_to_next_commitment_min": (
                    _minutes(e.next_start - e.wake) if e.next_start else None
                ),
                "predicted_bedtime": _local(e.predicted),
                "error_min": _minutes(e.onset - e.predicted) if e.predicted else None,
            }
        )

    return {
        "options": dict(entry.options),
        "learning": {
            "enabled": coordinator.learning_enabled,
            "paused_by": coordinator.paused_by,
            "nights": len(coordinator.episodes),
            "habits": {
                "usual_bedtime": {"setup": p.free_bedtime.strftime("%H:%M"), "learned": usual, "nights": habits.free_nights},
                "sleep_need_hours": {"setup": round(p.target_sleep.total_seconds() / 3600, 2), "learned": round(habits.target_sleep.total_seconds() / 3600, 2), "nights": habits.sleep_nights},
                "unwind_min": {"setup": _minutes(p.unwind), "learned": _minutes(habits.unwind), "nights": habits.unwind_nights},
                "get_ready_min": {"setup": _minutes(p.prep), "learned": _minutes(habits.prep), "mornings": habits.prep_mornings},
            },
            "prediction_error_min": round(result[0]) if result else None,
            "prediction_error_nights": result[1] if result else 0,
        },
        "prediction": (
            {
                "bedtime": _local(prediction.bedtime),
                "wake_up": _local(prediction.wake),
                "schedule_only_bedtime": _local(prediction.schedule_bedtime),
                "confidence": round(prediction.confidence * 100),
                "get_ready_min": _minutes(prediction.prep),
                "previous_shift_ended": _local(prediction.prev_end),
                "next_commitment": _local(prediction.next_start),
            }
            if prediction
            else None
        ),
        "upcoming_commitments": [
            {"start": _local(s.start), "end": _local(s.end), "kind": s.kind}
            for s in coordinator.shifts
            if s.end >= now
        ],
        "nights": nights,
    }

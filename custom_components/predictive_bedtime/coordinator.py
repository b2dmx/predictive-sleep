"""Coordinator: watches the sleep signals, reads the calendars, remembers recent nights, runs the model.

Nothing polls. Work happens when something changes (a sleep signal, presence, a calendar entity), at
the moments a prediction says something is due (wind-down, bedtime, wake), and at most a
day after the calendars were last read.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, time, timedelta
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_HOME
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_DAYS,
    CALENDAR_MAX_AGE,
    CALENDAR_RETRY,
    CONF_ASLEEP,
    CONF_ASLEEP_STATES,
    CONF_CALENDARS,
    CONF_FREE_BEDTIME,
    CONF_IN_BED,
    CONF_CALENDAR_RULES,
    CONF_MIN_SLEEP,
    CONF_NOTIFY,
    CONF_PAUSE_ENTITIES,
    CONF_PAUSE_STATES,
    CONF_PERSON,
    CONF_PREP,
    CONF_RETENTION,
    CONF_SETTLE,
    CONF_SLEEP_DEBT,
    CONF_TARGET_SLEEP,
    CONF_UNWIND,
    CONF_WAKE_GAP,
    CONF_WIND_DOWN,
    DEFAULT_OPTIONS,
    DEFAULT_PAUSE_STATES,
    DOMAIN,
    HALF_LIFE_FRACTION,
    SIGNAL_UPDATED,
    NOTIFY_ACTION_PREFIX,
    SHIFT_RETENTION,
    STORAGE_VERSION,
)
from .model import (
    DEFAULT_ASLEEP_VALUES,
    Episode,
    Params,
    Prediction,
    Shift,
    DEPART_MAX,
    MODE_WORK,
    SleepDetector,
    classify,
    combine,
    first_departure,
    make_episode,
    neighbours,
    predict,
)

_LOGGER = logging.getLogger(__name__)

# A prediction counts for a sleep that started within this long of it.
MATCH_WINDOW = timedelta(hours=6)
# How far back missing predictions are recovered from the recorder.
RECOVER_WINDOW = timedelta(days=30)
# Time in bed counts as going to sleep only this close to the predicted bedtime (or once
# it has lasted a full sleep): reading in bed in the afternoon is not bedtime.
ATTEMPT_WINDOW = timedelta(hours=2)
# A calendar that has not appeared for this long gets a repair notice.
MISSING_CALENDAR_GRACE = timedelta(minutes=15)
# Bumped when stored nights need re-describing against the calendar.
HISTORY_VERSION = 2

type BedtimeConfigEntry = ConfigEntry[BedtimeCoordinator]


def params_from_options(options: dict[str, Any]) -> Params:
    o = {**DEFAULT_OPTIONS, **options}
    hour, minute = (int(x) for x in str(o[CONF_FREE_BEDTIME]).split(":")[:2])
    return Params(
        target_sleep=timedelta(hours=float(o[CONF_TARGET_SLEEP])),
        prep=timedelta(minutes=float(o[CONF_PREP])),
        unwind=timedelta(minutes=float(o[CONF_UNWIND])),
        wind_down=timedelta(minutes=float(o[CONF_WIND_DOWN])),
        free_bedtime=time(hour, minute),
        settle=timedelta(minutes=float(o[CONF_SETTLE])),
        wake_gap=timedelta(minutes=float(o[CONF_WAKE_GAP])),
        min_sleep=timedelta(hours=float(o[CONF_MIN_SLEEP])),
        half_life_days=float(o[CONF_RETENTION]) * HALF_LIFE_FRACTION,
        use_sleep_debt=bool(o[CONF_SLEEP_DEBT]),
    )


class BedtimeCoordinator(DataUpdateCoordinator[Prediction]):
    """One per person."""

    config_entry: BedtimeConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BedtimeConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.title}", update_interval=None
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.episodes: list[Episode] = []
        self.shifts: list[Shift] = []
        self.detector = SleepDetector()
        self.last_wake: datetime | None = None
        # Once wind-down starts the prediction is held until the expected wake.
        self.committed: Prediction | None = None
        self._last_fetch: datetime | None = None
        self._calendars_changed = True
        self._unsub_detector: Callable[[], None] | None = None
        self._unsub_next: Callable[[], None] | None = None
        self._needs_backfill = False
        self._retry_fetch_at: datetime | None = None
        # The Learning switch; pause conditions can also stop learning.
        self.learning_enabled = True
        self._night_paused = False
        # The bedtime that was predicted for the sleep in progress, captured when it starts:
        # by wake-up the prediction has already moved on to the next night.
        self._night_predicted: datetime | None = None
        # Combined sleep reading and when it last flipped.
        self._on: bool | None = None
        self._since: datetime | None = None
        # Recent nights before this were already checked against the recorder.
        self._recovered_until: datetime | None = None
        self._history_version = HISTORY_VERSION
        # Use cached shifts for the first refresh; read the calendars right after setup.
        self._defer_fetch = False
        self._missing_since: datetime | None = None
        self._shown: tuple[bool, bool] | None = None

    # --- configuration -------------------------------------------------

    def _conf(self, key: str) -> Any:
        return self.config_entry.options.get(key, self.config_entry.data.get(key))

    @property
    def person(self) -> str:
        return self.config_entry.data[CONF_PERSON]

    @property
    def first_name(self) -> str:
        return self.config_entry.title.split()[0] if self.config_entry.title else ""

    @property
    def is_home(self) -> bool:
        state = self.hass.states.get(self.person)
        return state is None or state.state == STATE_HOME

    @callback
    def async_update_listeners(self) -> None:
        super().async_update_listeners()
        # Household sensors follow every person.
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    @property
    def in_bed_signals(self) -> list[str]:
        return list(self._conf(CONF_IN_BED) or [])

    @property
    def asleep_signals(self) -> list[str]:
        return list(self._conf(CONF_ASLEEP) or [])

    @property
    def asleep_values(self) -> list[str]:
        return list(self._conf(CONF_ASLEEP_STATES) or DEFAULT_ASLEEP_VALUES)

    @property
    def pause_entities(self) -> list[str]:
        return list(self._conf(CONF_PAUSE_ENTITIES) or [])

    @property
    def paused_by(self) -> str | None:
        """Why learning is off right now, or None if it is on."""
        if not self.learning_enabled:
            return "switch"
        states = {s.lower() for s in (self._conf(CONF_PAUSE_STATES) or DEFAULT_PAUSE_STATES)}
        for entity_id in self.pause_entities:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state.lower() in states:
                return entity_id
        return None

    @property
    def signals(self) -> list[str]:
        return [*self.in_bed_signals, *self.asleep_signals]

    def _reading(self, state_of: Callable[[str], str | None]) -> tuple[bool, bool, bool]:
        person = state_of(self.person)
        return combine(
            (state_of(e) for e in self.asleep_signals),
            (state_of(e) for e in self.in_bed_signals),
            person is None or person == STATE_HOME,
            self.asleep_values,
        )

    @property
    def calendars(self) -> list[str]:
        return list(self._conf(CONF_CALENDARS))

    @property
    def calendar_rules(self) -> dict[str, dict[str, Any]]:
        return dict(self._conf(CONF_CALENDAR_RULES) or {})

    @property
    def params(self) -> Params:
        return params_from_options(dict(self.config_entry.options))

    @property
    def retention(self) -> timedelta:
        return timedelta(days=float(self.config_entry.options.get(CONF_RETENTION, DEFAULT_OPTIONS[CONF_RETENTION])))

    # --- lifecycle -----------------------------------------------------

    async def _async_setup(self) -> None:
        stored = await self._store.async_load()
        if stored is None:
            # Reading history can take minutes on slow hardware; do it after setup.
            self._needs_backfill = True
            return
        self.episodes = [Episode.from_dict(e) for e in stored.get("episodes", [])]
        self._history_version = stored.get("history_version", 1)
        if stored.get("recovered_until"):
            self._recovered_until = dt_util.parse_datetime(stored["recovered_until"])
        self._forget_old_nights()
        self.shifts = [
            Shift(dt_util.parse_datetime(row[0]), dt_util.parse_datetime(row[1]), *row[2:3])
            for row in stored.get("shifts", [])
        ]
        self.detector.restore(stored.get("detector", {}))
        self.last_wake = dt_util.parse_datetime(stored["last_wake"]) if stored.get("last_wake") else None
        if stored.get("committed"):
            self.committed = Prediction.from_dict(stored["committed"])
        self.learning_enabled = stored.get("learning_enabled", True)
        self._night_paused = stored.get("night_paused", False)
        if stored.get("night_predicted"):
            self._night_predicted = dt_util.parse_datetime(stored["night_predicted"])
        self._defer_fetch = bool(self.shifts)

    @callback
    def async_start_tracking(self) -> None:
        entry = self.config_entry
        entry.async_on_unload(
            async_track_state_change_event(
                self.hass,
                [*self.signals, self.person, *self.pause_entities],
                self._async_on_signal,
            )
        )
        entry.async_on_unload(
            async_track_state_change_event(self.hass, self.calendars, self._async_on_calendar)
        )
        entry.async_on_unload(
            self.hass.bus.async_listen(
                "mobile_app_notification_action", self._async_on_notification_action
            )
        )
        entry.async_on_unload(self._cancel_timers)
        self._async_evaluate()
        if self._needs_backfill:
            self._needs_backfill = False
            entry.async_create_background_task(
                self.hass, self._async_run_backfill(), f"{DOMAIN} backfill {entry.title}"
            )
            return
        # The first refresh used cached shifts so setup never waits on a slow calendar.
        self.hass.async_create_task(self.async_request_refresh())
        entry.async_create_background_task(
            self.hass, self._async_maintain(), f"{DOMAIN} maintain {entry.title}"
        )

    async def _async_maintain(self) -> None:
        """One-off history work after setup, one job at a time so neither undoes the other."""
        # Calendars can finish loading well after this integration at startup.
        for _ in range(40):
            if all(self.hass.states.get(c) is not None for c in self.calendars):
                break
            await asyncio.sleep(15)
        if self._history_version < HISTORY_VERSION and self.episodes:
            await self._async_redescribe_nights()
        if any(e.predicted is None or e.left_home is None for e in self._unchecked_nights()):
            await self._async_recover_history()

    async def _async_run_backfill(self) -> None:
        await self._async_backfill()
        self._save()
        # Departures (for get-ready time) come from presence history, not the bed.
        await self._async_recover_history()
        await self.async_request_refresh()

    @callback
    def _cancel_timers(self) -> None:
        for unsub in (self._unsub_detector, self._unsub_next):
            if unsub:
                unsub()
        self._unsub_detector = self._unsub_next = None

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 10)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "episodes": [e.as_dict() for e in self.episodes],
            "shifts": [[s.start.isoformat(), s.end.isoformat(), s.kind] for s in self.shifts],
            "detector": self.detector.as_dict(),
            "last_wake": self.last_wake.isoformat() if self.last_wake else None,
            "committed": self.committed.as_dict() if self.committed else None,
            "learning_enabled": self.learning_enabled,
            "night_paused": self._night_paused,
            "night_predicted": (
                self._night_predicted.isoformat() if self._night_predicted else None
            ),
            "recovered_until": (
                self._recovered_until.isoformat() if self._recovered_until else None
            ),
            "history_version": self._history_version,
        }

    def _forget_old_nights(self) -> None:
        cutoff = dt_util.utcnow() - self.retention
        self.episodes = [e for e in self.episodes if e.onset >= cutoff]

    # --- sleep detection -----------------------------------------------

    @callback
    def _async_on_signal(self, event: Event[EventStateChangedData]) -> None:
        self._async_evaluate()

    @callback
    def _async_evaluate(self, *_: Any) -> None:
        if self._unsub_detector:
            self._unsub_detector()
            self._unsub_detector = None

        def state_of(entity_id: str) -> str | None:
            state = self.hass.states.get(entity_id)
            return state.state if state else None

        self._note_departure()
        on, sure, known = self._reading(state_of)
        # Signals dropping off WiFi say nothing about whether anyone is asleep.
        if not known:
            return
        now = dt_util.utcnow()
        if self._on is None:
            self._on, self._since = on, self._initial_since(on)
        elif on != self._on:
            self._on, self._since = on, now
        since = self._since or now
        p = self.params

        was_asleep = self.detector.asleep
        finished = self.detector.step(now, on, since, sure, p)
        if self.detector.asleep and not was_asleep:
            self._night_paused = self.paused_by is not None
            self._night_predicted = self._prediction_for(self.detector.onset)
        if finished:
            if self._night_paused or self.paused_by is not None:
                _LOGGER.debug("%s: learning paused, night not recorded", self.config_entry.title)
                self.last_wake = finished[1]
                self.committed = None
            else:
                self._record(*finished)
                self._async_notify_recorded(self.episodes[-1])
            self._night_paused = False
            self._night_predicted = None
        shown = (self.sleep_attempt, self.is_home)
        if finished or was_asleep != self.detector.asleep:
            self._save()
            self.async_update_listeners()
            self.hass.async_create_task(self.async_request_refresh())
        elif shown != self._shown:
            self.async_update_listeners()
            if shown[0] != (self._shown or (False,))[0]:
                self.hass.async_create_task(self.async_request_refresh())
        self._shown = shown

        # Check again the moment a pending stretch would cross its threshold.
        due: datetime | None = None
        if not self.detector.asleep and on and not sure:
            due = since + p.settle
        elif self.detector.asleep and not on:
            due = since + p.wake_gap
        elif self.detector.asleep and not self.sleep_attempt and self.detector.onset:
            # In bed early: it becomes a sleep attempt when bedtime nears or it lasts.
            moments = [self.detector.onset + p.min_sleep]
            if (guess := self.committed or self.data) is not None:
                moments.append(guess.bedtime - ATTEMPT_WINDOW)
            due = min(moments)
        if due:
            self._unsub_detector = async_track_point_in_utc_time(
                self.hass, self._async_evaluate, max(due, now) + timedelta(seconds=1)
            )

    def _note_departure(self) -> None:
        """Record when they left home after the last sleep, if it was soon after waking."""
        if not self.episodes or self.episodes[-1].left_home is not None:
            return
        last = self.episodes[-1]
        person = self.hass.states.get(self.person)
        if person is None or person.state in (STATE_HOME, "unavailable", "unknown"):
            return
        left = person.last_changed
        if last.wake < left <= last.wake + DEPART_MAX:
            self.episodes[-1] = replace(last, left_home=left)
            self._save()

    def _initial_since(self, on: bool) -> datetime:
        """Best guess at when the current reading began, from the signals' own history."""
        changed = [
            state.last_changed
            for entity_id in self.signals
            if (state := self.hass.states.get(entity_id)) is not None
        ]
        if not changed:
            return dt_util.utcnow()
        return min(changed) if on else max(changed)

    def _prediction_for(self, onset: datetime | None) -> datetime | None:
        """The current prediction, if it was about the sleep that began at onset."""
        guess = self.committed or self.data
        if guess is None or onset is None:
            return None
        return guess.bedtime if abs(guess.bedtime - onset) <= MATCH_WINDOW else None

    def _record(self, onset: datetime, wake: datetime, source: str) -> None:
        predicted = self._night_predicted or self._prediction_for(onset)
        self.episodes = [
            *self.episodes,
            make_episode(onset, wake, self.shifts, source, predicted),
        ]
        self._forget_old_nights()
        self.last_wake = wake
        self.committed = None
        _LOGGER.debug("%s: recorded sleep %s -> %s", self.config_entry.title, onset, wake)

    def _recent_nights(self) -> list[Episode]:
        cutoff = dt_util.utcnow() - RECOVER_WINDOW
        return [e for e in self.episodes if e.onset >= cutoff]

    def _unchecked_nights(self) -> list[Episode]:
        """Recent nights the recorder has not been checked for yet."""
        after = self._recovered_until
        return [e for e in self._recent_nights() if after is None or e.onset > after]

    async def _async_recover_history(self) -> None:
        """Fill in recent nights from the recorder: what was predicted, and when they left."""
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

        recent = self._unchecked_nights()
        if not recent:
            return
        start = min(e.onset for e in recent) - timedelta(days=1)
        prediction_entity = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self.config_entry.entry_id}_predicted_bedtime"
        )
        recorder = get_instance(self.hass)
        try:
            predictions = (
                await recorder.async_add_executor_job(
                    self._read_entity_history, prediction_entity, start, dt_util.utcnow()
                )
                if prediction_entity
                else []
            )
            presence = await recorder.async_add_executor_job(
                self._read_entity_history, self.person, start, dt_util.utcnow()
            )
        except Exception:  # noqa: BLE001 - best effort
            _LOGGER.debug("Could not read history to recover nights", exc_info=True)
            return

        predicted_history = sorted(
            (s.last_changed, v)
            for s in predictions
            if (v := dt_util.parse_datetime(s.state)) is not None
        )
        presence_history = sorted((s.last_changed, s.state) for s in presence)

        # Nights from here on are captured live, so this never needs repeating for them.
        self._recovered_until = dt_util.utcnow()
        changed = 0
        updated: list[Episode] = []
        for e in self.episodes:
            new = e
            if e in recent and e.predicted is None:
                before = [v for when, v in predicted_history if when <= e.onset]
                if before and abs(before[-1] - e.onset) <= MATCH_WINDOW:
                    new = replace(new, predicted=before[-1])
            if e in recent and e.left_home is None:
                left = first_departure(e.wake, presence_history)
                if left is not None:
                    new = replace(new, left_home=left)
            changed += new is not e
            updated.append(new)
        if not changed:
            self._save()
            return
        self.episodes = updated
        _LOGGER.info("%s: filled in %d recent nights from history", self.config_entry.title, changed)
        self._save()
        self.async_update_listeners()
        await self.async_request_refresh()

    async def _async_redescribe_nights(self) -> None:
        """Re-read the calendars for stored nights and describe them against real shifts.

        Early versions kept fewer days of shifts than they backfilled nights, so the oldest
        nights were stored as if no shift was nearby.
        """
        now = dt_util.utcnow()
        oldest = min(e.onset for e in self.episodes)
        start = max(oldest - timedelta(days=2), now - SHIFT_RETENTION)
        try:
            await self._async_fetch_shifts(start, now + timedelta(days=3))
        except HomeAssistantError as err:
            _LOGGER.warning("Could not re-read calendars to re-check past nights: %s", err)
            return
        described = []
        for e in self.episodes:
            if e.onset >= start:
                prev, nxt = neighbours(e.onset, self.shifts)
                e = replace(
                    e,
                    prev_end=prev.end if prev else None,
                    next_start=nxt.start if nxt else None,
                )
            described.append(e)
        self.episodes = described
        self._history_version = HISTORY_VERSION
        self._last_fetch = now
        _LOGGER.info("%s: re-checked past nights against the calendar", self.config_entry.title)
        self._save()
        await self.async_request_refresh()

    def _read_entity_history(self, entity_id: str, start: datetime, end: datetime) -> list[State]:
        from homeassistant.components.recorder import history  # noqa: PLC0415

        return history.state_changes_during_period(
            self.hass, start, end, entity_id=entity_id, no_attributes=True
        ).get(entity_id, [])

    # --- learning control ----------------------------------------------

    @callback
    def async_set_learning(self, enabled: bool) -> None:
        self.learning_enabled = enabled
        self._save()
        self.async_update_listeners()

    @callback
    def async_forget_night(self, onset: datetime | None = None) -> bool:
        """Forget the last night, or the night that began at onset."""
        if not self.episodes:
            return False
        if onset is None:
            target = self.episodes[-1]
        else:
            matches = [e for e in self.episodes if abs(e.onset - onset) < timedelta(minutes=1)]
            if not matches:
                return False
            target = matches[0]
        self.episodes = [e for e in self.episodes if e is not target]
        _LOGGER.info("%s: forgot the night from %s", self.config_entry.title, target.onset)
        self._save()
        self.async_update_listeners()
        self.hass.async_create_task(self.async_request_refresh())
        return True

    def _async_notify_recorded(self, episode: Episode) -> None:
        service = self._conf(CONF_NOTIFY)
        if not service:
            return
        tz = dt_util.get_default_time_zone()
        onset = episode.onset.astimezone(tz)
        wake = episode.wake.astimezone(tz)
        hours = (episode.wake - episode.onset).total_seconds() / 3600
        entry_id = self.config_entry.entry_id
        forget = f"{NOTIFY_ACTION_PREFIX}_{entry_id}_{int(episode.onset.timestamp())}"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "notify",
                str(service).removeprefix("notify."),
                {
                    "title": self.config_entry.title,
                    "message": (
                        f"Recorded a night: {onset:%H:%M} to {wake:%H:%M} ({hours:.1f} h). "
                        "Tap Forget if it was not a normal night."
                    ),
                    "data": {
                        "tag": f"{NOTIFY_ACTION_PREFIX}_{entry_id}",
                        "actions": [{"action": forget, "title": "Forget"}],
                    },
                },
            )
        )

    @callback
    def _async_on_notification_action(self, event: Event) -> None:
        action = str(event.data.get("action", ""))
        prefix = f"{NOTIFY_ACTION_PREFIX}_{self.config_entry.entry_id}_"
        if not action.startswith(prefix):
            return
        try:
            onset = dt_util.utc_from_timestamp(int(action.removeprefix(prefix)))
        except ValueError:
            return
        self.async_forget_night(onset)

    # --- calendars -----------------------------------------------------

    @callback
    def _async_on_calendar(self, event: Event[EventStateChangedData]) -> None:
        # The entity changes when a shift starts or ends, or its upcoming shift is edited.
        self._calendars_changed = True
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_fetch_shifts(self, start: datetime, end: datetime) -> None:
        response = await self.hass.services.async_call(
            "calendar",
            "get_events",
            {ATTR_ENTITY_ID: self.calendars, "start_date_time": start, "end_date_time": end},
            blocking=True,
            return_response=True,
        )
        fresh: set[Shift] = set()
        rules = self.calendar_rules
        for calendar_id, calendar in (response or {}).items():
            rule = rules.get(calendar_id, {"mode": MODE_WORK})
            for event in calendar.get("events", []):
                # All-day entries (holidays, notes) are not shifts.
                if "T" not in str(event.get("start")):
                    continue
                kind = classify(
                    event.get("summary"), event.get("description"), rule, self.first_name
                )
                if kind is None:
                    continue
                s = dt_util.parse_datetime(event["start"])
                e = dt_util.parse_datetime(event["end"])
                if s and e and e > s:
                    fresh.add(Shift(dt_util.as_utc(s), dt_util.as_utc(e), kind))
        # The calendar is authoritative inside the window it was asked about, so edited
        # and cancelled shifts disappear here. Older shifts are kept to describe past nights.
        keep = [s for s in self.shifts if s.end <= start or s.start >= end]
        cutoff = dt_util.utcnow() - SHIFT_RETENTION
        self.shifts = sorted(
            (s for s in {*keep, *fresh} if s.end >= cutoff), key=lambda s: s.start
        )

    # --- prediction ----------------------------------------------------

    async def _async_update_data(self) -> Prediction:
        now = dt_util.utcnow()
        defer, self._defer_fetch = self._defer_fetch, False
        if (
            self._calendars_changed
            or self._last_fetch is None
            or now - self._last_fetch >= CALENDAR_MAX_AGE
        ) and not defer and self._calendars_ready(now):
            try:
                await self._async_fetch_shifts(now - timedelta(days=2), now + timedelta(days=3))
                self._last_fetch = now
                self._calendars_changed = False
                self._save()
            except HomeAssistantError as err:
                if not self.shifts:
                    raise UpdateFailed(f"Could not read calendars: {err}") from err
                _LOGGER.warning("Could not read calendars, using cached shifts: %s", err)
                self._retry_fetch_at = now + CALENDAR_RETRY

        prediction = await self._async_predict(now)
        self._schedule_next(prediction, now)
        return prediction

    def _calendars_ready(self, now: datetime) -> bool:
        """At startup calendars can load after this integration.

        Their entities appearing fires a state change, which triggers a read, so there is
        nothing to do until then. One still missing after a grace period was probably
        removed or renamed, and gets a repair notice instead of failing silently.
        """
        missing = [c for c in self.calendars if self.hass.states.get(c) is None]
        issue_id = f"missing_calendar_{self.config_entry.entry_id}"
        if not missing:
            if self._missing_since is not None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self._missing_since = None
            return True
        self._missing_since = self._missing_since or now
        if now - self._missing_since >= MISSING_CALENDAR_GRACE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_calendar",
                translation_placeholders={
                    "name": self.config_entry.title,
                    "calendars": ", ".join(missing),
                },
            )
        else:
            self._retry_fetch_at = self._missing_since + MISSING_CALENDAR_GRACE
        return False

    async def _async_predict(self, now: datetime) -> Prediction:
        if self.committed and now < self.committed.wake:
            return self.committed
        self.committed = None

        p = self.params
        start = now
        if self.sleep_attempt and self.detector.onset:
            start = max(start, self.detector.onset + p.target_sleep + timedelta(hours=2))
        elif self.last_wake:
            start = max(start, self.last_wake + timedelta(hours=2))

        prediction = await self.hass.async_add_executor_job(
            predict,
            now,
            start,
            tuple(self.shifts),
            tuple(self.episodes),
            p,
            dt_util.get_default_time_zone(),
        )
        if not self.sleep_attempt and prediction.bedtime - now <= p.wind_down:
            self.committed = prediction
            self._save()
        return prediction

    def _schedule_next(self, prediction: Prediction, now: datetime) -> None:
        if self._unsub_next:
            self._unsub_next()
        moments = [
            prediction.bedtime - ATTEMPT_WINDOW,
            prediction.bedtime - self.params.wind_down,
            prediction.bedtime,
            prediction.wake,
            (self._last_fetch or now) + CALENDAR_MAX_AGE,
        ]
        if self._retry_fetch_at:
            moments.append(self._retry_fetch_at)
            self._retry_fetch_at = None
        due = min(m for m in moments if m > now) if any(m > now for m in moments) else now + CALENDAR_MAX_AGE
        self._unsub_next = async_track_point_in_utc_time(
            self.hass, self._async_on_due, due + timedelta(seconds=1)
        )

    @callback
    def _async_on_due(self, _now: datetime) -> None:
        self._unsub_next = None
        self.hass.async_create_task(self.async_request_refresh())

    # --- what the entities show ----------------------------------------

    @property
    def sleep_attempt(self) -> bool:
        """Settled in bed in a way that means going to sleep.

        A tracker reporting sleep always counts. Time in bed counts within ATTEMPT_WINDOW of
        the predicted bedtime, or once it has lasted a full sleep (a daytime sleep after a
        night shift, say). Reading in bed in the afternoon does not.
        """
        if not self.detector.asleep or self.detector.onset is None:
            return False
        now = dt_util.utcnow()
        if self.detector.tracked or now - self.detector.onset >= self.params.min_sleep:
            return True
        guess = self.committed or self.data
        return guess is not None and now >= guess.bedtime - ATTEMPT_WINDOW

    @property
    def expected_asleep(self) -> bool:
        """Home, and in the predicted sleep window or already going to sleep."""
        if not self.is_home:
            return False
        if self.sleep_attempt:
            return True
        if self.data is None:
            return False
        return self.data.bedtime <= dt_util.utcnow() < self.data.wake

    @property
    def winding_down(self) -> bool:
        if self.sleep_attempt or self.data is None:
            return False
        now = dt_util.utcnow()
        return self.data.bedtime - self.params.wind_down <= now < self.data.bedtime

    # --- one-off head start from the recorder --------------------------

    async def _async_backfill(self) -> None:
        now = dt_util.utcnow()
        start = now - timedelta(days=BACKFILL_DAYS)
        try:
            await self._async_fetch_shifts(start - timedelta(days=2), now + timedelta(days=3))
            self._last_fetch = now
            self._calendars_changed = False
        except HomeAssistantError as err:
            _LOGGER.warning("Backfill could not read calendars: %s", err)

        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415

            history = await get_instance(self.hass).async_add_executor_job(
                self._read_history, start, now
            )
        except Exception:  # noqa: BLE001 - best effort; learning simply starts from today
            _LOGGER.warning("Backfill could not read the recorder", exc_info=True)
            self._announce_backfill()
            return

        events = sorted(
            (state.last_changed, entity_id, state.state)
            for entity_id, states in history.items()
            for state in states
        )
        p = self.params
        detector = SleepDetector()
        current: dict[str, str] = {}
        on: bool | None = None
        since = start
        sure = False
        for when, entity_id, state in events:
            if on is not None and (done := detector.step(when, on, since, sure, p)):
                self._record(*done)
            current[entity_id] = state
            new_on, sure, known = self._reading(current.get)
            if known and new_on != on:
                on, since = new_on, when
        if on is not None and (done := detector.step(now, on, since, sure, p)):
            self._record(*done)
        self.episodes.sort(key=lambda e: e.onset)
        # Live detection has been running meanwhile; only adopt the replayed state if idle.
        if not self.detector.asleep:
            self.detector = detector
        self._announce_backfill()

    def _announce_backfill(self) -> None:
        count = len(self.episodes)
        name = self.config_entry.title
        if count:
            found = f"Found **{count} nights** of sleep for {name} in recent history and learned from them."
        else:
            found = f"No past nights were found for {name}, so learning starts tonight."
        persistent_notification.async_create(
            self.hass,
            f"{found}\n\nUntil more nights are learned, predictions follow the work schedule "
            "and the starting habits. Watch **Prediction confidence** rise as it learns.",
            title="Predictive Sleep is set up",
            notification_id=f"{DOMAIN}_{self.config_entry.entry_id}_setup",
        )

    def _read_history(self, start: datetime, end: datetime) -> dict[str, list[State]]:
        from homeassistant.components.recorder import history  # noqa: PLC0415

        out: dict[str, list[State]] = {}
        for entity_id in (*self.signals, self.person):
            out[entity_id] = history.state_changes_during_period(
                self.hass,
                start,
                end,
                entity_id=entity_id,
                no_attributes=True,
                include_start_time_state=True,
            ).get(entity_id, [])
        return out

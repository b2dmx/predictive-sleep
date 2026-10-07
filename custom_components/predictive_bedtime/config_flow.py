"""Config and options flows.

Setup asks only what is needed to start: who, which calendars and what each holds, how to
tell they're in bed, and a starting point for their habits (which are learned from then
on). Everything optional lives under Configure, split into short pages.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TimeSelector,
)
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ASLEEP,
    CONF_ASLEEP_STATES,
    CONF_CALENDAR_RULES,
    CONF_CALENDARS,
    CONF_FREE_BEDTIME,
    CONF_IN_BED,
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
    CONF_WAKE_GAP,
    CONF_WIND_DOWN,
    DEFAULT_OPTIONS,
    DEFAULT_PAUSE_STATES,
    DOMAIN,
    HOUSEHOLD_TITLE,
    KIND_HOUSEHOLD,
    TITLE_SUFFIX,
)
from .model import DEFAULT_ASLEEP_VALUES, DEFAULT_WAKE_WORDS, MODE_MIXED, MODE_WORK, mentions

# --- selectors -------------------------------------------------------------

PERSON = EntitySelector(EntitySelectorConfig(domain="person"))
CALENDARS = EntitySelector(EntitySelectorConfig(domain="calendar", multiple=True))
IN_BED = EntitySelector(
    EntitySelectorConfig(domain=["binary_sensor", "input_boolean", "sensor"], multiple=True)
)
TRACKERS = EntitySelector(
    EntitySelectorConfig(domain=["binary_sensor", "input_boolean", "sensor"], multiple=True)
)
ASLEEP_STATES = SelectSelector(
    SelectSelectorConfig(options=list(DEFAULT_ASLEEP_VALUES), multiple=True, custom_value=True)
)
PAUSE_ENTITIES = EntitySelector(EntitySelectorConfig(multiple=True))
PAUSE_STATES = SelectSelector(
    SelectSelectorConfig(options=DEFAULT_PAUSE_STATES, multiple=True, custom_value=True)
)
WORDS_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=list(DEFAULT_WAKE_WORDS), multiple=True, custom_value=True)
)


def _number(low: float, high: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=low, max=high, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX
        )
    )


# --- what each calendar holds ------------------------------------------------

# One choice per calendar. "shared" is a mixed calendar that only counts events
# mentioning the person (Home Assistant can't see who created an event).
CONTENT = "content"
CONTENT_WORK = "work"
CONTENT_MIXED = "mixed"
CONTENT_SHARED = "shared"
CONTENT_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[CONTENT_WORK, CONTENT_MIXED, CONTENT_SHARED], translation_key="calendar_content"
    )
)
WORDS = "words"
# Calendar names that suggest several people's events share one calendar.
SHARED_CALENDAR_WORDS = ("family", "home", "house", "household", "shared", "kids", "us")


def _calendar_name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return state.name if state and state.name else entity_id


def _guess_content(hass: HomeAssistant, entity_id: str, first_name: str) -> str:
    name = _calendar_name(hass, entity_id)
    if mentions(name, ["work", "shift", "shifts", "job"]):
        return CONTENT_WORK
    if mentions(name, SHARED_CALENDAR_WORDS) and not mentions(name, [first_name]):
        return CONTENT_SHARED
    return CONTENT_MIXED


def _content_of(rule: dict[str, Any]) -> str:
    if rule.get("mode", MODE_WORK) == MODE_WORK:
        return CONTENT_WORK
    return CONTENT_SHARED if rule.get("require_name") else CONTENT_MIXED


def _rule_for(content: str, words: list[str] | None = None) -> dict[str, Any]:
    if content == CONTENT_WORK:
        return {"mode": MODE_WORK}
    cleaned = [w.strip() for w in (words or DEFAULT_WAKE_WORDS) if w.strip()]
    return {
        "mode": MODE_MIXED,
        "words": cleaned or list(DEFAULT_WAKE_WORDS),
        "require_name": content == CONTENT_SHARED,
    }


class _CalendarRulesMixin:
    """Asks what each chosen calendar holds, one calendar at a time.

    Setup asks only the one question; Configure also shows the wake-up words.
    """

    hass: HomeAssistant
    _queue: list[str]
    _rules: dict[str, dict[str, Any]]
    _existing_rules: dict[str, dict[str, Any]]
    _with_words: bool = False

    def _first_name(self) -> str:
        raise NotImplementedError

    async def _async_rules_done(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_calendar(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self._queue[0]
        existing = self._existing_rules.get(current)
        if user_input is not None:
            words = user_input.get(WORDS) if self._with_words else (existing or {}).get("words")
            self._rules[current] = _rule_for(user_input[CONTENT], words)
            self._queue.pop(0)
            if self._queue:
                return await self.async_step_calendar()
            return await self._async_rules_done()

        schema: dict[Any, Any] = {vol.Required(CONTENT): CONTENT_SELECTOR}
        suggested: dict[str, Any] = {
            CONTENT: _content_of(existing)
            if existing
            else _guess_content(self.hass, current, self._first_name())
        }
        if self._with_words:
            schema[vol.Optional(WORDS)] = WORDS_SELECTOR
            suggested[WORDS] = (existing or {}).get("words") or list(DEFAULT_WAKE_WORDS)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="calendar",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                vol.Schema(schema), suggested
            ),
            description_placeholders={
                "calendar": _calendar_name(self.hass, current),
                "name": self._first_name(),
            },
        )


# --- setup -------------------------------------------------------------------

CALENDARS_SCHEMA = vol.Schema({vol.Required(CONF_CALENDARS): CALENDARS})

BED_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_IN_BED, default=[]): IN_BED,
        vol.Optional(CONF_ASLEEP, default=[]): TRACKERS,
    }
)

START_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_FREE_BEDTIME): TimeSelector(),
        vol.Required(CONF_TARGET_SLEEP): _number(4, 12, 0.25, "h"),
        vol.Required(CONF_PREP): _number(0, 240, 5, "min"),
    }
)


def _check_signals(user_input: dict[str, Any]) -> dict[str, str]:
    if not user_input.get(CONF_IN_BED) and not user_input.get(CONF_ASLEEP):
        return {"base": "no_signals"}
    return {}


class PredictiveBedtimeConfigFlow(_CalendarRulesMixin, ConfigFlow, domain=DOMAIN):
    VERSION = 6

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._queue = []
        self._rules = {}
        self._existing_rules = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        integration = await async_get_integration(self.hass, DOMAIN)
        return self.async_show_menu(
            step_id="user",
            menu_options=["person", KIND_HOUSEHOLD],
            description_placeholders={"version": str(integration.version or "")},
        )

    async def async_step_household(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(KIND_HOUSEHOLD)
        self._abort_if_unique_id_configured(error="household_exists")
        if user_input is not None:
            return self.async_create_entry(title=HOUSEHOLD_TITLE, data={"kind": KIND_HOUSEHOLD})
        return self.async_show_form(step_id=KIND_HOUSEHOLD, data_schema=vol.Schema({}))

    async def async_step_person(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_PERSON])
            self._abort_if_unique_id_configured()
            self._data.update(user_input)
            return await self.async_step_sources()

        return self.async_show_form(
            step_id="person", data_schema=vol.Schema({vol.Required(CONF_PERSON): PERSON})
        )

    async def async_step_sources(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_CALENDARS]:
                errors[CONF_CALENDARS] = "no_calendars"
            else:
                self._data[CONF_CALENDARS] = user_input[CONF_CALENDARS]
                self._queue = list(user_input[CONF_CALENDARS])
                return await self.async_step_calendar()

        return self.async_show_form(
            step_id="sources",
            data_schema=self.add_suggested_values_to_schema(
                CALENDARS_SCHEMA, user_input or {CONF_CALENDARS: self._suggest_calendars()}
            ),
            errors=errors,
            description_placeholders={"name": self._first_name()},
        )

    async def _async_rules_done(self) -> ConfigFlowResult:
        self._data[CONF_CALENDAR_RULES] = self._rules
        return await self.async_step_signals()

    async def async_step_signals(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check_signals(user_input)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_habits()

        return self.async_show_form(
            step_id="signals",
            data_schema=self.add_suggested_values_to_schema(BED_SCHEMA, user_input or {}),
            errors=errors,
            description_placeholders={"name": self._first_name()},
        )

    async def async_step_habits(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=f"{self._first_name()} {TITLE_SUFFIX}",
                data={CONF_PERSON: self._data[CONF_PERSON]},
                options={
                    **DEFAULT_OPTIONS,
                    CONF_ASLEEP_STATES: list(DEFAULT_ASLEEP_VALUES),
                    CONF_PAUSE_ENTITIES: [],
                    CONF_PAUSE_STATES: DEFAULT_PAUSE_STATES,
                    CONF_CALENDARS: self._data[CONF_CALENDARS],
                    CONF_CALENDAR_RULES: self._data[CONF_CALENDAR_RULES],
                    CONF_IN_BED: self._data.get(CONF_IN_BED, []),
                    CONF_ASLEEP: self._data.get(CONF_ASLEEP, []),
                    **user_input,
                },
            )

        return self.async_show_form(
            step_id="habits",
            data_schema=self.add_suggested_values_to_schema(START_SCHEMA, DEFAULT_OPTIONS),
            description_placeholders={"name": self._first_name()},
        )

    def _first_name(self) -> str:
        person = self.hass.states.get(self._data.get(CONF_PERSON, ""))
        return person.name.split()[0] if person and person.name else "this person"

    def _suggest_calendars(self) -> list[str]:
        """Calendars whose name mentions the person, e.g. "Work (Sam)"."""
        name = self._first_name().lower()
        return [
            state.entity_id
            for state in self.hass.states.async_all("calendar")
            if name in (state.name or "").lower()
        ]

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        return config_entry.data.get("kind") != KIND_HOUSEHOLD

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return PredictiveBedtimeOptionsFlow()


# --- Configure ------------------------------------------------------------------


def _notify_services(hass: HomeAssistant) -> list[str]:
    """Phones and other targets that can show a notification with buttons."""
    return sorted(
        f"notify.{name}"
        for name in hass.services.async_services_for_domain("notify")
        if name not in ("send_message", "persistent_notification")
    )


SIGNALS_OPTIONS_SCHEMA = BED_SCHEMA.extend({vol.Required(CONF_ASLEEP_STATES): ASLEEP_STATES})

TIMING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_WIND_DOWN): _number(0, 240, 5, "min"),
        vol.Required(CONF_SETTLE): _number(5, 90, 5, "min"),
        vol.Required(CONF_WAKE_GAP): _number(5, 120, 5, "min"),
        vol.Required(CONF_MIN_SLEEP): _number(1, 8, 0.5, "h"),
    }
)


def _learning_schema(notify_services: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_PAUSE_ENTITIES, default=[]): PAUSE_ENTITIES,
            vol.Optional(CONF_PAUSE_STATES, default=DEFAULT_PAUSE_STATES): PAUSE_STATES,
            vol.Optional(CONF_NOTIFY): SelectSelector(
                SelectSelectorConfig(options=notify_services, custom_value=True)
            ),
            vol.Required(CONF_SLEEP_DEBT, default=False): BooleanSelector(),
            vol.Required(CONF_RETENTION): _number(30, 1095, 1, "d"),
        }
    )


class PredictiveBedtimeOptionsFlow(_CalendarRulesMixin, OptionsFlow):
    _with_words = True

    def __init__(self) -> None:
        self._queue = []
        self._rules = {}
        self._existing_rules = {}
        self._calendars: list[str] = []

    def _first_name(self) -> str:
        return self.config_entry.title.split()[0] if self.config_entry.title else "this person"

    def _current(self) -> dict[str, Any]:
        return {
            **DEFAULT_OPTIONS,
            CONF_ASLEEP_STATES: list(DEFAULT_ASLEEP_VALUES),
            **self.config_entry.options,
        }

    def _save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["calendars", "signals", "learning", "timing"],
            description_placeholders={"name": self.config_entry.title},
        )

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_CALENDARS]:
                errors[CONF_CALENDARS] = "no_calendars"
            else:
                self._calendars = list(user_input[CONF_CALENDARS])
                self._queue = list(self._calendars)
                self._existing_rules = dict(self.config_entry.options.get(CONF_CALENDAR_RULES, {}))
                return await self.async_step_calendar()

        return self.async_show_form(
            step_id="calendars",
            data_schema=self.add_suggested_values_to_schema(
                CALENDARS_SCHEMA,
                user_input or {CONF_CALENDARS: self.config_entry.options.get(CONF_CALENDARS, [])},
            ),
            errors=errors,
            description_placeholders={"name": self._first_name()},
        )

    async def _async_rules_done(self) -> ConfigFlowResult:
        return self._save({CONF_CALENDARS: self._calendars, CONF_CALENDAR_RULES: self._rules})

    async def async_step_signals(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _check_signals(user_input)
            if not errors:
                return self._save(user_input)
        return self.async_show_form(
            step_id="signals",
            data_schema=self.add_suggested_values_to_schema(
                SIGNALS_OPTIONS_SCHEMA, user_input or self._current()
            ),
            errors=errors,
            description_placeholders={"name": self._first_name()},
        )

    async def async_step_learning(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            options = {**self.config_entry.options, **user_input}
            if not user_input.get(CONF_NOTIFY):
                options.pop(CONF_NOTIFY, None)
            return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="learning",
            data_schema=self.add_suggested_values_to_schema(
                _learning_schema(_notify_services(self.hass)), self._current()
            ),
            description_placeholders={"name": self._first_name()},
        )

    async def async_step_timing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        return self.async_show_form(
            step_id="timing",
            data_schema=self.add_suggested_values_to_schema(TIMING_SCHEMA, self._current()),
        )

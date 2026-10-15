"""Constants for Predictive Sleep."""
from datetime import timedelta

DOMAIN = "predictive_bedtime"
TITLE_SUFFIX = "Predictive Sleep"
# Entry kinds: one entry per person, plus an optional household entry.
KIND_HOUSEHOLD = "household"
HOUSEHOLD_TITLE = "Household Predictive Sleep"
# Sent whenever any person's prediction or sleep state changes.
SIGNAL_UPDATED = "predictive_bedtime_updated"

CONF_PERSON = "person"
CONF_CALENDARS = "calendars"
# Only events whose title contains one of these words count as shifts; empty = all.
CONF_KEYWORDS = "shift_keywords"
# Per calendar: {"mode": "work" | "mixed", "words": [...], "require_name": bool}.
CONF_CALENDAR_RULES = "calendar_rules"
# Replaced by the two signal lists in config version 2; kept for migration.
CONF_BED_SENSOR = "bed_sensor"
CONF_IN_BED = "in_bed_signals"
CONF_ASLEEP = "asleep_signals"
CONF_ASLEEP_STATES = "asleep_states"

# Learning control and feedback.
CONF_PAUSE_ENTITIES = "pause_entities"
CONF_PAUSE_STATES = "pause_states"
CONF_NOTIFY = "notify_service"
CONF_SLEEP_DEBT = "use_sleep_debt"
DEFAULT_PAUSE_STATES = ["on", "Guest", "Vacation", "Away"]
NOTIFY_ACTION_PREFIX = "PREDICTIVE_BEDTIME_FORGET"

CONF_TARGET_SLEEP = "target_sleep_hours"
CONF_PREP = "prep_minutes"
CONF_UNWIND = "unwind_minutes"
CONF_WIND_DOWN = "wind_down_minutes"
CONF_FREE_BEDTIME = "free_bedtime"
CONF_SETTLE = "settle_minutes"
CONF_WAKE_GAP = "wake_gap_minutes"
CONF_MIN_SLEEP = "min_sleep_hours"
CONF_RETENTION = "learning_window_days"

DEFAULT_OPTIONS = {
    CONF_TARGET_SLEEP: 7.5,
    CONF_PREP: 75,
    CONF_UNWIND: 90,
    CONF_WIND_DOWN: 60,
    CONF_FREE_BEDTIME: "23:30:00",
    CONF_SETTLE: 20,
    CONF_WAKE_GAP: 30,
    CONF_MIN_SLEEP: 3,
    CONF_RETENTION: 365,
    CONF_SLEEP_DEBT: False,
}

STORAGE_VERSION = 1
# Recent nights count more: a night this fraction of the learning window old counts half.
HALF_LIFE_FRACTION = 1 / 3
# Shifts are only needed to describe nights as they are recorded.
SHIFT_RETENTION = timedelta(days=40)
# The recorder usually keeps 10 days; this is a one-off head start, not the learning window.
BACKFILL_DAYS = 30
# Calendars are re-read at least this often, and immediately whenever a calendar entity changes.
CALENDAR_MAX_AGE = timedelta(hours=24)
# After a failed calendar read.
CALENDAR_RETRY = timedelta(minutes=10)

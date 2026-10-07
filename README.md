# Predictive Sleep

A Home Assistant integration that **learns when each person in the home goes to sleep** and predicts the next bedtime, so the house can get ready for sleep when sleep is actually expected, instead of on a fixed schedule.

It is built for irregular and rotating work schedules. It reads each person's work calendars, watches whatever sleep signals they have (a bed sensor, an mmWave sensor, a sleep tracker), and learns how their sleep relates to their shifts. When shifts change, predictions change with them.

## How it works

- **Learns from real nights.** Any stretch of 3+ hours in bed (while home) or asleep according to a tracker is recorded as a night's sleep. Shorter stretches are ignored.
- **Uses every signal you have.** Combine in-bed signals and sleep trackers; when one drops out, the others fill the gap. Each night records whether a tracker confirmed it.
- **Thinks in shifts, not weekdays.** Each night is described by hours until the next shift, hours since the last one ended, and time of day. A new rotation, a swapped shift or a schedule that changes every few months is handled by comparing it with similar situations, including ones never seen exactly before.
- **Starts sensible, then adapts.** Until enough nights are learned, predictions follow the schedule: a full night before early shifts, an unwinding buffer after late ones, and your usual bedtime on free days. As nights accumulate, actual behaviour takes over. **Prediction confidence** shows how far along it is.
- **Learns how long you sleep, too.** Next wake comes from how long similar nights lasted, capped by the next shift.
- **Learns your habits, not just your bedtimes.** Usual bedtime on free nights, sleep needed before a shift, unwind time after one, and get-ready time before one are all learned from real nights. The values asked for at setup are only where learning starts, so they're not in Configure.
- **Optional sleep debt.** When enabled, how much was slept in the previous 48 hours is also compared, since short nights tend to lead to earlier bedtimes.
- **You stay in control.** A Learning switch and "pause learning when" conditions (a home-mode selector, a guest or vacation toggle) keep untypical nights out. A Forget last sleep button, an action, and an optional phone notification with a Forget button handle the rest.
- **Bounded memory.** Nights older than the learning window (365 days by default) are discarded. Within the window, recent nights count more.
- **Nothing polls.** Calendars are re-read once a day, or immediately when one of that person's calendars changes. Sensors switch at the exact moments they are due.
- **Head start.** On first setup it learns from whatever bed history the recorder still holds.

## Entities (per person)

| Entity | What it is |
|---|---|
| Next bedtime | When this person is expected to go to bed next. Attributes include the schedule-only estimate and the shifts either side. |
| Next wake-up | When they are expected up: how long similar sleeps lasted, or earlier if a shift needs them up. |
| Prediction confidence | 0–100 %, how much the prediction rests on learned nights rather than the schedule alone. Diagnostic. |
| Last bedtime | When the most recent recorded sleep began, and whether a tracker confirmed it. |
| Last wake-up | When it ended. |
| Last sleep duration | Its length in hours, kept as long-term statistics for graphs. |
| Prediction error | Average minutes between predicted and actual bedtime over the last 14 sleeps. Lower is better. Diagnostic. |
| Learning (switch) | Turn off to stop learning by hand. Attributes show whether learning is active right now and what paused it. |
| Forget last sleep (button) | Removes the most recent sleep from learning. |
| Expected asleep | On while they are home and either in the predicted sleep window or going to sleep: settled in bed within 2 hours of the predicted bedtime, asleep per a tracker, or in bed long enough to be a real sleep. Reading in bed in the afternoon doesn't count. |
| Wind-down | On for a set time before the predicted bedtime. |

### Household sensors (optional)

Add the integration once more and choose **Add household sensors** for whole-home views across everyone set up. Handy for locks, heating and common-area lights, especially without a home-mode system of your own.

| Entity | What it is |
|---|---|
| Everyone expected asleep | On when everyone who is home is expected asleep. Attributes list who is and who isn't. |
| Anyone winding down | On when anyone who is home is winding down. |
| Earliest bedtime / Latest bedtime | Earliest and latest predicted bedtimes, with who they belong to. |

### Example

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.sam_predictive_sleep_wind_down
    to: "on"
actions:
  - action: light.turn_on
    target:
      area_id: bedroom
    data:
      brightness_pct: 30
      color_temp_kelvin: 2200
```

## Blueprints

Ready-made automations. Each asks you to pick Predictive Sleep sensors, so they work for any person or the household.

| Blueprint | What it does | |
|---|---|---|
| Wind-down lighting | Dims and warms lights at wind-down, optionally off once expected asleep | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fpredictive-sleep%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fpredictive_sleep%2Fwind_down_lights.yaml) |
| Pre-cool or pre-heat the bedroom | Sets the thermostat a chosen lead time before the predicted bedtime | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fpredictive-sleep%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fpredictive_sleep%2Fprecondition_bedroom.yaml) |
| Bedtime and wake-up routine | Your own actions at expected sleep and wake: lock up, blinds, do-not-disturb, alarm | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fpredictive-sleep%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fpredictive_sleep%2Fsleep_routine.yaml) |
| Bedtime reminder before early shifts | A phone reminder before bedtime, only when the next shift is soon after it | [![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fb2dmx%2Fpredictive-sleep%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fpredictive_sleep%2Fbedtime_reminder.yaml) |

## Dashboard card

The [`dashboards`](dashboards) folder has the same card three ways: [built-in cards only](dashboards/default.yaml), [Mushroom](dashboards/mushroom.yaml) and [Bubble Card](dashboards/bubble.yaml). Add a Manual card, paste one in, and replace `sam` with the person's first name as it appears in their entity IDs. The cards show clock times; Home Assistant's own entity pages always show times relative ("in 4 hours").

## Requirements

- One or more **calendars**. Each is read its own way, chosen during setup:
  - **Only work shifts:** every timed event is a shift (a dedicated work calendar).
  - **Work and other events:** only events whose title or description mentions a wake-up word count: work, shift, school, class, appointment, doctor, dentist, therapy, meeting, interview, flight, exam and so on (editable). Birthdays, holidays and anything else are ignored. Work events shape the sleep pattern; the rest are appointments that only set how early the person must be up.
  - **Only events that mention the person:** for calendars shared with others, e.g. "Sam dentist" counts for Sam but not for Alex. Home Assistant can't see who created an event, so the name stands in for it.

  Matching is whole-word and ignores case, so `work` matches "Work (Sam)" but not "Workout". All-day events are always ignored.
- At least one **sleep signal** per person, and a **person** entity:

| Signal | Examples | Treated as |
|---|---|---|
| In-bed | Bed pressure sensor for their side, mmWave presence aimed at the bed, occupancy sensor | In bed once settled for 20 minutes, while home |
| Sleep tracker | Sleep as Android, Withings, Fitbit, Apple Health sleep data (e.g. via Health Auto Export) | Asleep immediately; text states like `asleep` or `deep` are configurable |

Avoid "sleep focus" or bedtime-schedule entities: they follow preset times rather than actual sleep, which is what this integration replaces.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/b2dmx/predictive-sleep`, category **Integration**.
2. Download **Predictive Sleep**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Predictive Sleep**, once per person.

### Manual

Copy `custom_components/predictive_bedtime` into your `config/custom_components` folder and restart.

## Actions

`predictive_bedtime.forget_last_night` removes the most recent sleep for one person (pick their entry).

## Settings

Setup asks for the person, their calendars and how to read each, their sleep signals, learning preferences and a few starting habits. Everything can be changed later under **Configure**, which offers **Calendars** and **Sleep signals, learning and habits**. Everything can be changed later under **Configure**:

| Setting | Default |
|---|---|
| Usual bedtime with no shift nearby | 23:30 |
| Sleep needed before a shift | 7.5 h |
| Wake-up to shift start (getting ready + commute; learned) | 75 min |
| Wind-down before bedtime | 60 min |
| Shortest time from shift end to bed | 90 min |
| Time in bed before it counts as a sleep attempt | 20 min |
| Time out of bed before it counts as awake | 30 min |
| Shortest stretch that counts as a night | 3 h |
| Learning window | 365 days |
| Pause learning when | (none) |
| Pause states | on, Guest, Vacation, Away |
| Notification after each night | (off) |
| Account for sleep debt | off |

## Privacy

Everything runs locally. Learned nights are stored in Home Assistant's `.storage` folder and never leave your system.

## License

MIT

---

<p align="center">Predictive Sleep by Goobis<br>AI was used to help develop this integration.</p>

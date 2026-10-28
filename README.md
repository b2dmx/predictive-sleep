<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/predictive_bedtime/brand/dark_icon@2x.png">
    <img src="custom_components/predictive_bedtime/brand/icon@2x.png" width="140" alt="Predictive Sleep logo">
  </picture>
</p>

<h1 align="center">Predictive Sleep</h1>

<p align="center"><b>Learns when each person goes to sleep, and predicts the next bedtime.</b></p>

<p align="center">
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5?logo=homeassistantcommunitystore&logoColor=white" alt="HACS Custom"></a>
  <a href="https://ko-fi.com/goobis2dmx"><img src="https://img.shields.io/badge/Ko--fi-Support-FF5E5B?logo=kofi&logoColor=white" alt="Support on Ko-fi"></a>
</p>

A Home Assistant integration. The house gets ready for sleep when sleep is actually expected, not on a fixed schedule.

## What it does

- **Learns from real nights** using any sleep signal: bed sensor, mmWave, sleep tracker, or a switch.
- **Thinks in shifts, not weekdays.** Reads work calendars, so rotating schedules just work.
- **Predicts** next bedtime, next wake-up, and a wind-down window, per person and for the household.
- **Runs locally.** Nothing polls, nothing leaves your system.

## Installation

You need a **person** entity, ideally a sleep signal, and optionally a calendar.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=b2dmx&repository=predictive-sleep&category=integration)

1. Open in HACS (button above), download **Predictive Sleep**, restart.
2. **Settings → Devices & services → Add integration → Predictive Sleep**, once per person.

Manual: copy `custom_components/predictive_bedtime` into `config/custom_components` and restart.

## Onboarding

Setup takes a minute, all in the UI:

1. **Person.**
2. **Calendars** (optional): just shifts, work and appointments, or a shared family calendar.
3. **Sleep signals:** in-bed sensors and/or trackers.
4. **Starting points:** usual bedtime, sleep needed, get-ready time. These are learned from then on.

Add it once more and choose **Add household sensors** for whole-home views.

## Tips

- No sensor? Flip the **In bed for the night** switch at bedtime and on waking. It turns itself off after 12 hours.
- Use **Wind-down** to dim lights and **Expected asleep** to lock up. [Blueprints](blueprints/automation/predictive_sleep) are ready to import.
- Bad night? Use **Skip a night** or **Forget last sleep** to keep it out of learning.
- Add a guest or vacation toggle under **Configure → Learning** so odd nights are ignored.
- Avoid "sleep focus" entities: they follow preset times, not real sleep.
- Ready-made [dashboard cards](dashboards): built-in, Mushroom and Bubble Card.

## More

[Full reference →](docs/reference.md) (how it learns, entities, blueprints, calendars, settings)

---

<p align="center">
  <a href="LICENSE">MIT</a> · <a href="https://ko-fi.com/goobis2dmx">Support on Ko-fi</a><br><br>
  Predictive Sleep v0.14.2 by Goobis<br>
  AI was used to help develop this application
</p>

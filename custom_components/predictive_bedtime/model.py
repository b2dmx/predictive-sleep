"""Bedtime model. Pure Python with no Home Assistant imports, so it can be unit-tested alone.

Each recorded sleep is described by three features taken at the moment you fell asleep:

* hours until the next shift starts
* hours since the last shift ended
* local clock time
* optionally, hours slept in the 48 hours before (sleep debt)

To predict, every candidate time over the next 24 hours is described the same way and
scored by how closely it resembles past sleeps (a kernel density, weighted toward recent
nights). How long similar nights lasted gives the expected wake. A schedule-based guess is always added as a weak prior, so the first nights and
never-seen-before schedules still produce a sensible answer. The best-scoring time wins.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any

# Anything further away than this counts as "no shift nearby".
HORIZON_H = 30.0
# How far apart two situations can be (in hours) and still count as similar.
SIGMA_NEXT = 1.5
SIGMA_PREV = 2.5
SIGMA_CLOCK = 2.0
SIGMA_DEBT = 3.0
DEBT_WINDOW = timedelta(hours=48)
# Width of the schedule prior, in hours.
SIGMA_RULE = 1.0
# The prior counts as this many perfectly matching nights.
PRIOR_WEIGHT = 1.5
STEP = timedelta(minutes=10)
LOOKAHEAD = timedelta(hours=24)
# The search stays on the coming night: at most this long past its schedule bedtime, so a
# night with lots of similar history tomorrow cannot outscore a sparser one tonight.
NIGHT_SPAN = timedelta(hours=6)
MIN_WEIGHT = 0.02
MAX_SLEEP = timedelta(hours=16)

# What calendar events are.
KIND_WORK = "work"
KIND_APPOINTMENT = "appointment"
# A shared-calendar event that names nobody: whose it is gets worked out from who left home.
KIND_UNNAMED = "unnamed"
# What a shared calendar does with events that name nobody.
UNNAMED_GUESS = "guess"
UNNAMED_IGNORE = "ignore"
# How a calendar is read: every event is a shift, or only events with wake-up words.
MODE_WORK = "work"
MODE_MIXED = "mixed"
# Words that mark an event as work rather than an appointment.
WORK_WORDS = frozenset({"work", "shift", "on call", "on-call", "overtime"})
# Things that usually need someone up and out; birthdays and holidays are not among them.
DEFAULT_WAKE_WORDS = (
    "work",
    "shift",
    "school",
    "class",
    "appointment",
    "appt",
    "doctor",
    "dentist",
    "therapy",
    "clinic",
    "meeting",
    "interview",
    "flight",
    "exam",
    "court",
    "on call",
    "overtime",
)

# Where a recorded night came from.
SOURCE_TRACKER = "tracker"
SOURCE_IN_BED = "in_bed"

# States that mean an in-bed signal (bed pressure, mmWave, occupancy) sees someone.
IN_BED_VALUES = frozenset({"on", "occupied", "detected", "present", "home", "true"})
# States that mean a sleep tracker reports sleep. Editable per person.
DEFAULT_ASLEEP_VALUES = (
    "on",
    "asleep",
    "sleeping",
    "sleep",
    "light",
    "deep",
    "rem",
    "light_sleep",
    "deep_sleep",
    "rem_sleep",
    "sleep_tracking_started",
)
_UNUSABLE = frozenset({"unavailable", "unknown", ""})


@dataclass(frozen=True)
class Shift:
    """Anything on the calendar that needs the person up and out.

    Work shifts shape the whole sleep pattern (coming home, unwinding). Appointments only
    set how early the person needs to be up.
    """

    start: datetime
    end: datetime
    kind: str = "work"


@dataclass(frozen=True)
class Params:
    target_sleep: timedelta = timedelta(hours=7.5)
    prep: timedelta = timedelta(minutes=75)
    unwind: timedelta = timedelta(minutes=90)
    wind_down: timedelta = timedelta(minutes=60)
    free_bedtime: time = time(23, 30)
    settle: timedelta = timedelta(minutes=20)
    wake_gap: timedelta = timedelta(minutes=30)
    min_sleep: timedelta = timedelta(hours=3)
    half_life_days: float = 180.0
    use_sleep_debt: bool = False


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Episode:
    onset: datetime
    wake: datetime
    prev_end: datetime | None
    next_start: datetime | None
    source: str = SOURCE_IN_BED
    # What was predicted for this night, to measure accuracy.
    predicted: datetime | None = None
    # When they left home after waking, if soon after: marks a get-up-and-go morning.
    left_home: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "onset": _iso(self.onset),
            "wake": _iso(self.wake),
            "prev_end": _iso(self.prev_end),
            "next_start": _iso(self.next_start),
            "source": self.source,
            "predicted": _iso(self.predicted),
            "left_home": _iso(self.left_home),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Episode:
        return cls(
            onset=_parse(data["onset"]),
            wake=_parse(data["wake"]),
            prev_end=_parse(data.get("prev_end")),
            next_start=_parse(data.get("next_start")),
            source=data.get("source", SOURCE_IN_BED),
            predicted=_parse(data.get("predicted")),
            left_home=_parse(data.get("left_home")),
        )


@dataclass(frozen=True)
class Prediction:
    bedtime: datetime
    wake: datetime
    schedule_bedtime: datetime
    confidence: float
    prev_end: datetime | None
    next_start: datetime | None
    nights_used: int
    prep: timedelta = timedelta(minutes=75)
    # Learned baselines this prediction used (setup values until nights accumulate).
    usual_bedtime: float = 23.5
    target_sleep: timedelta = timedelta(hours=7.5)
    unwind: timedelta = timedelta(minutes=90)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bedtime": _iso(self.bedtime),
            "wake": _iso(self.wake),
            "schedule_bedtime": _iso(self.schedule_bedtime),
            "confidence": self.confidence,
            "prev_end": _iso(self.prev_end),
            "next_start": _iso(self.next_start),
            "nights_used": self.nights_used,
            "prep_minutes": self.prep.total_seconds() / 60,
            "usual_bedtime": self.usual_bedtime,
            "target_sleep_hours": _hours(self.target_sleep),
            "unwind_minutes": self.unwind.total_seconds() / 60,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Prediction:
        return cls(
            bedtime=_parse(data["bedtime"]),
            wake=_parse(data["wake"]),
            schedule_bedtime=_parse(data["schedule_bedtime"]),
            confidence=data["confidence"],
            prev_end=_parse(data.get("prev_end")),
            next_start=_parse(data.get("next_start")),
            nights_used=data.get("nights_used", 0),
            prep=timedelta(minutes=data.get("prep_minutes", 75)),
            usual_bedtime=data.get("usual_bedtime", 23.5),
            target_sleep=timedelta(hours=data.get("target_sleep_hours", 7.5)),
            unwind=timedelta(minutes=data.get("unwind_minutes", 90)),
        )


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _clock_gap(a: float, b: float) -> float:
    d = abs(a - b) % 24
    return min(d, 24 - d)


def mentions(text: str | None, words: Iterable[str]) -> list[str]:
    """Words found in text: whole-word, ignoring case ("work" matches "Sam work", not "Workout")."""
    return [
        w
        for w in (k.strip() for k in words if k)
        if w and re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text or "", re.IGNORECASE)
    ]


def is_shift(summary: str | None, keywords: Iterable[str]) -> bool:
    """With no keywords every event counts; otherwise one must be mentioned."""
    words = [k for k in keywords if k and k.strip()]
    return not words or bool(mentions(summary, words))


def classify(
    summary: str | None,
    description: str | None,
    rule: dict[str, Any],
    name: str,
    others: Iterable[str] = (),
) -> str | None:
    """What an event is for this person under a calendar's rule: work, appointment, or None.

    rule["mode"] is "work" (every timed event is a shift) or "mixed" (only events mentioning
    one of rule["words"] count, optionally only those that also mention the person's name).
    On a shared calendar set to guess, an event naming nobody in the household (others are
    the rest of it) is KIND_UNNAMED: who left home for it decides whose it is.
    """
    if rule.get("mode", MODE_WORK) == MODE_WORK:
        return KIND_WORK
    text = f"{summary or ''}\n{description or ''}"
    if rule.get("require_name") and not mentions(text, [name]):
        if rule.get("unnamed") == UNNAMED_GUESS and not mentions(text, others):
            return KIND_UNNAMED
        return None
    found = mentions(text, rule.get("words") or DEFAULT_WAKE_WORDS)
    if not found:
        return None
    return KIND_WORK if mentions(text, WORK_WORDS) else KIND_APPOINTMENT


def neighbours(t: datetime, shifts: Iterable[Shift]) -> tuple[Shift | None, Shift | None]:
    """The last work shift that ended by t, and the first commitment of any kind after t."""
    prev: Shift | None = None
    nxt: Shift | None = None
    for s in shifts:
        if s.kind == KIND_WORK and s.end <= t and (prev is None or s.end > prev.end):
            prev = s
        if s.start > t and (nxt is None or s.start < nxt.start):
            nxt = s
    return prev, nxt


def at_work(t: datetime, shifts: Iterable[Shift]) -> bool:
    return any(s.start <= t < s.end for s in shifts)


def features(
    t: datetime, prev_end: datetime | None, next_start: datetime | None, tz: tzinfo
) -> tuple[float, float, float]:
    until_next = min(_hours(next_start - t), HORIZON_H) if next_start else HORIZON_H
    since_prev = min(_hours(t - prev_end), HORIZON_H) if prev_end else HORIZON_H
    local = t.astimezone(tz)
    return until_next, since_prev, local.hour + local.minute / 60


def make_episode(
    onset: datetime,
    wake: datetime,
    shifts: Iterable[Shift],
    source: str = SOURCE_IN_BED,
    predicted: datetime | None = None,
) -> Episode:
    prev, nxt = neighbours(onset, shifts)
    return Episode(
        onset, wake, prev.end if prev else None, nxt.start if nxt else None, source, predicted
    )


def slept_before(t: datetime, episodes: Iterable[Episode]) -> float:
    """Hours slept in the DEBT_WINDOW before t."""
    window_start = t - DEBT_WINDOW
    total = timedelta()
    for e in episodes:
        overlap = min(e.wake, t) - max(e.onset, window_start)
        if overlap > timedelta():
            total += overlap
    return _hours(total)


def accuracy(episodes: Sequence[Episode], nights: int = 14) -> tuple[float, int] | None:
    """Mean absolute bedtime error in minutes over the most recent predicted nights."""
    errors = [
        abs(_hours(e.onset - e.predicted)) * 60 for e in episodes if e.predicted is not None
    ][-nights:]
    if not errors:
        return None
    return sum(errors) / len(errors), len(errors)


def schedule_bedtime(start: datetime, shifts: Sequence[Shift], p: Params, tz: tzinfo) -> datetime:
    """Bedtime from the schedule alone.

    Start from the usual free-day bedtime, push it past any shift plus unwind time, then
    pull it earlier if that would not leave a full night before the next shift.
    """
    ordered = sorted(shifts, key=lambda s: s.start)

    def past_shifts(t: datetime) -> datetime:
        for s in ordered:
            after = s.end + (p.unwind if s.kind == KIND_WORK else timedelta())
            if s.start <= t < after:
                t = after
        return t

    # Last night's bedtime still counts when a night shift pushed it into today.
    local = start.astimezone(tz)
    anchor = start
    for days in (-1, 0, 1):
        candidate = datetime.combine(local.date() + timedelta(days=days), p.free_bedtime, tzinfo=tz)
        candidate = past_shifts(candidate.astimezone(UTC))
        if candidate >= start - timedelta(hours=3):
            anchor = candidate
            break

    prev, nxt = neighbours(anchor, shifts)
    if nxt:
        latest = nxt.start - p.prep - p.target_sleep
        if latest < anchor:
            anchor = max(latest, prev.end + p.unwind) if prev else latest
    return anchor


# Getting up more than this long before a shift is a natural wake-up, not getting ready.
PREP_MAX = timedelta(hours=2, minutes=30)
# Leaving home within this long of waking marks a get-up-and-go morning.
DEPART_MAX = timedelta(minutes=90)


def is_get_ready_morning(e: Episode) -> bool:
    """Woke, got ready and left for a shift that started soon after.

    Lazy mornings before a late shift (up at 8 for a 2 pm start) fail on both counts: the
    shift is hours away and they don't leave until much later.
    """
    if e.next_start is None or e.left_home is None:
        return False
    return (
        timedelta() < e.next_start - e.wake <= PREP_MAX
        and timedelta() < e.left_home - e.wake <= DEPART_MAX
        and e.left_home <= e.next_start
    )


def first_departure(
    wake: datetime, changes: Sequence[tuple[datetime, str]], home: str = "home"
) -> datetime | None:
    """When presence first went from home to elsewhere after wake, within DEPART_MAX."""
    previous: str | None = None
    for when, state in changes:
        if state in ("unavailable", "unknown"):
            continue
        if when > wake + DEPART_MAX:
            break
        if when > wake and previous == home and state != home:
            return when
        previous = state
    return None


# Leaving home this long before an event (or a little after it starts) means going to it.
LEAVE_BEFORE = timedelta(minutes=90)
LEAVE_LATE = timedelta(minutes=15)


def left_for(
    start: datetime, changes: Sequence[tuple[datetime, str]], home: str = "home"
) -> bool | None:
    """Whether they left home for something starting at start.

    True: home LEAVE_BEFORE ahead of it, then gone by just after it began. False: home the
    whole time. None: already out (at work, say), which says nothing either way.
    """
    before = start - LEAVE_BEFORE
    state: str | None = None
    for when, value in changes:
        if value in ("unavailable", "unknown"):
            continue
        if when > before:
            break
        state = value
    if state != home:
        return None
    for when, value in changes:
        if value in ("unavailable", "unknown") or when <= before:
            continue
        if when > start + LEAVE_LATE:
            break
        if value != home:
            return True
    return False


def left_for_from_nights(start: datetime, episodes: Sequence[Episode]) -> bool | None:
    """left_for from stored nights, once presence history is gone: left soon after waking."""
    for e in episodes:
        if e.left_home is not None and start - LEAVE_BEFORE < e.left_home <= start + LEAVE_LATE:
            return True
    return None


def event_keys(summary: str | None, location: str | None) -> list[str]:
    """What identifies a recurring appointment: its title and where it is."""
    keys = []
    title = re.sub(r"\d+(?:st|nd|rd|th|am|pm)?\b", " ", (summary or "").lower())
    title = " ".join(re.sub(r"[^\w\s]", " ", title).split())
    if title:
        keys.append(f"title:{title}")
    place = " ".join((location or "").split("\n")[0].lower().split())
    if place:
        keys.append(f"place:{place}")
    return keys


def is_theirs(keys: Iterable[str], owners: dict[str, list[int]]) -> bool:
    """A guess for an appointment nobody has left for yet: theirs if they usually go to it."""
    mine = not_mine = 0
    for k in keys:
        yes, no = owners.get(k, (0, 0))
        mine, not_mine = mine + yes, not_mine + no
    return mine > not_mine


# How far apart two start times can be (hours) and still say much about each other.
SIGMA_START = 1.0


def learned_prep(
    now: datetime,
    episodes: Sequence[Episode],
    p: Params,
    tz: tzinfo | None = None,
    for_start: datetime | None = None,
) -> timedelta:
    """How long before a shift this person actually gets up.

    Taken only from get-up-and-go mornings (see is_get_ready_morning), weighted toward
    recent ones and, when for_start is given, toward mornings with a similar start time
    (people are often quicker for a 7:00 start than a 9:30 one). Pulled toward the
    configured value while there are few of them.
    """
    target = None
    if for_start is not None and tz is not None:
        local = for_start.astimezone(tz)
        target = local.hour + local.minute / 60
    total, weight_sum = 0.0, 0.0
    for e in episodes:
        if is_get_ready_morning(e):
            gap = e.next_start - e.wake
            w = 0.5 ** (_hours(now - e.onset) / 24 / p.half_life_days)
            if target is not None:
                start = e.next_start.astimezone(tz)
                gap_h = _clock_gap(start.hour + start.minute / 60, target)
                w *= math.exp(-0.5 * (gap_h / SIGMA_START) ** 2)
            total += w * _hours(gap)
            weight_sum += w
    hours = (total + PRIOR_WEIGHT * _hours(p.prep)) / (weight_sum + PRIOR_WEIGHT)
    return timedelta(hours=hours)


# A night counts as "free" when nothing needs the person up within this long of going to
# bed, and they did not just get home from a shift.
FREE_NEXT_MIN = timedelta(hours=12)
FREE_PREV_MIN = timedelta(hours=3)
# Going to bed within this long of a shift ending shows how long unwinding takes.
UNWIND_MAX = timedelta(hours=4)
# A free-night bedtime more than this many hours from the setup bedtime is a daytime sleep.
NIGHT_WINDOW_H = 6.0


@dataclass(frozen=True)
class Learned:
    """The person's own habits, learned from their nights.

    Each starts at the setup value and moves toward what the nights show, the more so the
    more nights there are; recent nights count more.
    """

    usual_bedtime: float  # local clock hours, e.g. 22.5
    target_sleep: timedelta
    unwind: timedelta
    prep: timedelta
    free_nights: int
    sleep_nights: int
    unwind_nights: int
    prep_mornings: int


def _recency(now: datetime, e: Episode, p: Params) -> float:
    return 0.5 ** (_hours(now - e.onset) / 24 / p.half_life_days)


def _blend(samples: list[tuple[float, float]], baseline: float) -> float:
    """Weighted mean of (value, weight) samples, pulled toward baseline while data is thin."""
    total = sum(v * w for v, w in samples) + PRIOR_WEIGHT * baseline
    return total / (sum(w for _, w in samples) + PRIOR_WEIGHT)


def is_free_night(e: Episode) -> bool:
    return (e.next_start is None or e.next_start - e.onset >= FREE_NEXT_MIN) and (
        e.prev_end is None or e.onset - e.prev_end >= FREE_PREV_MIN
    )


def learned_usual_bedtime(now: datetime, episodes: Sequence[Episode], p: Params, tz: tzinfo) -> tuple[float, int]:
    """Usual bedtime on free nights, as local clock hours (a circular mean, so 23:30 and
    00:30 average to midnight rather than noon)."""
    baseline = p.free_bedtime.hour + p.free_bedtime.minute / 60
    x = y = 0.0
    count = 0
    for e in episodes:
        if is_free_night(e):
            local = e.onset.astimezone(tz)
            clock = local.hour + local.minute / 60
            # A daytime sleep (a nap, or sleeping off a long day) is not a bedtime.
            if _clock_gap(clock, baseline) > NIGHT_WINDOW_H:
                continue
            angle = clock / 24 * 2 * math.pi
            w = _recency(now, e, p)
            x += w * math.cos(angle)
            y += w * math.sin(angle)
            count += 1
    base = (p.free_bedtime.hour + p.free_bedtime.minute / 60) / 24 * 2 * math.pi
    x += PRIOR_WEIGHT * math.cos(base)
    y += PRIOR_WEIGHT * math.sin(base)
    return (math.atan2(y, x) / (2 * math.pi) * 24) % 24, count


def learned_target_sleep(now: datetime, episodes: Sequence[Episode], p: Params) -> tuple[timedelta, int]:
    """How long they actually sleep before a shift that gets them up."""
    samples = [
        (_hours(e.wake - e.onset), _recency(now, e, p))
        for e in episodes
        if is_get_ready_morning(e)
    ]
    return timedelta(hours=_blend(samples, _hours(p.target_sleep))), len(samples)


def learned_unwind(now: datetime, episodes: Sequence[Episode], p: Params) -> tuple[timedelta, int]:
    """How soon after a shift they get to bed, from nights they went to bed soon after one."""
    samples = [
        (_hours(e.onset - e.prev_end), _recency(now, e, p))
        for e in episodes
        if e.prev_end is not None and timedelta() < e.onset - e.prev_end <= UNWIND_MAX
    ]
    return timedelta(hours=_blend(samples, _hours(p.unwind))), len(samples)


def learn(
    now: datetime,
    episodes: Sequence[Episode],
    p: Params,
    tz: tzinfo,
    for_start: datetime | None = None,
) -> Learned:
    usual, free = learned_usual_bedtime(now, episodes, p, tz)
    target, slept = learned_target_sleep(now, episodes, p)
    unwind, unwound = learned_unwind(now, episodes, p)
    return Learned(
        usual_bedtime=usual,
        target_sleep=target,
        unwind=unwind,
        prep=learned_prep(now, episodes, p, tz, for_start),
        free_nights=free,
        sleep_nights=slept,
        unwind_nights=unwound,
        prep_mornings=sum(is_get_ready_morning(e) for e in episodes),
    )


def predict(
    now: datetime,
    start: datetime,
    shifts: Sequence[Shift],
    episodes: Sequence[Episode],
    p: Params,
    tz: tzinfo,
) -> Prediction:
    """Predict the next sleep onset at or after start, and when it will end."""
    # The schedule-based estimate uses the person's learned habits, not the setup values:
    # those are only where learning starts. Get-ready time is for the next thing to be up
    # for, learned from similar start times.
    setup = p

    def with_habits(upcoming: Shift | None) -> tuple[Learned, Params]:
        habits = learn(now, episodes, setup, tz, upcoming.start if upcoming else None)
        minutes = round(habits.usual_bedtime * 60) % (24 * 60)
        return habits, replace(
            setup,
            prep=habits.prep,
            target_sleep=habits.target_sleep,
            unwind=habits.unwind,
            free_bedtime=time(minutes // 60, minutes % 60),
        )

    _, upcoming = neighbours(start, shifts)
    habits, p = with_habits(upcoming)
    rule = schedule_bedtime(start, shifts, p, tz)
    _, after_night = neighbours(rule, shifts)
    if after_night != upcoming:
        habits, p = with_habits(after_night)
        rule = schedule_bedtime(start, shifts, p, tz)

    # (until next shift, since last shift, clock, debt, hours slept, weight)
    learned: list[tuple[float, float, float, float, float, float]] = []
    for e in episodes:
        weight = 0.5 ** (_hours(now - e.onset) / 24 / p.half_life_days)
        if weight >= MIN_WEIGHT:
            debt = slept_before(e.onset, episodes) if p.use_sleep_debt else 0.0
            learned.append(
                (
                    *features(e.onset, e.prev_end, e.next_start, tz),
                    debt,
                    _hours(e.wake - e.onset),
                    weight,
                )
            )

    def similarity(t: datetime) -> list[float]:
        prev, nxt = neighbours(t, shifts)
        n, pv, c = features(t, prev.end if prev else None, nxt.start if nxt else None, tz)
        debt = slept_before(t, episodes) if p.use_sleep_debt else 0.0
        return [
            w
            * math.exp(
                -0.5
                * (
                    ((n - en) / SIGMA_NEXT) ** 2
                    + ((pv - ep) / SIGMA_PREV) ** 2
                    + (_clock_gap(c, ec) / SIGMA_CLOCK) ** 2
                    + ((debt - ed) / SIGMA_DEBT) ** 2
                )
            )
            for en, ep, ec, ed, _, w in learned
        ]

    start = datetime.fromtimestamp(
        math.ceil(start.timestamp() / STEP.total_seconds()) * STEP.total_seconds(), UTC
    )
    best_score, best_t, best_mass = -1.0, start, 0.0
    # A schedule bedtime already behind (after a daytime sleep, say) says less about when
    # the next sleep comes, so look further.
    end = rule + NIGHT_SPAN if rule >= start else start + 2 * NIGHT_SPAN
    # Already at a commitment: the sleep comes after it.
    for s in shifts:
        if s.start <= start < s.end:
            end = max(end, s.end + NIGHT_SPAN)
    end = min(end, start + LOOKAHEAD)

    def room_to_sleep(t: datetime) -> bool:
        _, nxt = neighbours(t, shifts)
        return nxt is None or nxt.start - p.prep - t >= p.min_sleep

    t = start
    while t <= end:
        # Up to the schedule bedtime a short night still counts (a quick turnaround);
        # later, a sleep needs room before the next commitment.
        if not at_work(t, shifts) and (t <= rule or room_to_sleep(t)):
            mass = sum(similarity(t))
            prior = PRIOR_WEIGHT * math.exp(-0.5 * (_hours(t - rule) / SIGMA_RULE) ** 2)
            if mass + prior > best_score:
                best_score, best_t, best_mass = mass + prior, t, mass
        t += STEP
    if best_score < 0:
        # Nothing fits (a commitment longer than the search): the first free moment.
        t = start
        while at_work(t, shifts) and t < start + 2 * LOOKAHEAD:
            t += STEP
        best_t = t

    # Expected length: how long similar nights lasted, pulled toward the target while
    # there is little to go on. A shift still sets the latest possible wake.
    weights = similarity(best_t)
    hours = (
        sum(k * d for k, (*_, d, _w) in zip(weights, learned, strict=True))
        + PRIOR_WEIGHT * _hours(p.target_sleep)
    ) / (sum(weights) + PRIOR_WEIGHT)
    wake = best_t + timedelta(hours=hours)
    prev, nxt = neighbours(best_t, shifts)
    if nxt:
        wake = max(min(wake, nxt.start - p.prep), best_t)
    return Prediction(
        bedtime=best_t,
        wake=wake,
        schedule_bedtime=rule,
        confidence=best_mass / (best_mass + PRIOR_WEIGHT),
        prev_end=prev.end if prev else None,
        next_start=nxt.start if nxt else None,
        nights_used=len(learned),
        prep=p.prep,
        usual_bedtime=habits.usual_bedtime,
        target_sleep=habits.target_sleep,
        unwind=habits.unwind,
    )


def combine(
    asleep: Iterable[str | None],
    in_bed: Iterable[str | None],
    home: bool,
    asleep_values: Iterable[str],
) -> tuple[bool, bool, bool]:
    """Fold every sleep signal into one reading.

    Returns (on, sure, known):
    * on: a tracker reports sleep, or an in-bed signal sees someone while they are home
    * sure: a tracker reports sleep, so no settling wait is needed
    * known: at least one signal is reporting at all
    """
    values = {v.lower() for v in asleep_values}
    trackers = [s.lower() for s in asleep if s is not None and s.lower() not in _UNUSABLE]
    beds = [s.lower() for s in in_bed if s is not None and s.lower() not in _UNUSABLE]
    if not trackers and not beds:
        return False, False, False
    sure = any(s in values for s in trackers)
    in_bed_on = home and any(s in IN_BED_VALUES for s in beds)
    return sure or in_bed_on, sure, True


# Out of bed this briefly while settling (shifting, a dropout) doesn't restart the clock.
SETTLE_GAP = timedelta(minutes=5)


class SleepDetector:
    """Turns noisy sleep signals into nights.

    Fed the combined reading from `combine`. A night starts when the reading has been on
    for `settle` (in-bed signals) or immediately (a tracker reporting sleep); its onset is
    when that stretch began, bridging gaps of up to SETTLE_GAP. It ends once the reading
    has been off for `wake_gap`; the wake time is when it went off. Short sleeps (naps) and
    implausibly long ones are dropped.
    """

    def __init__(self) -> None:
        self.asleep = False
        self.onset: datetime | None = None
        self.tracked = False
        # The in-bed stretch being settled into, and when it last dropped out.
        self.run_start: datetime | None = None
        self.on_since: datetime | None = None
        self.left_at: datetime | None = None

    def step(
        self, now: datetime, on: bool, since: datetime, sure: bool, p: Params
    ) -> tuple[datetime, datetime, str] | None:
        if not self.asleep:
            if not on:
                if self.run_start is not None and self.left_at is None:
                    self.left_at = since
                return None
            if self.run_start is None:
                self.run_start = since
            elif since != self.on_since:
                # Back in bed: the same stretch if the dropout was brief.
                brief = self.left_at is not None and since - self.left_at <= SETTLE_GAP
                if not brief:
                    self.run_start = since
            self.on_since, self.left_at = since, None
            if sure or now - self.run_start >= p.settle:
                self.asleep, self.onset, self.tracked = True, self.run_start, sure
                self.run_start = None
            return None

        self.tracked = self.tracked or sure
        if on or now - since < p.wake_gap:
            return None
        onset, wake = self.onset, since
        source = SOURCE_TRACKER if self.tracked else SOURCE_IN_BED
        self.asleep, self.onset, self.tracked = False, None, False
        if onset is None or not p.min_sleep <= wake - onset <= MAX_SLEEP:
            return None
        return onset, wake, source

    def as_dict(self) -> dict[str, Any]:
        return {
            "asleep": self.asleep,
            "onset": _iso(self.onset),
            "tracked": self.tracked,
            "run_start": _iso(self.run_start),
            "on_since": _iso(self.on_since),
            "left_at": _iso(self.left_at),
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.asleep = bool(data.get("asleep"))
        self.onset = _parse(data.get("onset"))
        self.tracked = bool(data.get("tracked"))
        self.run_start = _parse(data.get("run_start"))
        self.on_since = _parse(data.get("on_since"))
        self.left_at = _parse(data.get("left_at"))

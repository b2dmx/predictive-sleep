"""Model tests. Run: py -3 tests/test_model.py (or pytest)."""
from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

# Load model.py directly so Home Assistant is not needed.
_path = Path(__file__).parents[1] / "custom_components" / "predictive_bedtime" / "model.py"
_spec = importlib.util.spec_from_file_location("model", _path)
model = importlib.util.module_from_spec(_spec)
sys.modules["model"] = model
_spec.loader.exec_module(model)

TZ = ZoneInfo("America/New_York")
P = model.Params()


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ).astimezone(model.UTC)


def shift(day: int, start: int, end: int) -> model.Shift:
    s = at(day, start)
    e = at(day, end) if end > start else at(day + 1, end)
    return model.Shift(s, e)


# A backward-rotating week: evenings drifting earlier, then a quick turn to days.
ROTATION = [shift(24, 15, 23), shift(25, 13, 21), shift(26, 13, 21), shift(27, 7, 15), shift(28, 6, 14)]


def local(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%d %H:%M")


def test_quick_turn_is_floored_by_unwind():
    # 13-21 then 07-15: a full night is impossible, so bed right after unwinding.
    assert local(model.schedule_bedtime(at(26, 21, 30), ROTATION, P, TZ)) == "26 22:30"


def test_late_shift_pushes_bedtime_past_unwind():
    assert local(model.schedule_bedtime(at(24, 23, 10), ROTATION, P, TZ)) == "25 00:30"


def test_early_shift_pulls_bedtime_earlier():
    # 07-15 then 06-14: latest bedtime for 7.5h + 75min prep is 21:15.
    assert local(model.schedule_bedtime(at(27, 16), ROTATION, P, TZ)) == "27 21:15"


def test_free_day_uses_usual_bedtime():
    assert local(model.schedule_bedtime(at(20, 12), [], P, TZ)) == "20 23:30"


def test_no_history_follows_schedule_with_zero_confidence():
    pred = model.predict(at(27, 16), at(27, 16), ROTATION, [], P, TZ)
    assert local(pred.bedtime) == "27 21:10" or local(pred.bedtime) == "27 21:20"
    assert pred.confidence == 0
    # 7.5 h after 21:10, which is just inside the 04:45 deadline for a 06:00 shift.
    assert local(pred.wake) == "28 04:40"


def test_learns_later_habit_before_early_shifts():
    # Schedule says 21:15 before a 06:00 shift; this person actually goes down at 22:30.
    shifts, episodes = [], []
    for d in range(60):
        base = datetime(2026, 6, 1, tzinfo=TZ) + timedelta(days=d)
        s = model.Shift(base.replace(hour=6).astimezone(model.UTC), base.replace(hour=14).astimezone(model.UTC))
        shifts.append(s)
        onset = (base - timedelta(days=1)).replace(hour=22, minute=30).astimezone(model.UTC)
        episodes.append((onset, s.start - timedelta(hours=1)))
    episodes = [model.make_episode(o, w, shifts) for o, w in episodes]
    now = shifts[-1].end + timedelta(hours=2)
    upcoming = shifts + [model.Shift(shifts[-1].start + timedelta(days=1), shifts[-1].end + timedelta(days=1))]
    pred = model.predict(now, now, upcoming, episodes, P, TZ)
    # No departures recorded, so get-ready stays at the 75 min setting: 21:15.
    assert local(pred.schedule_bedtime).endswith("21:15")
    assert pred.bedtime.astimezone(TZ).strftime("%H:%M") in ("22:20", "22:30", "22:40")
    assert pred.confidence > 0.9


def test_interpolates_unseen_shift_time():
    # Learned 22:30 before 06:00 shifts only; a never-seen 08:00 shift should land later.
    shifts, episodes = [], []
    for d in range(30):
        base = datetime(2026, 6, 1, tzinfo=TZ) + timedelta(days=d)
        s = model.Shift(base.replace(hour=6).astimezone(model.UTC), base.replace(hour=14).astimezone(model.UTC))
        shifts.append(s)
        onset = (base - timedelta(days=1)).replace(hour=22, minute=30).astimezone(model.UTC)
        episodes.append((onset, s.start - timedelta(hours=1)))
    episodes = [model.make_episode(o, w, shifts) for o, w in episodes]
    now = shifts[-1].end + timedelta(hours=2)
    nxt = (datetime(2026, 7, 1, tzinfo=TZ)).replace(hour=8).astimezone(model.UTC)
    pred = model.predict(now, now, shifts + [model.Shift(nxt, nxt + timedelta(hours=8))], episodes, P, TZ)
    h = pred.bedtime.astimezone(TZ)
    assert h.hour * 60 + h.minute > 22 * 60 + 30, local(pred.bedtime)


def test_detector_debounces_and_drops_naps():
    det = model.SleepDetector()
    # In bed 22:00, briefly out 22:05 (not settled), back 22:10.
    assert det.step(at(26, 22, 5), True, at(26, 22), False, P) is None
    assert not det.asleep
    assert det.step(at(26, 22, 40), True, at(26, 22, 10), False, P) is None
    assert det.asleep and local(det.onset) == "26 22:10"
    # Up for 10 minutes at 02:00: still asleep.
    assert det.step(at(27, 2, 10), False, at(27, 2), False, P) is None
    assert det.asleep
    # Out of bed at 06:00 for good.
    done = det.step(at(27, 6, 45), False, at(27, 6), False, P)
    assert done and local(done[0]) == "26 22:10" and local(done[1]) == "27 06:00"
    assert done[2] == model.SOURCE_IN_BED
    # A 1-hour nap is not a night.
    det.step(at(27, 16, 30), True, at(27, 16), False, P)
    assert det.step(at(27, 17, 45), False, at(27, 17), False, P) is None


def test_tracker_counts_immediately_and_marks_the_night():
    det = model.SleepDetector()
    det.step(at(26, 23, 1), True, at(26, 23), True, P)
    assert det.asleep and local(det.onset) == "26 23:00"
    done = det.step(at(27, 7, 31), False, at(27, 7), False, P)
    assert done[2] == model.SOURCE_TRACKER


VALUES = model.DEFAULT_ASLEEP_VALUES


def test_combine_needs_someone_home_for_in_bed_signals():
    assert model.combine([], ["on"], True, VALUES) == (True, False, True)
    assert model.combine([], ["on"], False, VALUES) == (False, False, True)
    # A tracker still counts away from home (travel is still sleep).
    assert model.combine(["asleep"], [], False, VALUES) == (True, True, True)


def test_combine_fills_gaps_between_signals():
    # Flaky bed sensor unavailable, tracker still reporting deep sleep.
    assert model.combine(["deep"], ["unavailable"], True, VALUES) == (True, True, True)
    # mmWave sees someone, tracker says awake: in bed, not sure.
    assert model.combine(["awake"], ["detected"], True, VALUES) == (True, False, True)
    # Nothing reporting at all is unknown, not awake.
    assert model.combine(["unavailable"], [None], True, VALUES)[2] is False


def test_combine_custom_states_are_case_insensitive():
    assert model.combine(["Sleeping"], [], True, ["sleeping"])[1] is True
    assert model.combine(["core"], [], True, ["Core"])[1] is True


def test_shift_keywords_match_whole_words_only():
    assert model.is_shift("Work (Sam)", ["work"])
    assert model.is_shift("Bailey work", ["Work"])
    assert model.is_shift("Night shift", ["work", "shift"])
    assert not model.is_shift("Workout", ["work"])
    assert not model.is_shift("Dentist", ["work"])
    # No keywords: every event is a shift.
    assert model.is_shift("Dentist", [])
    assert model.is_shift(None, [" "])


def _free_nights(count: int, hour: int, length_h: float) -> list:
    """Nights with no shifts nearby, starting at hour and lasting length_h."""
    nights = []
    for d in range(count):
        onset = datetime(2026, 6, 1, hour, tzinfo=TZ).astimezone(model.UTC) + timedelta(days=d)
        nights.append(model.Episode(onset, onset + timedelta(hours=length_h), None, None))
    return nights


def test_learns_how_long_nights_last():
    # Free days: to bed at 23:00, up after 9.5 h. The target is 7.5 h.
    nights = _free_nights(30, 23, 9.5)
    now = nights[-1].wake + timedelta(hours=4)
    pred = model.predict(now, now, [], nights, P, TZ)
    hours = (pred.wake - pred.bedtime).total_seconds() / 3600
    assert 9.0 < hours <= 9.5, hours


def test_shift_still_caps_learned_wake():
    nights = _free_nights(30, 23, 9.5)
    now = nights[-1].wake + timedelta(hours=4)
    bed = datetime(2026, 7, 1, 23, tzinfo=TZ).astimezone(model.UTC)
    shift = model.Shift(bed + timedelta(hours=8), bed + timedelta(hours=16))
    pred = model.predict(now, now, [shift], nights, P, TZ)
    assert pred.wake <= shift.start - P.prep


def test_slept_before_counts_the_last_48_hours():
    nights = _free_nights(3, 23, 8)
    t = nights[-1].wake + timedelta(hours=10)
    # Two full nights fall inside the 48 h window before t, the third only partly.
    assert 16 <= model.slept_before(t, nights) <= 24


def test_sleep_debt_is_off_by_default_and_optional():
    assert P.use_sleep_debt is False
    nights = _free_nights(20, 23, 8)
    now = nights[-1].wake + timedelta(hours=4)
    with_debt = model.predict(now, now, [], nights, model.Params(use_sleep_debt=True), TZ)
    assert with_debt.confidence > 0.5


def test_accuracy_averages_recent_predicted_nights():
    onset = at(20, 23)
    nights = [
        model.Episode(onset, onset + timedelta(hours=8), None, None, predicted=onset - timedelta(minutes=30)),
        model.Episode(onset, onset + timedelta(hours=8), None, None, predicted=onset + timedelta(minutes=10)),
        model.Episode(onset, onset + timedelta(hours=8), None, None),
    ]
    minutes, count = model.accuracy(nights)
    assert count == 2 and round(minutes) == 20
    assert model.accuracy([]) is None


MIXED = {"mode": "mixed", "words": list(model.DEFAULT_WAKE_WORDS), "require_name": False}


def test_work_calendar_counts_every_event_as_work():
    assert model.classify("Anything", None, {"mode": "work"}, "Sam") == model.KIND_WORK


def test_mixed_calendar_keeps_wake_up_events_only():
    assert model.classify("Dentist", None, MIXED, "Sam") == model.KIND_APPOINTMENT
    assert model.classify("Sam work", None, MIXED, "Sam") == model.KIND_WORK
    assert model.classify("Mom's birthday dinner", None, MIXED, "Sam") is None
    # The word can be in the description too.
    assert model.classify("Checkup", "doctor at 9", MIXED, "Sam") == model.KIND_APPOINTMENT


def test_shared_calendar_can_require_the_persons_name():
    rule = {**MIXED, "require_name": True}
    assert model.classify("Sam dentist", None, rule, "Sam") == model.KIND_APPOINTMENT
    assert model.classify("Alex dentist", None, rule, "Sam") is None
    assert model.classify("Dentist", "for Sam", rule, "Sam") == model.KIND_APPOINTMENT


def test_appointments_limit_wake_up_but_are_not_shift_ends():
    appointment = model.Shift(at(28, 9), at(28, 10), model.KIND_APPOINTMENT)
    prev, nxt = model.neighbours(at(28, 12), [appointment])
    assert prev is None  # not a shift that ended
    prev, nxt = model.neighbours(at(27, 22), [appointment])
    assert nxt == appointment  # but it is the next thing to be up for
    # A 09:00 appointment pulls a free-day 23:30 bedtime earlier only if needed: 09:00 - 75 min
    # - 7.5 h = 00:15, so 23:30 stands.
    assert local(model.schedule_bedtime(at(27, 20), [appointment], P, TZ)) == "27 23:30"
    early = model.Shift(at(28, 6), at(28, 7), model.KIND_APPOINTMENT)
    assert local(model.schedule_bedtime(at(27, 20), [early], P, TZ)) == "27 21:15"


def test_get_ready_time_is_learned_from_real_mornings():
    # Up 45 minutes before a 07:00 shift and out the door 20 minutes later, ten times.
    nights = []
    for d in range(10):
        shift = datetime(2026, 6, 2, 7, tzinfo=TZ).astimezone(model.UTC) + timedelta(days=d)
        wake = shift - timedelta(minutes=45)
        left = wake + timedelta(minutes=20)
        nights.append(model.Episode(wake - timedelta(hours=8), wake, None, shift, left_home=left))
    now = nights[-1].wake + timedelta(hours=10)
    minutes = model.learned_prep(now, nights, P).total_seconds() / 60
    assert 45 <= minutes < 52, minutes  # 75 min setting, pulled almost all the way to 45


def test_get_ready_time_depends_on_the_start_time():
    # Quick for 07:00 starts (50 min), slower for 09:30 starts (85 min).
    nights = []
    for d, (hour, minute, lead) in enumerate([(7, 0, 50), (9, 30, 85)] * 6):
        start = datetime(2026, 6, 2 + d, hour, minute, tzinfo=TZ).astimezone(model.UTC)
        wake = start - timedelta(minutes=lead)
        nights.append(
            model.Episode(wake - timedelta(hours=8), wake, None, start, left_home=wake + timedelta(minutes=20))
        )
    now = nights[-1].wake + timedelta(hours=10)
    seven = datetime(2026, 6, 30, 7, tzinfo=TZ).astimezone(model.UTC)
    half_nine = datetime(2026, 6, 30, 9, 30, tzinfo=TZ).astimezone(model.UTC)
    early = model.learned_prep(now, nights, P, TZ, seven).total_seconds() / 60
    late = model.learned_prep(now, nights, P, TZ, half_nine).total_seconds() / 60
    assert 50 <= early < 58, early
    assert 78 < late <= 85, late


def test_lazy_mornings_do_not_count_as_getting_ready():
    shift = at(20, 14)
    # Up at 8 for a 2 pm shift, leaving at 1:15 pm: not a get-ready morning.
    lazy = model.Episode(at(19, 23), at(20, 8), None, shift, left_home=at(20, 13, 15))
    # Up 2 h before, but never recorded leaving soon after: not counted either.
    stayed = model.Episode(at(19, 23), at(20, 12), None, shift)
    assert not model.is_get_ready_morning(lazy)
    assert not model.is_get_ready_morning(stayed)
    assert model.learned_prep(at(21, 0), [lazy, stayed], P) == P.prep


def test_first_departure_after_waking():
    wake = at(20, 6)
    changes = [
        (at(19, 18), "home"),
        (at(20, 6, 10), "unavailable"),
        (at(20, 6, 40), "not_home"),
        (at(20, 7), "work"),
    ]
    assert local(model.first_departure(wake, changes)) == "20 06:40"
    # Leaving 3 hours later is not a get-up-and-go morning.
    assert model.first_departure(wake, [(at(19, 18), "home"), (at(20, 9), "not_home")]) is None


def test_usual_bedtime_is_learned_from_free_nights():
    # Free nights around 22:30 pull a 23:30 setup value most of the way there.
    nights = _free_nights(20, 22, 8)
    nights = [model.Episode(e.onset + timedelta(minutes=30), e.wake, None, None) for e in nights]
    now = nights[-1].wake + timedelta(hours=4)
    hours, count = model.learned_usual_bedtime(now, nights, P, TZ)
    assert count == 20 and 22.5 <= hours < 22.7, hours


def test_usual_bedtime_averages_across_midnight():
    late = [model.Episode(at(10 + d, 23, 30), at(11 + d, 8), None, None) for d in range(0, 10, 2)]
    later = [model.Episode(at(11 + d, 0, 30) + timedelta(days=1), at(12 + d, 9), None, None) for d in range(0, 10, 2)]
    hours, _ = model.learned_usual_bedtime(at(25, 12), late + later, model.Params(free_bedtime=model.time(0, 0)), TZ)
    assert hours < 0.5 or hours > 23.5, hours  # midnight, not noon


def test_nights_before_an_early_shift_are_not_free():
    shift = at(21, 7)
    assert not model.is_free_night(model.Episode(at(20, 22), at(21, 6), None, shift))
    assert model.is_free_night(model.Episode(at(20, 22), at(21, 8), None, at(21, 14)))


def test_predictions_use_learned_habits_not_setup_values():
    nights = [model.Episode(e.onset + timedelta(minutes=30), e.wake, None, None) for e in _free_nights(20, 22, 8)]
    now = nights[-1].wake + timedelta(hours=4)
    pred = model.predict(now, now, [], nights, P, TZ)
    assert 22.5 <= pred.usual_bedtime < 22.7
    assert "22:30" <= local(pred.schedule_bedtime)[3:] <= "22:45", local(pred.schedule_bedtime)


def test_daytime_sleeps_do_not_count_toward_usual_bedtime():
    nights = [model.Episode(e.onset + timedelta(minutes=30), e.wake, None, None) for e in _free_nights(10, 22, 8)]
    # Two long afternoon sleeps on days off.
    naps = [model.Episode(at(20 + d, 13, 30), at(20 + d, 17), None, None) for d in range(2)]
    now = nights[-1].wake + timedelta(hours=4)
    with_naps, count = model.learned_usual_bedtime(now, nights + naps, P, TZ)
    without, _ = model.learned_usual_bedtime(now, nights, P, TZ)
    assert count == 10 and abs(with_naps - without) < 0.01


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as err:
                failures += 1
                print(f"FAIL {name}: {err}")
    sys.exit(1 if failures else 0)


"""P3: the week planner's placement (backend/brain/planner.py) -- pure, so no fakes at all.

The acceptance line from the roadmap: no overlaps with busy times, rules respected, a
clock-change week correct, unplaceable items reported, never a block in the past. The last
group of tests in the placement section is randomised over hundreds of weeks (including the
two clock-change weeks) and checks those invariants on whatever comes out.
"""
import random
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.brain import planner as P
from backend.brain.planner import Block, Interval, LOCAL_TZ, RulesError, at, plus

WEEK = date(2026, 10, 5)                     # a Monday, no clock change


def day(offset, week=WEEK):
    return week + timedelta(days=offset)


def busy(offset, start, end, label="", week=WEEK):
    return Interval(at(day(offset, week), start), at(day(offset, week), end), label)


def rules(**kw):
    return P.normalize_rules(kw)


GYM4 = {"blocks": [{"kind": "gym", "count": 4, "minutes": 60, "prefer": "morning"}]}


def local(dt):
    return dt.astimezone(LOCAL_TZ)


# ===========================================================================================
# Reading the rules
# ===========================================================================================

@pytest.mark.parametrize("spec,days", [
    ("mon-fri", (0, 1, 2, 3, 4)), ("mon,wed,fri", (0, 2, 4)), ("weekdays", (0, 1, 2, 3, 4)),
    ("weekend", (5, 6)), ("all", tuple(range(7))), (["Mon", "Wednesday"], (0, 2)),
    ("fri-mon", (0, 4, 5, 6)), ("Tues and Thurs", (1, 3)), ("sat", (5,)),
])
def test_days_are_read_in_the_ways_people_write_them(spec, days):
    got, given = P.parse_days(spec, default=(9,))
    assert got == days and given is True


def test_no_days_means_the_default_and_says_it_was_not_given():
    assert P.parse_days(None, default=(1, 2)) == ((1, 2), False)
    assert P.parse_days("", default=(1, 2)) == ((1, 2), False)


@pytest.mark.parametrize("bad", ["someday", "mon-xyz", "funday, mon"])
def test_a_word_that_is_not_a_day_is_an_error_not_a_guess(bad):
    with pytest.raises(RulesError):
        P.parse_days(bad, default=())


def test_minimal_rules_fill_in_the_defaults():
    r = rules(blocks=[{"kind": "Gym", "count": 2, "minutes": 45}])
    (spec,) = r.blocks
    assert (spec.kind, spec.count, spec.minutes, spec.prefer) == ("gym", 2, 45, "")
    assert spec.days == tuple(range(7)) and spec.days_given is False
    assert (r.not_before, r.not_after, r.buffer, r.has_work) == ("06:00", "22:00", 0, False)


def test_rules_may_arrive_as_a_json_string():
    r = P.normalize_rules('{"blocks": [{"kind": "gym", "count": 1, "minutes": 60}]}')
    assert r.blocks[0].kind == "gym"


@pytest.mark.parametrize("raw,fragment", [
    ({"gym": 4}, "unknown field(s) gym"),                                    # a rule typed under the wrong name
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60, "when": "am"}]}, "unknown field(s) when"),
    ({"work": {"start": "09:00", "end": "17:00", "hours": 8}}, "unknown field(s) hours"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60}], "not_after": "25:00"}, "not a valid time"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60}], "not_before": "9h"}, "not a time"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60}], "not_before": "22:00"}, "must be earlier"),
    ({"work": {"start": "17:00", "end": "09:00"}}, "must be earlier"),
    ({"work": {"start": "09:00"}}, "work.end is missing"),
    ({"blocks": [{"kind": "gym", "count": 0, "minutes": 60}]}, "between 1 and 14"),
    ({"blocks": [{"kind": "gym", "count": 15, "minutes": 60}]}, "between 1 and 14"),
    ({"blocks": [{"kind": "gym", "count": 2, "minutes": 5}]}, "between 15 and 600"),
    ({"blocks": [{"kind": "gym", "count": 2, "minutes": "an hour"}]}, "whole number"),
    ({"blocks": [{"kind": "work", "count": 1, "minutes": 60}]}, "work field"),
    ({"blocks": [{"kind": "", "count": 1, "minutes": 60}]}, "short lowercase name"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60}, {"kind": "gym", "count": 1, "minutes": 60}]}, "twice"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60, "prefer": "noon"}]}, "morning, afternoon or evening"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60, "days": "someday"}]}, "not a day"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60}], "buffer": 90}, "between 0 and 60"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60}], "keep_free": [{"days": "wed", "from": "23:00", "to": "18:00"}]}, "must be earlier"),
    ({"blocks": [{"kind": "gym", "count": 1, "minutes": 60}], "keep_free": "wednesday"}, "must be a list"),
    ({"blocks": "gym"}, "must be a list"),
    ({}, "ask for nothing"),
    ([], "must be an object"),
])
def test_rules_that_cannot_be_used_say_exactly_what_to_change(raw, fragment):
    with pytest.raises(RulesError, match=re.escape(fragment)):
        P.normalize_rules(raw)


def test_a_kind_cannot_widen_the_global_hours():
    r = rules(not_after="20:00", blocks=[{"kind": "gym", "count": 1, "minutes": 60, "not_after": "23:00"}])
    assert r.blocks[0].not_after == "20:00"
    with pytest.raises(RulesError, match="no hours are left"):
        rules(not_before="07:00", not_after="09:00",
              blocks=[{"kind": "gym", "count": 1, "minutes": 60, "not_before": "10:00", "not_after": "12:00"}])


def test_normalized_rules_round_trip_through_storage():
    r = rules(work={"days": "mon-fri", "start": "09:00", "end": "17:00", "min_minutes": 60},
              blocks=[{"kind": "gym", "count": 4, "minutes": 60, "prefer": "evening", "days": "mon-sat",
                       "not_after": "20:00", "max_per_day": 1},
                      {"kind": "reading", "count": 2, "minutes": 30}],
              keep_free=[{"days": "wed", "from": "18:00", "to": "23:00"}], buffer=15)
    assert P.normalize_rules(r.as_dict()) == r


# ===========================================================================================
# Time
# ===========================================================================================

def test_a_time_in_the_skipped_hour_moves_to_the_real_instant():
    assert at(date(2027, 3, 28), "02:30") == datetime(2027, 3, 28, 1, 30, tzinfo=timezone.utc)
    assert local(at(date(2027, 3, 28), "02:30")).strftime("%H:%M") == "03:30"


def test_a_time_that_happens_twice_is_the_first_of_the_two():
    assert at(date(2026, 10, 25), "02:30") == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)


def test_plus_counts_real_minutes_across_a_clock_change():
    start = at(date(2027, 3, 28), "01:30")
    end = plus(start, 60)
    assert P.minutes_between(start, end) == 60 and local(end).strftime("%H:%M") == "03:30"


@pytest.mark.parametrize("h,m,s,expect", [(10, 0, 0, "10:00"), (10, 1, 0, "10:15"), (10, 14, 59, "10:15"),
                                          (10, 15, 0, "10:15"), (10, 45, 1, "11:00"), (23, 50, 0, "00:00")])
def test_times_align_up_to_the_quarter_hour(h, m, s, expect):
    dt = datetime(2026, 10, 5, h, m, s, tzinfo=LOCAL_TZ)
    assert local(P._align_up(dt)).strftime("%H:%M") == expect


# ===========================================================================================
# Placement
# ===========================================================================================

def test_four_gym_sessions_land_on_four_different_weekdays_in_the_morning():
    plan = P.plan_week(WEEK, P.normalize_rules(GYM4), [])
    gyms = [b for b in plan.blocks if b.kind == "gym"]
    assert len(gyms) == 4 and not plan.unplaced
    days = [local(b.start).date() for b in gyms]
    assert len(set(days)) == 4, "one a day"
    assert all(d.weekday() < 5 for d in days), "he did not name the days: weekdays are preferred"
    for b in gyms:
        assert P.minutes_between(b.start, b.end) == 60
        assert 6 <= local(b.start).hour < 12
        assert local(b.start).minute % 15 == 0


def test_gym_sessions_are_spread_over_the_week_not_stacked():
    plan = P.plan_week(WEEK, P.normalize_rules(
        {"blocks": [{"kind": "gym", "count": 2, "minutes": 60, "prefer": "morning", "days": "mon-fri"}]}), [])
    a, b = [local(x.start).date().weekday() for x in plan.blocks]
    assert b - a >= 2, "two sessions a week are not on neighbouring days"


def test_a_day_he_names_is_used_even_at_the_weekend():
    plan = P.plan_week(WEEK, P.normalize_rules(
        {"blocks": [{"kind": "long run", "count": 1, "minutes": 90, "days": "sun"}]}), [])
    assert local(plan.blocks[0].start).date().weekday() == 6


def test_not_after_is_a_hard_limit():
    plan = P.plan_week(WEEK, P.normalize_rules(
        {"blocks": [{"kind": "gym", "count": 4, "minutes": 60, "prefer": "evening", "not_after": "20:00"}]}), [])
    assert len(plan.blocks) == 4
    assert all(local(b.end).strftime("%H:%M") <= "20:00" for b in plan.blocks)


def test_keep_wednesday_evening_free_is_respected():
    r = P.normalize_rules({"blocks": [{"kind": "gym", "count": 7, "minutes": 60, "prefer": "evening",
                                        "days": "all", "max_per_day": 1}],
                           "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}]})
    plan = P.plan_week(WEEK, r, [])
    wed = [b for b in plan.blocks if local(b.start).date() == day(2)]
    for b in wed:
        assert b.end <= at(day(2), "18:00") or b.start >= at(day(2), "23:00")


def test_a_buffer_is_kept_clear_around_a_meeting():
    meeting = busy(0, "18:00", "19:00", "Board call")
    r = P.normalize_rules({"blocks": [{"kind": "gym", "count": 1, "minutes": 60, "prefer": "evening",
                                        "days": "mon"}], "buffer": 15})
    (b,) = P.plan_week(WEEK, r, [meeting]).blocks
    assert b.end <= plus(meeting.start, -15) or b.start >= plus(meeting.end, 15)


def test_a_block_never_lands_on_an_event():
    events = [busy(0, "06:00", "21:00", "Offsite"), busy(1, "07:00", "08:30", "Standup"),
              busy(2, "12:00", "13:30", "Lunch")]
    plan = P.plan_week(WEEK, P.normalize_rules(GYM4), events)
    for b in plan.blocks:
        for e in events:
            assert b.end <= e.start or b.start >= e.end


def test_work_fills_the_free_time_inside_working_hours_around_a_meeting():
    r = P.normalize_rules({"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"}})
    plan = P.plan_week(WEEK, r, [busy(0, "11:00", "12:00", "Design review")])
    monday = [(local(b.start).strftime("%H:%M"), local(b.end).strftime("%H:%M"))
              for b in plan.blocks if local(b.start).date() == day(0)]
    assert monday == [("09:00", "11:00"), ("12:00", "17:00")], "work runs right up to a meeting"
    assert [b.kind for b in plan.blocks].count("work") == 6
    assert not any(local(b.start).date().weekday() >= 5 for b in plan.blocks)


def test_short_leftovers_are_not_made_into_work_blocks():
    r = P.normalize_rules({"work": {"days": "mon", "start": "09:00", "end": "17:00", "min_minutes": 60}})
    plan = P.plan_week(WEEK, r, [busy(0, "09:30", "16:30")])
    assert plan.blocks == (), "30 minutes either side is not a work block"


def test_work_stops_short_of_a_gym_block_by_the_buffer():
    r = P.normalize_rules({"work": {"days": "mon", "start": "09:00", "end": "17:00"},
                           "blocks": [{"kind": "gym", "count": 1, "minutes": 60, "days": "mon",
                                       "prefer": "afternoon"}], "buffer": 15})
    plan = P.plan_week(WEEK, r, [])
    gym = next(b for b in plan.blocks if b.kind == "gym")
    for w in (b for b in plan.blocks if b.kind == "work"):
        assert w.end <= plus(gym.start, -15) or w.start >= plus(gym.end, 15)


def test_the_first_kind_listed_gets_the_better_slots():
    r = P.normalize_rules({"blocks": [{"kind": "focus", "count": 1, "minutes": 120, "days": "mon", "prefer": "morning"},
                                      {"kind": "gym", "count": 1, "minutes": 60, "days": "mon", "prefer": "morning"}]})
    plan = P.plan_week(WEEK, r, [])
    by_kind = {b.kind: b for b in plan.blocks}
    assert by_kind["focus"].start < by_kind["gym"].start or by_kind["focus"].start == at(day(0), "07:30")


def test_nothing_is_placed_in_the_past():
    now = at(day(2), "10:07")                          # Wednesday 10:07
    plan = P.plan_week(WEEK, P.normalize_rules(
        {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"}, **GYM4}), [], now=now)
    assert plan.blocks and all(b.start >= now for b in plan.blocks)
    assert min(b.start for b in plan.blocks) == at(day(2), "10:15"), "aligned up to the quarter hour"
    assert not any(local(b.start).date() < day(2) for b in plan.blocks)


def test_a_request_that_cannot_be_met_is_reported_with_the_reason():
    events = [busy(3, "06:00", "22:00", "Offsite")] + [busy(d, "06:00", "22:00", "Trip") for d in (4, 5, 6)]
    plan = P.plan_week(WEEK, P.normalize_rules(GYM4), events)
    assert len([b for b in plan.blocks if b.kind == "gym"]) == 3
    (u,) = plan.unplaced
    assert (u.kind, u.wanted, u.placed, u.minutes) == ("gym", 4, 3, 60)
    assert "Thursday: no free time between 06:00 and 22:00" in u.reason
    assert "already has gym" in u.reason
    assert "Could not place 1 of 4 gym (60 min each)" in P.describe(plan)


def test_a_completely_busy_week_places_nothing_and_says_why():
    events = [busy(d, "05:00", "23:00", "Conference") for d in range(7)]
    plan = P.plan_week(WEEK, P.normalize_rules(GYM4), events)
    assert plan.blocks == ()
    assert plan.unplaced[0].placed == 0 and "no free time" in plan.unplaced[0].reason


def test_max_per_day_allows_two_on_a_day_when_asked():
    r = P.normalize_rules({"blocks": [{"kind": "study", "count": 3, "minutes": 60, "days": "mon", "max_per_day": 3}]})
    plan = P.plan_week(WEEK, r, [])
    assert len(plan.blocks) == 3 and len({local(b.start).date() for b in plan.blocks}) == 1


def test_a_second_pass_keeps_clear_of_blocks_that_already_stand():
    first = P.plan_week(WEEK, P.normalize_rules(GYM4), [])
    more = P.plan_week(WEEK, P.normalize_rules(
        {"blocks": [{"kind": "reading", "count": 3, "minutes": 30, "prefer": "morning"}]}), [],
        taken=first.blocks)
    for r in more.blocks:
        for g in first.blocks:
            assert r.end <= g.start or r.start >= g.end


def test_the_plan_does_not_depend_on_the_order_the_events_arrive_in():
    events = [busy(0, "07:00", "08:00"), busy(1, "12:00", "13:00"), busy(2, "18:00", "19:30"),
              busy(3, "09:00", "10:00")]
    rl = P.normalize_rules({"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"}, **GYM4})
    a = P.plan_week(WEEK, rl, events)
    b = P.plan_week(WEEK, rl, list(reversed(events)))
    assert a == b and a == P.plan_week(WEEK, rl, events)


def test_a_week_must_start_on_a_monday():
    with pytest.raises(ValueError):
        P.plan_week(date(2026, 10, 6), P.normalize_rules(GYM4), [])


def test_block_keys_are_unique_and_stable():
    plan = P.plan_week(WEEK, P.normalize_rules(
        {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"}, **GYM4}), [busy(0, "11:00", "12:00")])
    keys = [b.key for b in plan.blocks]
    assert len(keys) == len(set(keys))
    assert keys == [b.key for b in P.plan_week(WEEK, P.normalize_rules(
        {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"}, **GYM4}), [busy(0, "11:00", "12:00")]).blocks]


def test_describe_lists_the_week_day_by_day():
    plan = P.plan_week(WEEK, P.normalize_rules({"blocks": [{"kind": "gym", "count": 1, "minutes": 60, "days": "tue",
                                                            "prefer": "morning"}]}), [])
    assert P.describe(plan) == "Tue 2026-10-06\n  07:30-08:30 gym"
    assert P.describe(P.Placement(WEEK, (), ())) == "Nothing to place."


# ===========================================================================================
# Clock changes
# ===========================================================================================

def test_the_week_the_clocks_go_back_keeps_wall_clock_hours_and_real_durations():
    week = date(2026, 10, 19)                    # Sunday 25 October has 25 hours
    r = P.normalize_rules({"blocks": [
        {"kind": "run", "count": 2, "minutes": 60, "days": "mon,sun", "prefer": "morning",
         "not_before": "09:00", "not_after": "10:00"}]})
    plan = P.plan_week(week, r, [])
    mon = next(b for b in plan.blocks if local(b.start).date() == date(2026, 10, 19))
    sun = next(b for b in plan.blocks if local(b.start).date() == date(2026, 10, 25))
    assert local(mon.start).strftime("%H:%M") == local(sun.start).strftime("%H:%M") == "09:00"
    assert local(mon.start).utcoffset() == timedelta(hours=2) and local(sun.start).utcoffset() == timedelta(hours=1)
    assert P.minutes_between(mon.start, mon.end) == P.minutes_between(sun.start, sun.end) == 60


def test_the_week_the_clocks_go_forward_never_starts_a_block_in_the_hour_that_does_not_exist():
    week = date(2027, 3, 22)                     # Sunday 28 March 2027: 02:00 becomes 03:00
    r = P.normalize_rules({"not_before": "01:00", "not_after": "05:00",
                           "blocks": [{"kind": "night owl", "count": 1, "minutes": 60, "days": "sun"}]})
    (b,) = P.plan_week(week, r, []).blocks
    assert P.minutes_between(b.start, b.end) == 60
    assert local(b.start).hour != 2 and local(b.end).hour != 2 or local(b.end).strftime("%H:%M") == "02:00"
    every_start = []
    cursor = at(date(2027, 3, 28), "01:00")
    while cursor < at(date(2027, 3, 28), "05:00"):
        every_start.append(local(cursor).strftime("%H:%M"))
        cursor = plus(cursor, 15)
    assert not any(t.startswith("02:") for t in every_start), "the grid skips the hour that never happens"


def test_a_meeting_across_the_clock_change_blocks_its_real_duration():
    week = date(2027, 3, 22)
    meeting = Interval(at(date(2027, 3, 28), "01:30"), plus(at(date(2027, 3, 28), "01:30"), 120), "Night call")
    r = P.normalize_rules({"not_before": "00:00", "not_after": "06:00",
                           "blocks": [{"kind": "x", "count": 1, "minutes": 60, "days": "sun", "prefer": "morning"}]})
    (b,) = P.plan_week(week, r, [meeting]).blocks
    assert b.end <= meeting.start or b.start >= meeting.end


# ===========================================================================================
# The invariants, on hundreds of random weeks
# ===========================================================================================

WEEKS = [date(2026, 10, 5), date(2026, 10, 19), date(2026, 10, 26), date(2027, 3, 22), date(2027, 3, 29)]


def _random_case(rng):
    week = rng.choice(WEEKS)
    events = []
    for _ in range(rng.randint(0, 14)):
        d = day(rng.randint(0, 6), week)
        s = rng.randrange(6 * 60, 21 * 60, 15)
        e = min(s + rng.choice([30, 45, 60, 90, 120, 180]), 23 * 60)
        events.append(Interval(at(d, f"{s // 60:02d}:{s % 60:02d}"), at(d, f"{e // 60:02d}:{e % 60:02d}"), "e"))
    raw = {"buffer": rng.choice([0, 10, 15]),
           "not_before": rng.choice(["06:00", "07:00"]), "not_after": rng.choice(["20:00", "21:30", "22:00"])}
    if rng.random() < 0.7:
        raw["work"] = {"days": rng.choice(["mon-fri", "mon-thu", "weekdays"]),
                       "start": rng.choice(["08:00", "09:00"]), "end": rng.choice(["16:00", "17:30"]),
                       "min_minutes": rng.choice([30, 45, 60])}
    kinds = rng.sample(["gym", "reading", "run", "guitar"], rng.randint(0, 3))
    raw["blocks"] = [{"kind": k, "count": rng.randint(1, 5), "minutes": rng.choice([30, 45, 60, 90]),
                      "prefer": rng.choice(["", "morning", "afternoon", "evening"]),
                      "days": rng.choice(["", "mon-fri", "all", "mon,wed,fri,sat"]),
                      "max_per_day": rng.choice([1, 1, 2])} for k in kinds]
    if rng.random() < 0.5:
        a, z = rng.choice([("12:00", "14:00"), ("12:00", "22:00"), ("18:00", "22:00")])
        raw["keep_free"] = [{"days": rng.choice(["wed", "fri", "sun"]), "from": a, "to": z}]
    if not raw["blocks"] and "work" not in raw:
        raw["blocks"] = [{"kind": "gym", "count": 2, "minutes": 60}]
    return week, events, raw


def test_the_invariants_hold_on_random_weeks_including_the_clock_changes():
    rng = random.Random(20260930)
    now_choices = [None, None, at(day(2), "13:07"), at(date(2026, 10, 20), "08:00")]
    checked = 0
    for trial in range(400):
        week, events, raw = _random_case(rng)
        rules_ = P.normalize_rules(raw)
        now = rng.choice(now_choices)
        plan = P.plan_week(week, rules_, events, now=now)
        again = P.plan_week(week, rules_, list(reversed(events)), now=now)
        assert plan == again, f"trial {trial}: not deterministic"
        blocks = list(plan.blocks)
        assert blocks == sorted(blocks, key=lambda b: (b.start, b.kind))
        for i, b in enumerate(blocks):
            s, e = local(b.start), local(b.end)
            assert s.second == 0 and s.minute % 15 == 0, f"trial {trial}: off the grid {b}"
            assert e > s
            assert s.date() == e.date() or e.strftime("%H:%M") in ("00:00",), f"trial {trial}: crosses midnight"
            if now is not None:
                assert b.start >= now, f"trial {trial}: in the past"
            for other in blocks[i + 1:]:
                assert b.end <= other.start or other.end <= b.start, f"trial {trial}: blocks overlap"
            pad = 0 if b.kind == "work" else rules_.buffer
            for ev in events:
                assert b.end <= plus(ev.start, -pad) or b.start >= plus(ev.end, pad), \
                    f"trial {trial}: {b.kind} {s:%a %H:%M} hits an event"
            for days, a, z in rules_.keep_free:
                if s.date().weekday() in days:
                    assert b.end <= at(s.date(), a) or b.start >= at(s.date(), z), f"trial {trial}: keep-free broken"
            if b.kind == "work":
                assert s.date().weekday() in rules_.work_days
                assert b.start >= at(s.date(), rules_.work_start) and b.end <= at(s.date(), rules_.work_end)
                assert P.minutes_between(b.start, b.end) >= rules_.work_min
            else:
                spec = next(x for x in rules_.blocks if x.kind == b.kind)
                assert s.date().weekday() in spec.days
                assert b.start >= at(s.date(), spec.not_before) and b.end <= at(s.date(), spec.not_after)
                assert P.minutes_between(b.start, b.end) == spec.minutes
            checked += 1
        for spec in rules_.blocks:
            mine = [b for b in blocks if b.kind == spec.kind]
            unplaced = next((u for u in plan.unplaced if u.kind == spec.kind), None)
            placed_count = len(mine)
            assert placed_count + (unplaced.wanted - unplaced.placed if unplaced else 0) == spec.count
            if unplaced:
                assert unplaced.placed == placed_count and unplaced.reason
            per_day = {}
            for b in mine:
                per_day[local(b.start).date()] = per_day.get(local(b.start).date(), 0) + 1
            assert max(per_day.values(), default=0) <= spec.max_per_day
    assert checked > 1500, "the random weeks should have produced plenty of blocks to check"


# ===========================================================================================
# Editing one block
# ===========================================================================================

def _sample_plan():
    r = P.normalize_rules({"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"},
                           "blocks": [{"kind": "gym", "count": 2, "minutes": 60, "prefer": "evening",
                                       "days": "mon,thu"}], "buffer": 15,
                           "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}]})
    return r, P.plan_week(WEEK, r, [])


def test_find_blocks_by_kind_and_day_nearest_first():
    r, plan = _sample_plan()
    thu = P.parse_day("thursday", WEEK)
    assert thu == day(3) and P.parse_day("2026-10-08", WEEK) == day(3) and P.parse_day("soon", WEEK) is None
    (g,) = P.find_blocks(plan.blocks, kind="gym", day=thu)
    assert g.kind == "gym" and local(g.start).date() == thu
    on_monday = P.find_blocks(plan.blocks, day=day(0), near="17:45")
    assert on_monday == sorted(on_monday, key=lambda b: abs(local(b.start).hour * 60 + local(b.start).minute - (17 * 60 + 45)))
    assert P.find_blocks(plan.blocks, kind="swim") == []


def test_a_move_is_checked_against_everything_that_could_stop_it():
    r, plan = _sample_plan()
    gym = next(b for b in plan.blocks if b.kind == "gym")
    meeting = busy(0, "19:00", "20:00", "Dinner")
    others = [b for b in plan.blocks if b != gym]

    def moved(start_hhmm, minutes=60, d=day(0)):
        s = at(d, start_hhmm)
        return Block("gym", s, plus(s, minutes))

    assert P.check_block(moved("18:00"), others, [], r) is None
    assert "in the past" in P.check_block(moved("18:00"), others, [], r, now=at(day(0), "19:00"))
    assert "clashes with 'Dinner'" in P.check_block(moved("18:30"), others, [meeting], r)
    assert "clashes with 'Dinner'" in P.check_block(moved("20:10", d=day(0)), others, [meeting], r), "the buffer counts"
    assert "overlaps your work block" in P.check_block(moved("10:00"), others, [], r)
    assert "keep Wednesday 18:00-23:00 free" in P.check_block(moved("19:00", d=day(2)), others, [], r)
    assert "outside 06:00-22:00" in P.check_block(moved("21:30"), others, [], r)
    assert "not allowed on Tuesday" in P.check_block(moved("18:00", d=day(1)), others, [], r)
    assert "before it starts" in P.check_block(Block("gym", at(day(0), "18:00"), at(day(0), "17:00")), others, [], r)


def test_moving_a_work_block_needs_no_buffer_next_to_a_meeting():
    r, plan = _sample_plan()
    meeting = busy(1, "10:00", "11:00", "Standup")
    work = Block("work", at(day(1), "11:00"), at(day(1), "12:00"))
    assert P.check_block(work, [], [meeting], r) is None


def test_monday_of_any_day_in_the_week():
    for offset in range(7):
        assert P.monday_of(day(offset)) == WEEK
    assert P.monday_of(date(2026, 10, 12)) == date(2026, 10, 12)
    assert P.monday_of(date(2026, 10, 11)) == WEEK, "Sunday belongs to the week that started before it"
    assert P.monday_of(date(2026, 10, 25)).weekday() == 0        # the clock-change Sunday


def test_a_day_he_named_may_break_the_kinds_weekday_rule_but_nothing_else():
    r, plan = _sample_plan()                                     # gym is mon,thu only
    gym = next(b for b in plan.blocks if b.kind == "gym")
    others = [b for b in plan.blocks if b != gym]
    s = at(day(1), "18:00")
    tue = Block("gym", s, plus(s, 60))
    assert "not allowed on Tuesday" in P.check_block(tue, others, [], r)
    assert P.check_block(tue, others, [], r, ignore_days=True) is None
    wed = Block("gym", at(day(2), "19:00"), at(day(2), "20:00"))
    assert "keep Wednesday" in P.check_block(wed, others, [], r, ignore_days=True), "keep-free still holds"
    late = Block("gym", at(day(1), "21:30"), at(day(1), "22:30"))
    assert "outside" in P.check_block(late, others, [], r, ignore_days=True), "the hours still hold"


# ===========================================================================================
# One slot on one day
# ===========================================================================================

def test_find_slot_uses_the_preferred_part_of_the_day():
    r = P.normalize_rules(GYM4)
    for prefer, lo, hi in (("morning", 6, 12), ("afternoon", 12, 17), ("evening", 17, 22)):
        b = P.find_slot("gym", 60, day(1), r, [], [], prefer=prefer)
        assert lo <= local(b.start).hour < hi, prefer
        assert P.minutes_between(b.start, b.end) == 60 and local(b.start).date() == day(1)


def test_find_slot_keeps_clear_of_events_other_blocks_and_keep_free():
    r = P.normalize_rules({"buffer": 15, "keep_free": [{"days": "tue", "from": "07:00", "to": "12:00"}],
                           "blocks": [{"kind": "gym", "count": 1, "minutes": 60}]})
    meeting = busy(1, "13:00", "14:00", "Standup")
    other = Block("reading", at(day(1), "14:15"), at(day(1), "15:00"))
    b = P.find_slot("gym", 60, day(1), r, [meeting], [other], prefer="afternoon")
    assert b is not None and P.check_block(b, [other], [meeting], r) is None
    assert local(b.start).hour >= 12, "the morning is kept free"


def test_find_slot_says_none_when_the_day_has_no_room():
    r = P.normalize_rules(GYM4)
    assert P.find_slot("gym", 60, day(1), r, [busy(1, "00:00", "23:59", "All day")], []) is None
    assert P.find_slot("gym", 840, day(1), r, [busy(1, "08:00", "09:00", "x")], [], prefer="morning") is None, "14 hours fit nowhere"


def test_find_slot_never_returns_the_past_and_honours_max_per_day():
    r = P.normalize_rules(GYM4)
    now = at(day(1), "13:07")
    b = P.find_slot("gym", 60, day(1), r, [], [], prefer="morning", now=now)
    assert b.start >= now
    already = [Block("gym", at(day(1), "07:00"), at(day(1), "08:00"))]
    assert P.find_slot("gym", 60, day(1), r, [], already) is None, "one gym a day"
    two = P.normalize_rules({"blocks": [{"kind": "gym", "count": 4, "minutes": 60, "max_per_day": 2}]})
    assert P.find_slot("gym", 60, day(1), two, [], already) is not None


# ===========================================================================================
# Editing a block: the parts he named
# ===========================================================================================

def _gym():
    return Block("gym", at(day(3), "17:00"), at(day(3), "18:00"))


def hhmm(dt):
    return local(dt).strftime("%H:%M")


def test_a_new_start_moves_the_whole_block_and_keeps_its_length():
    b = P.edited(_gym(), start="18:30")
    assert (hhmm(b.start), hhmm(b.end)) == ("18:30", "19:30") and b.kind == "gym"


def test_a_new_end_moves_only_the_end():
    b = P.edited(_gym(), end="17:30")
    assert (hhmm(b.start), hhmm(b.end)) == ("17:00", "17:30")


def test_start_and_end_together_and_a_length():
    b = P.edited(_gym(), start="07:00", end="08:15")
    assert (hhmm(b.start), hhmm(b.end)) == ("07:00", "08:15")
    assert P.edited(_gym(), minutes=90).minutes == 90
    assert hhmm(P.edited(_gym(), start="09:00", minutes=45).end) == "09:45"
    with pytest.raises(RulesError, match="not both"):
        P.edited(_gym(), end="19:00", minutes=30)


def test_a_different_day_alone_keeps_the_clock_times():
    b = P.edited(_gym(), day=day(4))
    assert local(b.start).date() == day(4) and (hhmm(b.start), hhmm(b.end)) == ("17:00", "18:00")
    moved = P.edited(_gym(), day=day(4), start="08:00")
    assert local(moved.start).date() == day(4) and hhmm(moved.end) == "09:00"


def test_an_unreadable_time_is_an_error_not_a_guess():
    for bad in ("evening", "25:00", "18:75"):
        with pytest.raises(RulesError):
            P.edited(_gym(), start=bad)


def test_an_edit_across_the_clock_change_keeps_real_length():
    sunday = date(2026, 10, 25)
    b = Block("run", at(sunday, "01:00"), plus(at(sunday, "01:00"), 60))
    assert P.edited(b, minutes=180).minutes == 180


# ===========================================================================================
# Adding to a plan
# ===========================================================================================

def _base_rules():
    return P.normalize_rules({"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"},
                              "blocks": [{"kind": "gym", "count": 2, "minutes": 60, "prefer": "evening"}],
                              "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}],
                              "buffer": 15})


def test_a_new_kind_is_appended_and_only_it_is_placed_besides_work():
    merged, extra = P.merge_rules(_base_rules(), {"blocks": [{"kind": "reading", "count": 3, "minutes": 30}]})
    assert [b.kind for b in merged.blocks] == ["gym", "reading"]
    assert [b.kind for b in extra.blocks] == ["reading"] and extra.blocks[0].count == 3
    assert extra.has_work and extra.work_start == "09:00" and extra.buffer == 15
    assert extra.keep_free == merged.keep_free, "every keep-free window still applies to the new blocks"


def test_a_kind_already_in_the_plan_means_more_of_it():
    merged, extra = P.merge_rules(_base_rules(), {"blocks": [{"kind": "gym", "count": 1}]})
    gym = next(b for b in merged.blocks if b.kind == "gym")
    assert gym.count == 3 and gym.minutes == 60 and gym.prefer == "evening"
    (more,) = extra.blocks
    assert (more.kind, more.count, more.minutes, more.prefer) == ("gym", 1, 60, "evening")


def test_more_of_a_kind_may_change_its_other_fields_for_the_new_blocks():
    merged, extra = P.merge_rules(_base_rules(), {"blocks": [{"kind": "gym", "count": 2, "minutes": 30,
                                                              "prefer": "morning"}]})
    (more,) = extra.blocks
    assert (more.count, more.minutes, more.prefer) == (2, 30, "morning")
    gym = next(b for b in merged.blocks if b.kind == "gym")
    assert gym.count == 4 and gym.minutes == 30


def test_new_keep_free_windows_are_added_to_the_old_ones():
    merged, extra = P.merge_rules(_base_rules(), {"keep_free": [{"days": "fri", "from": "15:00", "to": "17:00"}],
                                                  "blocks": [{"kind": "reading", "count": 1, "minutes": 30}]})
    assert len(merged.keep_free) == 2 and len(extra.keep_free) == 2


def test_new_work_hours_and_limits_replace_the_old_ones():
    merged, extra = P.merge_rules(_base_rules(), {"work": {"days": "mon-thu", "start": "08:00", "end": "16:00"},
                                                  "not_after": "20:00", "buffer": 10,
                                                  "blocks": [{"kind": "reading", "count": 1, "minutes": 30}]})
    assert (merged.work_days, merged.work_start, merged.work_end) == ((0, 1, 2, 3), "08:00", "16:00")
    assert (merged.not_after, merged.buffer) == ("20:00", 10) and extra.not_after == "20:00"


def test_rules_that_only_change_a_limit_have_nothing_new_to_place():
    plain = P.normalize_rules({"blocks": [{"kind": "gym", "count": 2, "minutes": 60}]})
    merged, extra = P.merge_rules(plain, {"keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}]})
    assert extra is None and len(merged.keep_free) == 1
    merged2, extra2 = P.merge_rules(_base_rules(), {"buffer": 5})
    assert extra2 is not None and extra2.blocks == () and extra2.has_work, "work is refilled"


def test_merging_leaves_the_old_rules_untouched_and_round_trips():
    old = _base_rules()
    before = old.as_dict()
    merged, _ = P.merge_rules(old, {"blocks": [{"kind": "gym", "count": 1}]})
    assert old.as_dict() == before
    assert P.normalize_rules(merged.as_dict()) == merged


@pytest.mark.parametrize("raw,fragment", [
    ({"colour": "blue"}, "unknown field"),
    ({"blocks": "gym"}, "must be a list"),
    ({"blocks": ["gym"]}, "must be an object"),
    ({"blocks": [{"kind": "gym", "count": 13}]}, "between 1 and 14"),
    ({"keep_free": "wed"}, "must be a list"),
    ("not json", "not valid JSON"),
    ([], "must be an object"),
])
def test_rules_to_add_are_checked_as_strictly_as_new_rules(raw, fragment):
    with pytest.raises(RulesError, match=re.escape(fragment)):
        P.merge_rules(_base_rules(), raw)


def test_the_extra_blocks_fit_around_what_already_stands():
    """The point of `add`: new blocks go around the existing ones, which do not move."""
    merged, extra = P.merge_rules(_base_rules(), {"blocks": [{"kind": "reading", "count": 3, "minutes": 30,
                                                              "prefer": "evening"}]})
    first = P.plan_week(WEEK, _base_rules(), [])
    standing = [b for b in first.blocks if b.kind != "work"]
    more = P.plan_week(WEEK, extra, [], taken=standing)
    reading = [b for b in more.blocks if b.kind == "reading"]
    assert len(reading) == 3
    for b in reading:
        for s in standing:
            assert b.end <= s.start or s.end <= b.start


# ===========================================================================================
# gaps that mutation testing found
# ===========================================================================================

def test_find_slot_uses_the_kinds_own_preferred_part_of_the_day_when_none_is_asked():
    r = P.normalize_rules(GYM4)                                  # gym prefers the morning
    b = P.find_slot("gym", 60, day(1), r, [], [])
    assert 6 <= local(b.start).hour < 12
    evening = P.find_slot("gym", 60, day(1), r, [], [], prefer="evening")
    assert local(evening.start).hour >= 17, "what he asks for wins over the kind's habit"


def test_editing_only_the_start_or_the_day_keeps_the_blocks_length():
    ninety = Block("gym", at(day(0), "18:00"), at(day(0), "19:30"))
    moved = P.edited(ninety, start="19:00")
    assert (moved.start, moved.end) == (at(day(0), "19:00"), at(day(0), "20:30"))
    other_day = P.edited(ninety, day=day(2))
    assert P.minutes_between(other_day.start, other_day.end) == 90 and local(other_day.start).hour == 18

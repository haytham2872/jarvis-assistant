"""Skills, slice 5 (4 October 2026): the overnight pass.

Real SQLite records (conftest gives each test its own database and nightly state file);
the cheap-tier model is faked and its prompts inspected.
"""
import asyncio
import datetime
import inspect
import json

import pytest

from backend.brain import research, skill_nightly as N, skills
from backend.brain.test_skill_teaching import seed_run
from backend.storage import lifelog, operations
from backend.storage import skills as store
from backend.tools import policy, vault

LOCAL_TZ = lifelog.LOCAL_TZ
NOW = datetime.datetime(2026, 10, 5, 3, 30, tzinfo=LOCAL_TZ)            # Monday, 03:30
DINNER_OPS = [("web_browse", "ok", {"task": "restaurants"}, "Da Vito"),
              ("calendar_action", "ok", {"action": "list_events"}, "free")]
DRAFT = {"name": "find-restaurant-and-check-calendar",
         "description": "Find a restaurant for a meal he names and check his calendar is free.",
         "when_to_use": "He asks for a place to eat at a time.",
         "steps": ["Search with web_browse", "Check the calendar with calendar_action", "Give three options"],
         "pitfalls": [], "checks": ["Three options"], "tools": ["web_browse", "calendar_action"]}


def run(coro):
    return asyncio.run(coro)


def at(day, hour, minute=0):
    return datetime.datetime(2026, 9, day, hour, minute, tzinfo=LOCAL_TZ).timestamp()


@pytest.fixture(autouse=True)
def _vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "skills").mkdir()
    operations.current_run_id.set(None)


@pytest.fixture
def model(monkeypatch):
    seen = {"prompts": [], "reply": json.dumps(DRAFT)}

    async def fake(prompt, max_tokens=0, images=None):
        seen["prompts"].append(prompt)
        return seen["reply"]
    monkeypatch.setattr(research, "_llm", fake)
    return seen


def seed_dinners(days=(22, 24, 29), hour=19):
    for i, d in enumerate(days):
        seed_run(f"d{i}", f"find me a good restaurant near Bastille for dinner on {d}",
                 DINNER_OPS, at=at(d, hour, 5 * i))


def save(**kw):
    token = policy.current_caller.set(policy.LIVE)
    try:
        return run(skills.skill("save", **kw))
    finally:
        policy.current_caller.reset(token)


# ------------------------------------------------------------------ finding repeats
def test_three_requests_on_different_days_with_the_same_tools_are_one_repeat():
    seed_dinners()
    seed_run("other", "what's NVDA trading at", [("web_browse", "ok", {"task": "NVDA"}, "$190"),
                                                  ("fetch_page", "ok", {"url": "x"}, "y")], at=at(23, 9))
    [c] = N.find_repeats(NOW.timestamp())
    assert len(c["runs"]) == 3 and c["days"] == ["2026-09-22", "2026-09-24", "2026-09-29"]
    assert c["tools"] == ["calendar_action", "web_browse"] and "restaurant" in c["words"]
    assert c["covered_by"] == ""


@pytest.mark.parametrize("days", [(22, 22, 22), (22, 24)])
def test_not_enough_repeats_or_days_is_not_a_repeat(days):
    seed_dinners(days)
    assert N.find_repeats(NOW.timestamp()) == []


def test_a_request_that_already_followed_a_skill_is_not_a_repeat():
    seed_dinners()
    operations.record_operation(tool="skill", outcome="ok", started_at=at(29, 19, 30),
                                args={"action": "view", "name": "x"}, run_id="d2")
    assert N.find_repeats(NOW.timestamp()) == []


def test_a_repeat_a_skill_already_covers_is_marked():
    seed_dinners()
    save(name="restaurant-finder", description="Find a good restaurant near a place for dinner.",
         steps=["Search", "Check the calendar"])
    [c] = N.find_repeats(NOW.timestamp())
    assert c["covered_by"] == "restaurant-finder"


# ------------------------------------------------------------------ the routine offered
def test_the_same_weekday_and_time_offers_a_weekly_routine(monkeypatch):
    monkeypatch.setattr(lifelog, "stamp", lambda ts=None: (NOW.timestamp(), "2026-10-05"))
    runs = [{"started": datetime.datetime(2026, 9, d, 9, m, tzinfo=LOCAL_TZ).timestamp()}
            for d, m in ((14, 2), (21, 10), (28, 55))]                # three Mondays ~9
    r = N._routine_for(runs)
    assert r == {"when": "2026-10-12 09:00", "repeat": "weekly", "words": "every Monday at 09:00"}


def test_weekdays_only_offers_a_weekday_routine_and_scattered_times_offer_none(monkeypatch):
    monkeypatch.setattr(lifelog, "stamp", lambda ts=None: (NOW.timestamp(), "2026-10-05"))
    weekdays = [{"started": datetime.datetime(2026, 9, d, 18, 0, tzinfo=LOCAL_TZ).timestamp()} for d in (21, 23, 25)]
    assert N._routine_for(weekdays)["repeat"] == "weekdays"
    scattered = [{"started": datetime.datetime(2026, 9, d, h, 0, tzinfo=LOCAL_TZ).timestamp()}
                 for d, h in ((21, 8), (23, 13), (25, 21))]
    assert N._routine_for(scattered) is None
    weekend = [{"started": datetime.datetime(2026, 9, d, 10, 0, tzinfo=LOCAL_TZ).timestamp()} for d in (19, 21, 23)]
    assert N._routine_for(weekend) is None


# ------------------------------------------------------------------ the pass
def test_a_repeat_becomes_a_skill_and_is_reported(model):
    seed_dinners()
    lines = run(N.run_pass(NOW))
    s = skills.load("find-restaurant-and-check-calendar")
    assert s is not None and s.active and s.source == "learned"
    assert lines[0].startswith("learned find-restaurant-and-check-calendar (you asked for it 3 times")
    assert "has asked for this kind of task 3 times" in model["prompts"][0]
    assert N.brief_line(NOW).startswith("Skills overnight: learned find-restaurant-and-check-calendar")


def test_a_repeat_is_tried_once_and_one_skill_a_night_at_most(model):
    seed_dinners()
    run(N.run_pass(NOW))
    model["prompts"].clear()
    run(N.run_pass(NOW + datetime.timedelta(days=1)))
    assert not any("has asked for this kind" in p for p in model["prompts"])


def test_the_model_saying_these_are_different_saves_nothing(model):
    seed_dinners()
    model["reply"] = json.dumps({**DRAFT, "skip": True})         # a full draft, but "skip"
    assert run(N.run_pass(NOW)) == [] and skills.load_all() == []


def test_a_regular_repeat_queues_a_routine_offer_put_to_him_after_the_brief(model, monkeypatch):
    for i, d in enumerate((14, 21, 28)):                               # three Mondays, ~19:00
        seed_run(f"m{i}", f"find me a good restaurant near Bastille for dinner tonight {i}",
                 DINNER_OPS, at=datetime.datetime(2026, 9, d, 19, 0, tzinfo=LOCAL_TZ).timestamp())
    lines = run(N.run_pass(NOW))
    assert "I'll ask whether to run it every Monday at 19:00" in lines[0]
    asked = []

    async def request(**kw):
        asked.append(kw)
        return {"id": 7}
    from backend.tools import approval_flow
    monkeypatch.setattr(approval_flow, "request", request)
    assert run(N.offer_pending()) == 1
    [kw] = asked
    assert kw["tool"] == "schedule_task" and kw["args"]["skill"] == "find-restaurant-and-check-calendar"
    assert kw["args"]["repeat"] == "weekly" and kw["args"]["when"].endswith("19:00")
    assert run(N.offer_pending()) == 0                                 # once


def test_the_brief_carries_the_line_and_offers_follow_its_delivery():
    from backend.brain import chronos
    src = inspect.getsource(chronos.compose_brief)
    assert "_collect_skills_overnight(now)" in src
    send = inspect.getsource(chronos._send_brief)
    assert send.index("_mark_briefed(now, covered") < send.index("await _offer_skill_routines()")


def test_yesterdays_report_is_not_todays_line(model):
    seed_dinners()
    run(N.run_pass(NOW))
    assert N.brief_line(NOW + datetime.timedelta(days=1)) == ""


# ------------------------------------------------------------------ merges
def test_two_skills_doing_the_same_job_are_merged_into_the_more_used(model):
    save(name="weekly-email", description="Write and send his weekly summary email to the team.",
         steps=["Read the calendar", "Draft"], tools=["calendar_action", "gmail_action"])
    save(name="weekly-summary", description="Write his weekly summary email for the team.",
         steps=["Read the week", "Draft it"], tools=["calendar_action", "gmail_action"])
    store.record_use("weekly-summary", "model")
    model["reply"] = json.dumps({"same": True, "description": "Write his weekly summary email for the team.",
                                 "when_to_use": "End of the week.", "steps": ["Read the calendar", "Draft it"],
                                 "pitfalls": [], "checks": [], "tools": ["calendar_action", "gmail_action"]})
    lines = run(N.run_pass(NOW))
    assert "merged weekly-email into weekly-summary" in lines
    assert skills.load("weekly-email").status == "retired" and skills.load("weekly-summary").active


def test_skills_the_model_calls_different_are_left_alone(model):
    save(name="weekly-email", description="Write and send his weekly summary email to the team.",
         steps=["Draft"], tools=["calendar_action", "gmail_action"])
    save(name="weekly-summary", description="Write his weekly summary email for the team.",
         steps=["Draft"], tools=["calendar_action", "gmail_action"])
    model["reply"] = json.dumps({"same": False, "description": "x", "steps": ["a", "b"],
                                 "tools": ["calendar_action", "gmail_action"]})
    assert run(N.run_pass(NOW)) == []
    assert all(s.active for s in skills.load_all())


def test_unrelated_skills_are_not_merge_candidates():
    save(name="meeting-prep", description="Prepare him for a meeting.", steps=["x"], tools=["calendar_action"])
    save(name="stock-research", description="Research a stock.", steps=["x"], tools=["web_browse", "portfolio"])
    assert N.merge_candidates(skills.active_skills()) == []


# ------------------------------------------------------------------ folds
def _followed(run_id, name, ts):
    operations.record_operation(tool="skill", outcome="ok", started_at=ts,
                                args={"action": "view", "name": name}, run_id=run_id)


def test_a_missed_correction_is_folded_into_the_skill(model):
    save(name="meeting-prep", description="Prepare him for a meeting.",
         steps=["Find the meeting", "Read the profile", "Give three points"], tools=["calendar_action"])
    t = NOW.timestamp() - 3 * 3600
    seed_run("p1", "prep me for Marc", [("calendar_action", "ok", {}, "x")], at=t)
    _followed("p1", "meeting-prep", t + 2)
    seed_run("p2", "next time skip the profile, it's always empty", [], at=t + 300)
    model["reply"] = json.dumps({"edits": [{"op": "remove", "section": "procedure", "index": 2}]})
    lines = run(N.run_pass(NOW))
    assert any(l.startswith("changed meeting-prep from what you said") for l in lines)
    assert "Read the profile" not in skills._section(skills.load("meeting-prep").body, ("procedure",))
    assert "it's always empty" in skills.load("meeting-prep").body


def test_a_correction_already_handled_or_not_a_correction_is_not_folded(model):
    save(name="meeting-prep", description="Prepare him for a meeting.", steps=["a", "b"], tools=["calendar_action"])
    t = NOW.timestamp() - 3 * 3600
    seed_run("p1", "prep me for Marc", [("calendar_action", "ok", {}, "x")], at=t)
    _followed("p1", "meeting-prep", t + 2)
    seed_run("p2", "thanks, that's useful", [], at=t + 300)
    assert N.fold_candidates(NOW.timestamp()) == []
    seed_run("p3", "prep me for Sarah", [("calendar_action", "ok", {}, "x")], at=t + 600)
    _followed("p3", "meeting-prep", t + 602)
    seed_run("p4", "next time skip step two", [], at=t + 900)
    operations.record_operation(tool="skill", outcome="ok", started_at=t + 901,
                                args={"action": "patch", "name": "meeting-prep"}, run_id="p4")
    assert N.fold_candidates(NOW.timestamp()) == []


# ------------------------------------------------------------------ retires
def test_a_skill_unused_for_90_days_is_retired_but_the_clock_starts_when_first_seen(model):
    save(name="old-one", description="Something he no longer asks for.", steps=["x"])
    run(N.run_pass(NOW))
    assert skills.load("old-one").active                          # first seen today
    # Saving stamps the skill with the real date, so count from whichever is later: NOW or the
    # real date (from NOW alone the test stopped passing on 7 October, two days after NOW).
    later = max(NOW, datetime.datetime.now(LOCAL_TZ)) + datetime.timedelta(days=91)
    lines = run(N.run_pass(later))
    assert "retired old-one (not opened in 90 days)" in lines and not skills.load("old-one").active


def test_a_skill_opened_recently_stays(model, monkeypatch):
    save(name="kept", description="Something he still asks for.", steps=["x"])
    run(N.run_pass(NOW))
    later = NOW + datetime.timedelta(days=91)
    monkeypatch.setattr(store, "use_stats", lambda: {"kept": {"uses": 1, "last": later.timestamp() - 86_400}})
    run(N.run_pass(later))
    assert skills.load("kept").active


# ------------------------------------------------------------------ the loop
def test_the_pass_runs_once_a_night_in_its_window_and_not_while_he_talks(model, monkeypatch):
    from backend.brain import chronos
    calls = []

    async def fake_pass(now):
        calls.append(now)
        return []
    monkeypatch.setattr(N, "run_pass", fake_pass)
    clock = {"t": NOW.replace(hour=1)}
    monkeypatch.setattr(chronos, "now_local", lambda: clock["t"])
    run(N._cycle())
    assert calls == []
    clock["t"] = NOW
    N.set_busy_check(lambda: True)
    try:
        run(N._cycle())
        assert calls == []
    finally:
        N.set_busy_check(None)
    run(N._cycle())
    run(N._cycle())
    assert len(calls) == 1


def test_it_is_wired_paused_by_pause_and_held_while_a_turn_runs():
    from backend import main
    src = inspect.getsource(main)
    assert '"skill_nightly": skill_nightly.run_loop' in src
    assert "session_close, skill_nightly]" in src
    assert 'pausable="work"' in inspect.getsource(N.run_loop)


# ------------------------------------------------------------------ the Sunday line
def test_the_week_line_counts_uses_corrections_and_new_skills():
    monday = datetime.date(2026, 9, 28)
    assert N.week_line(monday) == ""
    save(name="meeting-prep", description="Prepare him for a meeting.", steps=["a", "b"])
    store.record_use("meeting-prep", "model")
    store.record_use("meeting-prep", "routine")
    token = policy.current_caller.set(policy.LIVE)
    run(skills.skill("patch", name="meeting-prep", edits=[{"op": "remove", "section": "procedure", "index": 1}],
                     reason="skip that"))
    policy.current_caller.reset(token)
    today = datetime.datetime.fromtimestamp(lifelog.stamp()[0], tz=LOCAL_TZ).date()
    line = N.week_line(today - datetime.timedelta(days=today.weekday()))
    assert line == "Skills this week: used meeting-prep 2x; corrected meeting-prep; new meeting-prep."


def test_the_sunday_review_includes_it():
    from backend.brain import weeklyreview
    assert "skill_nightly.week_line(monday)" in inspect.getsource(weeklyreview.compose)



def test_slash_skill_runs_and_unrelated_requests_are_not_repeats():
    for i, d in enumerate((22, 24, 29)):
        seed_run(f"s{i}", f"/skill find-restaurant Bastille {d}", DINNER_OPS, at=at(d, 19))
    for i, (d, text) in enumerate(((22, "what's NVDA trading at"), (24, "weather in Lyon tomorrow"),
                                   (29, "latest news on the ECB decision"))):
        seed_run(f"u{i}", text, DINNER_OPS, at=at(d, 9))
    assert N.find_repeats(NOW.timestamp()) == []


def test_a_correction_long_after_the_reply_is_not_folded():
    save(name="meeting-prep", description="Prepare him for a meeting.", steps=["a", "b"], tools=["calendar_action"])
    t = NOW.timestamp() - 5 * 3600
    seed_run("p1", "prep me for Marc", [("calendar_action", "ok", {}, "x")], at=t)
    _followed("p1", "meeting-prep", t + 2)
    seed_run("p2", "next time skip step two", [], at=t + 45 * 60)
    assert N.fold_candidates(NOW.timestamp()) == []

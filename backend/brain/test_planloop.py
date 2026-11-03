"""P4: the daily loop (backend/brain/planloop.py) -- when the check-in goes out, when a finished day is
closed, when the calendar is looked at, when the weekly review is sent, and what a pause holds."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.brain import planday as D
from backend.brain import planloop as L
from backend.brain import weekplan as W
from backend.storage import approvals, checkins, pauses, plans
from backend.tools import approval_flow
from backend.tools import calendar as cal
from backend.tools.calendar_fake import FakeCalendarService

LOCAL_TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
RULES = {"work": {"days": "mon-fri", "start": "09:00", "end": "17:00"},
         "keep_free": [{"days": "wed", "from": "18:00", "to": "23:00"}], "buffer": 15,
         "blocks": [{"kind": "gym", "count": 3, "minutes": 60, "not_before": "17:00", "not_after": "20:00"}]}


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


def utc(dt):
    return dt.astimezone(timezone.utc)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def svc(monkeypatch):
    fake = FakeCalendarService()
    monkeypatch.setattr(cal, "_get_service", lambda: fake)
    monkeypatch.setattr(L, "_calendar_ready", lambda: True)
    return fake


@pytest.fixture
def sent():
    out = []

    async def notifier(text):
        out.append(text)
        return True
    L.set_notifier(notifier)
    L._unsent_review.clear()
    yield out
    L.set_notifier(None)
    L._unsent_review.clear()


def planned(svc):
    svc.seed("Standup", paris(2026, 10, 5, 10), paris(2026, 10, 5, 10, 30))
    svc.seed("Dentist", paris(2026, 10, 7, 15), paris(2026, 10, 7, 16))
    svc.seed("Offsite", paris(2026, 10, 8, 9), paris(2026, 10, 8, 17))
    assert W.run(W.Ask(action="propose", rules=RULES), NOW).startswith("Proposed")
    assert W.run(W.Ask(action="apply"), NOW).startswith("Applied")


EVE = paris(2026, 10, 9, 21, 30)


# ===========================================================================================
# the evening check-in
# ===========================================================================================

def test_the_check_in_goes_out_at_its_time_once_and_is_recorded(svc, sent):
    planned(svc)
    run(L._maybe_checkin(utc(paris(2026, 10, 9, 21, 29))))
    assert sent == [] and checkins.get("2026-10-09") is None
    run(L._maybe_checkin(utc(EVE)))
    assert len(sent) == 1 and sent[0].startswith("Evening check-in, Friday 9 Oct.")
    row = checkins.get("2026-10-09")
    assert row["sent_at"] and len(row["block_uids"]) == 2
    run(L._maybe_checkin(utc(EVE + timedelta(minutes=1))))
    run(L._maybe_checkin(utc(EVE + timedelta(hours=1))))
    assert len(sent) == 1, "once a day"


def test_a_day_with_no_plan_or_no_blocks_gets_no_check_in(svc, sent):
    run(L._maybe_checkin(utc(EVE)))                                   # no plan at all
    planned(svc)
    run(L._maybe_checkin(utc(paris(2026, 10, 10, 21, 30))))           # Saturday
    assert sent == [] and checkins.recent() == []


def test_the_check_in_can_be_switched_off_or_moved(svc, sent):
    planned(svc)
    plans.set_setting("checkin_enabled", False)
    run(L._maybe_checkin(utc(EVE)))
    assert sent == []
    plans.set_setting("checkin_enabled", True)
    plans.set_setting("checkin_time", "22:00")
    run(L._maybe_checkin(utc(EVE)))
    assert sent == []
    run(L._maybe_checkin(utc(paris(2026, 10, 9, 22, 0))))
    assert len(sent) == 1


def test_a_check_in_that_could_not_be_delivered_is_retried_and_sent_once(svc, monkeypatch):
    planned(svc)
    out = []
    outcomes = iter([False, False, True])

    async def flaky(text):
        ok = next(outcomes)
        if ok:
            out.append(text)
        return ok
    L.set_notifier(flaky)
    try:
        run(L._maybe_checkin(utc(EVE)))
        row = checkins.get("2026-10-09")
        assert row is not None and row["sent_at"] is None, "claimed, not sent"
        run(L._maybe_checkin(utc(EVE + timedelta(minutes=1))))
        assert checkins.get("2026-10-09")["sent_at"] is None
        run(L._maybe_checkin(utc(EVE + timedelta(minutes=2))))
        assert checkins.get("2026-10-09")["sent_at"] and len(out) == 1
        run(L._maybe_checkin(utc(EVE + timedelta(minutes=3))))
        assert len(out) == 1
    finally:
        L.set_notifier(None)


def test_a_notifier_that_raises_never_breaks_the_loop(svc):
    planned(svc)

    async def broken(text):
        raise RuntimeError("discord is down")
    L.set_notifier(broken)
    try:
        run(L._maybe_checkin(utc(EVE)))
        assert checkins.get("2026-10-09")["sent_at"] is None
    finally:
        L.set_notifier(None)


def test_no_notifier_means_nothing_is_marked_sent(svc):
    planned(svc)
    L.set_notifier(None)
    run(L._maybe_checkin(utc(EVE)))
    assert checkins.get("2026-10-09")["sent_at"] is None


def test_no_check_in_when_he_already_covered_every_block(svc, sent):
    planned(svc)
    W.run(W.Ask(action="report", day="2026-10-09", outcomes=[{"kind": "work", "outcome": "done"},
                                                              {"kind": "gym", "outcome": "done"}]), utc(EVE))
    run(L._maybe_checkin(utc(EVE)))
    assert sent == []


def test_pause_all_holds_the_check_in_and_says_so_once_a_plain_pause_does_not(svc, sent):
    planned(svc)
    pauses.start(pauses.WORK)
    run(L._cycle(utc(EVE)))
    assert len(sent) == 1, "a plain /pause leaves the check-in he asked for"
    pauses.end("resume")
    day2 = paris(2026, 10, 8, 21, 30)
    pauses.start(pauses.ALL)
    run(L._cycle(utc(day2)))
    run(L._cycle(utc(day2 + timedelta(minutes=1))))
    assert len(sent) == 1 and checkins.get("2026-10-08") is None
    row = pauses.active()
    skips = pauses.skips(row["id"])
    assert [s["kind"] for s in skips] == ["checkin_held"], "noted once, for /resume"
    pauses.end("resume")
    run(L._cycle(utc(day2 + timedelta(minutes=2))))
    assert len(sent) == 2, "after the pause the evening's check-in still goes out"


# ===========================================================================================
# closing a finished day
# ===========================================================================================

def test_a_finished_day_is_closed_at_four_and_unanswered_blocks_become_unknown(svc, sent):
    planned(svc)
    run(L._maybe_checkin(utc(EVE)))
    W.run(W.Ask(action="report", day="2026-10-09", outcomes=[{"kind": "work", "outcome": "done"}]), utc(EVE))
    run(L._close_days(utc(paris(2026, 10, 10, 3, 59))))
    assert checkins.get("2026-10-09")["closed_at"] is None
    run(L._close_days(utc(paris(2026, 10, 10, 4, 0))))
    assert checkins.get("2026-10-09")["closed_at"]
    told = plans.outcomes([r["uid"] for r in D.live_blocks_on(date(2026, 10, 9))])
    assert sorted(v["outcome"] for v in told.values()) == ["done", "unknown"]


def test_closing_is_never_held_by_a_pause(svc, sent):
    planned(svc)
    run(L._maybe_checkin(utc(EVE)))
    pauses.start(pauses.ALL)
    run(L._cycle(utc(paris(2026, 10, 10, 6, 0))))
    assert checkins.get("2026-10-09")["closed_at"]


# ===========================================================================================
# the 30-minute look
# ===========================================================================================

def _clash(svc):
    svc.seed("Board dinner", paris(2026, 10, 8, 17, 45), paris(2026, 10, 8, 19))


def test_the_look_asks_about_a_clash_and_waits_half_an_hour(svc, sent):
    planned(svc)
    _clash(svc)
    approval_asked = []

    async def notifier(row):
        approval_asked.append(row)
        return "m1"
    approval_flow.set_notifier(notifier)
    try:
        at = utc(paris(2026, 10, 2, 10, 0))
        run(L._maybe_scan(at))
        assert len(approval_asked) == 1 and approval_asked[0]["context"] == "planner"
        assert plans.get_setting("last_scan_at") == at.timestamp()
        svc.seed("Second clash", paris(2026, 10, 9, 17, 45), paris(2026, 10, 9, 19))
        run(L._maybe_scan(at + timedelta(minutes=29)))
        assert len(approval_asked) == 1, "not before 30 minutes have passed"
        run(L._maybe_scan(at + timedelta(minutes=30)))
        assert len(approval_asked) == 2
    finally:
        approval_flow.set_notifier(None)


def test_the_look_only_runs_between_seven_and_eleven(svc, sent):
    planned(svc)
    _clash(svc)
    for hour in (6, 23):
        run(L._maybe_scan(utc(paris(2026, 10, 2, hour, 30))))
    assert plans.get_setting("last_scan_at") is None
    run(L._maybe_scan(utc(paris(2026, 10, 2, 7, 0))))
    assert plans.get_setting("last_scan_at") is not None


def test_the_look_never_starts_the_google_consent_flow(svc, sent, monkeypatch):
    planned(svc)
    _clash(svc)
    monkeypatch.setattr(L, "_calendar_ready", lambda: False)
    monkeypatch.setattr(cal, "_get_service", lambda: (_ for _ in ()).throw(AssertionError("consent flow")))
    run(L._maybe_scan(utc(paris(2026, 10, 2, 10, 0))))
    assert plans.get_setting("last_scan_at") is None


def test_the_look_is_held_by_any_pause_and_a_failing_look_waits_a_full_interval(svc, sent, monkeypatch):
    planned(svc)
    _clash(svc)
    called = []
    monkeypatch.setattr(L.reshuffle, "scan", lambda now: called.append(now) or [])
    pauses.start(pauses.WORK)
    run(L._cycle(utc(paris(2026, 10, 2, 10, 0))))
    assert called == []
    pauses.end("resume")
    monkeypatch.setattr(L.reshuffle, "scan", lambda now: (_ for _ in ()).throw(RuntimeError("calendar down")))
    at = utc(paris(2026, 10, 2, 10, 0))
    run(L._cycle(at))
    assert plans.get_setting("last_scan_at") == at.timestamp(), "recorded before the look, so it is not retried every minute"


def test_a_step_that_fails_does_not_stop_the_others(svc, sent, monkeypatch):
    planned(svc)

    async def boom(now):
        raise RuntimeError("boom")
    monkeypatch.setattr(L, "_maybe_scan", boom)
    run(L._cycle(utc(EVE)))
    assert len(sent) == 1, "the check-in still went out"


# ===========================================================================================
# the weekly review
# ===========================================================================================

SUN = paris(2026, 10, 11, 19, 0)


def test_the_review_goes_out_on_sunday_evening_once_a_week(svc, sent):
    planned(svc)
    run(L._maybe_review(utc(paris(2026, 10, 11, 18, 59))))
    run(L._maybe_review(utc(paris(2026, 10, 10, 19, 0))))             # Saturday
    assert sent == []
    run(L._maybe_review(utc(SUN)))
    assert len(sent) == 1 and sent[0].startswith("Weekly review, week of 5 Oct.")
    assert plans.get_setting("last_review_week") == "2026-10-05"
    run(L._maybe_review(utc(SUN + timedelta(minutes=1))))
    assert len(sent) == 1
    assert plans.draft("2026-10-12") is not None, "and next week is drawn as a proposal"


def test_the_review_can_be_switched_off(svc, sent):
    planned(svc)
    plans.set_setting("review_enabled", False)
    run(L._maybe_review(utc(SUN)))
    assert sent == [] and plans.draft("2026-10-12") is None


def test_a_week_with_no_plan_gets_no_review_and_is_not_retried(svc, sent):
    run(L._maybe_review(utc(SUN)))
    assert sent == [] and plans.get_setting("last_review_week") == "2026-10-05"


def test_a_review_that_could_not_be_delivered_is_resent_not_redrawn(svc):
    planned(svc)
    results = iter([False, False, True])
    got = []

    async def flaky(text):
        ok = next(results)
        if ok:
            got.append(text)
        return ok
    L.set_notifier(flaky)
    L._unsent_review.clear()
    try:
        run(L._maybe_review(utc(SUN)))
        assert plans.get_setting("last_review_week") is None
        first_draft = plans.draft("2026-10-12")["id"]
        run(L._maybe_review(utc(SUN + timedelta(minutes=1))))
        assert plans.draft("2026-10-12")["id"] == first_draft, "the proposal is not drawn again on a retry"
        run(L._maybe_review(utc(SUN + timedelta(minutes=2))))
        assert len(got) == 1 and plans.get_setting("last_review_week") == "2026-10-05"
    finally:
        L.set_notifier(None)
        L._unsent_review.clear()


def test_a_review_that_fails_to_compose_is_not_retried_every_minute(svc, sent, monkeypatch):
    planned(svc)
    monkeypatch.setattr(L.weeklyreview, "compose", lambda now, ready: (_ for _ in ()).throw(RuntimeError("x")))
    run(L._maybe_review(utc(SUN)))
    assert plans.get_setting("last_review_week") == "2026-10-05" and sent == []


def test_a_pause_holds_the_review_and_says_so_once(svc, sent):
    planned(svc)
    pauses.start(pauses.WORK)
    run(L._cycle(utc(SUN)))
    run(L._cycle(utc(SUN + timedelta(minutes=1))))
    assert sent == []
    assert [s["kind"] for s in pauses.skips(pauses.active()["id"])] == ["review_held"]
    pauses.end("resume")
    run(L._cycle(utc(SUN + timedelta(minutes=2))))
    assert len(sent) == 1, "the review goes out once the pause is over (still Sunday evening)"


# ===========================================================================================
# wiring
# ===========================================================================================

def test_the_loop_is_registered_paced_and_wired_in_main():
    import ast
    from pathlib import Path
    src = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    assert '"planloop":   planloop.run_loop' in src and "planloop," in src
    assert "plan_handler        = planday.handle_plan_command" in src
    body = (Path(__file__).with_name("planloop.py")).read_text(encoding="utf-8")
    tree = ast.parse(body)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run_periodic"]
    assert len(calls) == 1
    assert "pause.is_paused(\"work\")" in body and "pause.is_paused(\"all\")" in body


def test_the_loop_never_reaches_the_calendar_through_the_consent_flow():
    from pathlib import Path
    body = Path(__file__).with_name("planloop.py").read_text(encoding="utf-8")
    assert "_calendar_ready()" in body and "TOKEN_PATH" in body


def test_run_loop_logs_and_starts_the_pacer(monkeypatch):
    started = []

    async def fake(name, check_interval_s, on_cycle, **kw):
        started.append((name, check_interval_s, on_cycle))
    monkeypatch.setattr(L.pacing, "run_periodic", fake)
    run(L.run_loop())
    assert started == [("planloop", 60.0, L._cycle)]


# ===========================================================================================
# gaps that mutation testing found
# ===========================================================================================

def test_a_check_in_someone_else_claimed_a_moment_ago_is_not_sent_again(svc, sent, monkeypatch):
    planned(svc)
    monkeypatch.setattr(L.checkins, "claim", lambda *a, **k: False)
    run(L._maybe_checkin(utc(EVE)))
    assert sent == []


def test_the_resent_review_is_the_one_that_was_composed_not_a_new_one(svc):
    planned(svc)
    results = iter([False, True])
    got = []

    async def flaky(text):
        ok = next(results)
        if ok:
            got.append(text)
        return ok
    L.set_notifier(flaky)
    L._unsent_review.clear()
    try:
        run(L._maybe_review(utc(SUN)))
        run(L._maybe_review(utc(SUN + timedelta(minutes=1))))
        (text,) = got
        assert "Next week drawn from the same rules (proposal #" in text, "the composed text, not a second look"
        assert "already has a proposal" not in text
    finally:
        L.set_notifier(None)
        L._unsent_review.clear()

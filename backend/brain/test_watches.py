"""Watches: event-triggered checks that message Sam only when evidence shows it.

The evidence tools are faked at the dispatch table, so every check still goes through
the real tool gateway (validation, outcome classification, receipts) and the real
SQLite store (conftest gives each test its own database).
"""
import asyncio
import datetime
import inspect
import json

import pytest

from backend.brain import watches as w
from backend.storage import watches as store


def run(coro):
    return asyncio.run(coro)


class Env:
    def __init__(self):
        self.evidence = "NVDA closed at $176.40 today, below the $180 mark."
        self.verdict = {"met": True, "quote": "NVDA closed at $176.40",
                        "confidence": 0.9, "summary": "NVDA closed below $180."}
        self.judge_calls = 0
        self.tool_calls: list[tuple[str, dict]] = []
        self.sent: list[str] = []
        self.dm_ok = True


@pytest.fixture
def env(monkeypatch):
    e = Env()

    async def web_browse(task):
        e.tool_calls.append(("web_browse", {"task": task}))
        return e.evidence

    async def gmail_action(action, query="", max_results=10, **_):
        e.tool_calls.append(("gmail_action", {"action": action, "query": query}))
        return e.evidence

    async def calendar_action(action, days=1, max_results=20, **_):
        e.tool_calls.append(("calendar_action", {"action": action}))
        return e.evidence

    async def judge(watch, evidence):
        e.judge_calls += 1
        return json.dumps(e.verdict)

    async def notifier(text):
        if not e.dm_ok:
            return False
        e.sent.append(text)
        return True

    monkeypatch.setattr(w, "_dispatch", lambda: {
        "web_browse": web_browse, "gmail_action": gmail_action,
        "calendar_action": calendar_action})
    monkeypatch.setattr(w, "_judge", judge)
    monkeypatch.setattr(w, "_notifier", notifier)
    monkeypatch.setattr(w, "_busy_check", None)
    return e


def add(**kw):
    args = dict(action="add", subject="NVDA daily close", condition="closes below $180",
                query="NVDA stock price today")
    args.update(kw)
    return run(w.watch_action(**args))


def only_watch():
    active = store.list_active()
    assert len(active) == 1
    return active[0]


def check(now=None):
    return run(w.evaluate(store.get(only_watch()["watch_id"]), now))


# --------------------------------------------------------------------- the tool
def test_add_confirms_only_after_the_watch_is_stored(env):
    out = add()
    assert out.startswith("Confirmed"), out
    saved = only_watch()
    assert saved["watch_id"] in out
    assert saved["method"] == "web" and saved["every_s"] == 6 * 3600


@pytest.mark.parametrize("kw, needle", [
    (dict(method="twitter"), "method must be"),
    (dict(every="minute"), "every must be"),
    (dict(until="2020-01-01"), "in the past"),
    (dict(until="not a date"), "YYYY-MM-DD"),
    (dict(subject=""), "needs subject"),
])
def test_add_rejects_bad_arguments_without_storing(env, kw, needle):
    out = add(**kw)
    assert out.startswith("Error") and needle in out
    assert store.list_active() == []


def test_add_refuses_a_watch_longer_than_the_maximum(env):
    far = (datetime.date.today() + datetime.timedelta(days=w._MAX_DAYS + 5)).isoformat()
    assert "at most" in add(until=far)


def test_active_watches_are_capped(env, monkeypatch):
    monkeypatch.setattr(w, "_MAX_ACTIVE", 2)
    add(); add()
    assert "already 2 active" in add()


def test_add_reports_failure_when_storage_is_unavailable(env, monkeypatch):
    monkeypatch.setattr(store, "_conn", lambda: None)
    assert add().startswith("Error")


def test_list_and_cancel(env):
    add()
    wid = only_watch()["watch_id"]
    assert wid in run(w.watch_action("list"))
    assert "Cancelled" in run(w.watch_action("cancel", watch_id=wid))
    assert store.list_active() == []
    assert run(w.watch_action("cancel", watch_id=wid)).startswith("Error")


# ------------------------------------------------------------------ evaluation
def test_a_met_condition_fires_once_with_its_quote(env):
    add()
    wid = only_watch()["watch_id"]
    assert check() == "fired"
    assert len(env.sent) == 1
    assert '"NVDA closed at $176.40"' in env.sent[0]
    row = store.get(wid)
    assert row["state"] == store.FIRED and row["alert_state"] == "sent"
    assert store.list_active() == []       # fire-once: nothing is due any more


def test_a_failed_search_is_never_evidence(env):
    """The A06 rule: a SEARCH FAILED string must not reach the judge."""
    add()
    env.evidence = "SEARCH FAILED — no live results"
    assert check() == "failed"
    assert env.judge_calls == 0 and env.sent == []
    assert only_watch()["evidence_hash"] is None


def test_unchanged_evidence_costs_no_model_call(env):
    add()
    env.verdict = {"met": False, "quote": "", "confidence": 0.9, "summary": ""}
    assert check() == "not_met"
    assert check() == "unchanged"
    assert env.judge_calls == 1


def test_relative_ages_do_not_count_as_a_change(env):
    add()
    env.verdict = {"met": False, "quote": "", "confidence": 0.9, "summary": ""}
    env.evidence = "Fed holds rates. 3 hours ago"
    check()
    env.evidence = "Fed holds rates. 5 hours ago"
    assert check() == "unchanged"


def test_an_invented_quote_does_not_fire(env):
    """No quote, no fire -- and the quote must really be in the evidence."""
    add()
    env.verdict = {"met": True, "quote": "NVDA crashed to $150 on earnings",
                   "confidence": 0.95, "summary": "It crashed."}
    assert check() == "not_met"
    assert env.sent == []


def test_low_confidence_rechecks_instead_of_pinging(env):
    add()
    env.verdict["confidence"] = 0.4
    assert check() == "not_met"
    assert env.sent == [] and len(store.list_active()) == 1


def test_a_malformed_judge_answer_is_not_met(env, monkeypatch):
    async def junk(watch, evidence):
        return "I think it probably happened, yes."
    monkeypatch.setattr(w, "_judge", junk)
    add()
    assert check() == "not_met"


def test_daily_judging_is_capped(env, monkeypatch):
    monkeypatch.setattr(w, "_MAX_JUDGED_PER_DAY", 2)
    add()
    env.verdict = {"met": False, "quote": "", "confidence": 0.9, "summary": ""}
    for i in range(2):
        env.evidence = f"different evidence {i}"
        assert check() == "not_met"
    env.evidence = "different evidence 3"
    assert check() == "capped"
    assert env.judge_calls == 2


def test_a_repeating_watch_fires_on_the_transition_not_every_check(env):
    add(repeat=True)
    assert check() == "fired"
    env.evidence = "NVDA closed at $176.40 again, still below $180."
    assert check() == "not_met"            # still met: no second DM
    env.evidence = "NVDA closed at $185 today."
    env.verdict = {"met": False, "quote": "", "confidence": 0.9, "summary": ""}
    assert check() == "not_met"
    env.evidence = "NVDA closed at $176.40, back below $180."
    env.verdict = {"met": True, "quote": "NVDA closed at $176.40",
                   "confidence": 0.9, "summary": "Below again."}
    assert check() == "fired"
    assert len(env.sent) == 2


# --------------------------------------------------------------- delivery
def test_a_failed_dm_is_mentioned_on_the_next_turn_exactly_once(env):
    env.dm_ok = False
    add()
    assert check() == "fired"
    first = w.pop_pending_alerts()
    assert "NVDA closed at $176.40" in first
    assert w.pop_pending_alerts() == ""


def test_a_firing_interrupted_mid_send_is_parked_never_resent(env):
    """The firing is recorded before the DM. A process that died in between leaves it
    `sending`; restart turns that into an uncertain mention, not a second DM."""
    add(repeat=True)
    wid = only_watch()["watch_id"]
    assert store.begin_alert(wid, "Watch: test alert", next_state=store.ACTIVE,
                             next_check_at=0, verdict="met: x")
    assert store.due(10**12, 10) == []     # a mid-send watch is never picked up again
    w.reconcile_on_startup()
    mention = w.pop_pending_alerts()
    assert "Watch: test alert" in mention and "may already have reached him" in mention
    assert env.sent == []


def test_begin_alert_is_won_by_exactly_one_caller(env):
    add()
    wid = only_watch()["watch_id"]
    assert store.begin_alert(wid, "a", next_state=store.FIRED, next_check_at=0, verdict="met")
    assert not store.begin_alert(wid, "b", next_state=store.FIRED, next_check_at=0, verdict="met")


# --------------------------------------------------------------- safety
def test_evidence_calls_are_fixed_and_read_only(env):
    """Code picks the call; gmail can only ever search, never send."""
    add(method="gmail", query="from:sarah@example.com newer_than:7d")
    check()
    assert env.tool_calls == [("gmail_action", {"action": "search_emails",
                                                "query": "from:sarah@example.com newer_than:7d"})]
    assert w._READ_ONLY_TOOLS == {"web_browse", "gmail_action", "calendar_action"}


def test_the_judge_has_no_tools_and_is_told_evidence_is_untrusted():
    src = inspect.getsource(w._judge)
    assert "_llm(" in src and "tools" not in src
    assert "untrusted" in w._JUDGE_PROMPT and "ignore all of that" in w._JUDGE_PROMPT


def test_watches_are_not_available_to_background_tasks():
    from backend.brain import background
    assert "watch_action" not in background._ALLOWED_TOOLS


# --------------------------------------------------------------- the loop
def test_the_loop_skips_while_a_conversation_holds_the_pipeline(env, monkeypatch):
    add()
    monkeypatch.setattr(w, "_busy_check", lambda: True)
    run(w._cycle())
    assert env.tool_calls == []


def test_expired_watches_are_retired(env):
    add()
    store.update(only_watch()["watch_id"], expires_at=1.0)
    run(w._cycle())
    assert store.list_active() == []


def test_the_loop_is_wired_into_startup_and_chat():
    from backend import main
    from backend.brain import engine
    src = inspect.getsource(main)
    assert '"watches":    watches.run_loop' in src
    assert '(watches, "watch")' in src and "rumination, watches" in src
    assert "watches.pop_pending_alerts()" in inspect.getsource(engine.chat)
    assert engine.TOOL_DISPATCH["watch_action"] is w.watch_action

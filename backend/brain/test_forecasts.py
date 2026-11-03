"""Confidence you can check: logged forecasts, automatic resolution, scoring, and the
self-correction that turns a gut probability into a measured one.

Evidence tools are faked at watches' dispatch table, so settling still goes through the
real tool gateway and the real SQLite store (conftest gives each test its own database).
"""
import asyncio
import datetime
import inspect
import json
import time

import pytest

from backend.brain import forecasts as fc
from backend.brain import watches
from backend.storage import forecasts as store

LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=2))
NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=LOCAL_TZ)


def run(coro):
    return asyncio.run(coro)


def _add(**kw):
    args = dict(action="add", claim="The Fed cuts rates at the October meeting",
                probability="70%", criteria="the FOMC statement lowers the target range",
                resolve_by="2026-10-29", domain="markets", method="web",
                check_query="FOMC October 2026 rate decision")
    args.update(kw)
    return run(fc.forecast(**args))


def _resolved(domain, raw_p, outcome, n=1, stated_p=None):
    """Seed resolved history directly, as if it had settled."""
    for _ in range(n):
        fid = store.add(claim="seed", criteria="seed criterion", domain=domain, raw_p=raw_p,
                        stated_p=raw_p if stated_p is None else stated_p, method="web",
                        check_query="q", resolve_by=time.time() - 86400)
        store.resolve(fid, outcome=outcome, resolver="web", quote="seed", notice="")


@pytest.fixture(autouse=True)
def paris(monkeypatch):
    monkeypatch.setattr(fc, "_local_now", lambda: NOW)


class Env:
    def __init__(self):
        self.evidence = "The Committee decided to lower the target range by 25 basis points."
        self.verdict = {"outcome": "yes", "quote": "decided to lower the target range",
                        "confidence": 0.9, "summary": "The Fed cut by 25bp."}
        self.search_ok = True
        self.judge_calls = 0
        self.sent: list[str] = []
        self.dm_ok = True


@pytest.fixture
def env(monkeypatch):
    e = Env()

    async def web_browse(task):
        return e.evidence if e.search_ok else "SEARCH FAILED -- no results."

    async def gmail_action(action, query="", max_results=10, **_):
        return e.evidence

    async def judge(f, evidence):
        e.judge_calls += 1
        return json.dumps(e.verdict)

    async def notifier(text):
        if e.dm_ok:
            e.sent.append(text)
        return e.dm_ok

    monkeypatch.setattr(watches, "_dispatch", lambda: {
        "web_browse": web_browse, "gmail_action": gmail_action,
        "calendar_action": gmail_action})
    monkeypatch.setattr(fc, "_judge", judge)
    monkeypatch.setattr(fc, "_notifier", notifier)
    return e


def _due_forecast(**kw):
    out = _add(**kw)
    assert out.startswith("Confirmed"), out
    return store.query(state=store.OPEN)[-1]


# ------------------------------------------------------------------ probabilities
@pytest.mark.parametrize("value,expected", [
    (0.7, 0.7), ("0.7", 0.7), ("70%", 0.7), ("70", 0.7), ("~65 %", 0.65),
    ("likely", 0.75), ("very unlikely", 0.1), ("coin flip", 0.5), ("More likely than not", 0.6),
])
def test_probabilities_and_hedges_parse(value, expected):
    assert fc.parse_probability(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", ["", "maybe-ish", None, True, "150%", -0.2])
def test_unparseable_probabilities_are_none(value):
    assert fc.parse_probability(value) is None


# ------------------------------------------------------------------ scoring
def test_brier_and_skill_against_the_base_rate():
    rows = [{"stated_p": 0.9, "outcome": 1}, {"stated_p": 0.2, "outcome": 0},
            {"stated_p": 0.6, "outcome": 0}, {"stated_p": 0.8, "outcome": 1}]
    s = fc.summarize(rows)
    assert s["brier"] == pytest.approx((0.01 + 0.04 + 0.36 + 0.04) / 4)
    assert s["base_rate"] == 0.5
    assert s["skill"] == pytest.approx(1 - s["brier"] / 0.25)


def test_safe_claims_score_well_on_brier_but_zero_on_skill():
    """Saying 90% on things that happen 90% of the time is calibrated and uninformative."""
    rows = [{"stated_p": 0.9, "outcome": 1}] * 9 + [{"stated_p": 0.9, "outcome": 0}]
    s = fc.summarize(rows)
    assert s["brier"] == pytest.approx(0.09)
    assert s["skill"] == pytest.approx(0.0, abs=1e-9)


def test_wilson_interval_is_wide_at_small_n():
    lo, hi = fc.wilson(2, 3)
    assert lo < 0.25 and hi > 0.9
    lo, hi = fc.wilson(200, 300)
    assert 0.6 < lo < hi < 0.72


def test_bands_group_by_stated_probability():
    rows = [{"stated_p": 0.7, "outcome": 1}, {"stated_p": 0.75, "outcome": 0},
            {"stated_p": 0.3, "outcome": 0}]
    got = {(round(b["lo"], 1), b["n"], b["hits"]) for b in fc.bands(rows)}
    assert got == {(0.6, 2, 1), (0.2, 1, 0)}


# ------------------------------------------------------------------ recalibration
def test_too_few_neighbours_leaves_the_gut_alone():
    history = [{"raw_p": 0.7, "outcome": 0}] * 4
    assert fc.recalibrate(0.7, history) == {"p": 0.7, "n": 4, "hits": 0, "adjusted": False}


def test_overconfidence_is_shrunk_toward_the_record():
    """The suggestions doc's example: 13 of 23 at ~70% -> between 57% and 70%."""
    history = [{"raw_p": 0.7, "outcome": 1}] * 13 + [{"raw_p": 0.72, "outcome": 0}] * 10
    cal = fc.recalibrate(0.7, history)
    assert cal["n"] == 23 and cal["hits"] == 13 and cal["adjusted"]
    assert cal["p"] == pytest.approx((13 + 8 * 0.7) / 31, abs=0.005)
    assert 13 / 23 < cal["p"] < 0.7


def test_only_nearby_confidence_counts_as_a_neighbour():
    history = [{"raw_p": 0.3, "outcome": 0}] * 20
    assert fc.recalibrate(0.7, history)["n"] == 0


def test_add_uses_the_domain_record(env):
    _resolved("markets", 0.7, 1, n=3)
    _resolved("markets", 0.7, 0, n=7)
    out = _add()
    assert "3 of 10 came true" in out and "overconfident" in out
    f = store.query(state=store.OPEN)[-1]
    assert f["raw_p"] == 0.7 and f["stated_p"] < 0.7
    assert f"State it as {round(f['stated_p'] * 100)}%" in out


def test_add_falls_back_to_all_areas_when_the_domain_is_thin(env):
    _resolved("tech", 0.7, 0, n=6)
    out = _add()
    assert "across all areas" in out
    assert store.query(state=store.OPEN)[-1]["stated_p"] < 0.7


def test_add_with_no_history_says_so_plainly(env):
    """Live, 'Only 0 resolved claims' was paraphrased as 'one' -- zero gets its own words."""
    out = _add()
    assert "No resolved claims" in out and "Only 0" not in out
    assert store.query(state=store.OPEN)[-1]["stated_p"] == 0.7


def test_add_with_thin_history_says_too_early(env):
    _resolved("markets", 0.7, 0, n=2)
    out = _add()
    assert "Only 2 resolved claim(s)" in out and "too early to correct" in out
    assert store.query(state=store.OPEN)[-1]["stated_p"] == 0.7


# ------------------------------------------------------------------ uncheckable claims
@pytest.mark.parametrize("kw,needle", [
    ({"probability": "99%"}, "statement of fact"),
    ({"probability": "1%"}, "statement of fact"),
    ({"criteria": "yes"}, "concrete criterion"),
    ({"check_query": ""}, "check_query"),
    ({"resolve_by": "2026-09-01"}, "has passed"),
    ({"resolve_by": "2028-01-01"}, "within 365"),
    ({"resolve_by": "soon"}, "YYYY-MM-DD"),
    ({"domain": "vibes"}, "domain"),
    ({"method": "calendar"}, "method"),
    ({"probability": "sort of"}, "probability must be"),
])
def test_uncheckable_claims_are_refused(env, kw, needle):
    out = _add(**kw)
    assert out.startswith("Error") and needle in out, out
    assert store.query() == []


def test_conversation_forecasts_need_no_query(env):
    assert _add(method="conversation", check_query="").startswith("Confirmed")


def test_add_records_the_originating_message(env):
    from backend.context import origin
    tok = origin.current_origin.set(origin.Origin(user_text="will the fed cut in october?"))
    try:
        _add()
    finally:
        origin.current_origin.reset(tok)
    assert store.query()[-1]["source_quote"] == "will the fed cut in october?"


# ------------------------------------------------------------------ resolution
def test_a_quoted_verdict_settles_and_is_dmed_once(env):
    f = _due_forecast()
    assert run(fc.evaluate(f)) == "resolved"
    row = store.get(f["forecast_id"])
    assert row["state"] == store.RESOLVED and row["outcome"] == 1
    assert row["resolution_quote"] == "decided to lower the target range"
    assert len(env.sent) == 1 and "decided to lower the target range" in env.sent[0]
    assert "the right column" in env.sent[0]
    assert row["notice_state"] == "sent"
    assert run(fc.evaluate(row)) == "lost"          # never scored twice
    assert len(env.sent) == 1


def test_an_invented_quote_does_not_settle(env):
    f = _due_forecast()
    env.verdict["quote"] = "The Fed announced a 50bp emergency cut"
    assert run(fc.evaluate(f)) == "unclear"
    row = store.get(f["forecast_id"])
    assert row["state"] == store.OPEN and row["attempts"] == 1 and env.sent == []


def test_low_confidence_does_not_settle(env):
    f = _due_forecast()
    env.verdict["confidence"] = 0.5
    assert run(fc.evaluate(f)) == "unclear"


def test_unclear_gives_up_after_a_week_and_asks_sam(env):
    f = _due_forecast()
    env.verdict = {"outcome": "unclear", "quote": "", "confidence": 0.2}
    for _ in range(fc._MAX_ATTEMPTS - 1):
        assert run(fc.evaluate(store.get(f["forecast_id"]))) == "unclear"
    assert run(fc.evaluate(store.get(f["forecast_id"]))) == "unresolvable"
    assert store.get(f["forecast_id"])["state"] == store.UNRESOLVABLE
    assert "couldn't settle" in fc.pop_pending_notices()


def test_a_failed_search_is_not_evidence_and_not_an_attempt(env):
    f = _due_forecast()
    env.search_ok = False
    assert run(fc.evaluate(f)) == "failed"
    row = store.get(f["forecast_id"])
    assert env.judge_calls == 0 and row["attempts"] == 0 and row["state"] == store.OPEN


def test_a_no_outcome_is_scored_as_a_miss(env):
    f = _due_forecast()
    env.evidence = "The Committee decided to maintain the target range at 4 percent."
    env.verdict = {"outcome": "no", "quote": "decided to maintain the target range",
                   "confidence": 0.95, "summary": "The Fed held."}
    assert run(fc.evaluate(f)) == "resolved"
    assert store.get(f["forecast_id"])["outcome"] == 0
    assert "the wrong column" in env.sent[0]


def test_a_failed_dm_is_mentioned_next_turn_exactly_once(env):
    f = _due_forecast()
    env.dm_ok = False
    run(fc.evaluate(f))
    note = fc.pop_pending_notices()
    assert "decided to lower the target range" in note
    assert fc.pop_pending_notices() == ""


def test_a_restart_mid_send_never_resends(env):
    f = _due_forecast()
    store.resolve(f["forecast_id"], outcome=1, resolver="web", quote="q", notice="settled F")
    assert store.reconcile_inflight() == 1
    assert "may already have reached him" in fc.pop_pending_notices()


def test_conversation_forecasts_ask_sam_then_give_up(env):
    f = _due_forecast(method="conversation", check_query="")
    assert run(fc.evaluate(f)) == "asked"
    assert "only Sam can settle it" in fc.pop_pending_notices()
    for _ in range(fc._MAX_ASKS - 1):
        run(fc.evaluate(store.get(f["forecast_id"])))
    assert run(fc.evaluate(store.get(f["forecast_id"]))) == "unresolvable"


def test_nothing_is_checked_before_the_due_date(env):
    _due_forecast()
    due = store.query()[-1]["resolve_by"]
    assert store.due(due - 60, 5) == []
    assert store.due(due + fc._SETTLE_DELAY_S + 1, 5)


# ------------------------------------------------------------------ manual resolution
def test_sam_can_settle_a_forecast(env):
    f = _due_forecast()
    out = run(fc.forecast(action="resolve", forecast_id=f["forecast_id"], outcome="no"))
    assert out.startswith("Confirmed") and "did not happen" in out
    assert store.get(f["forecast_id"])["outcome"] == 0
    again = run(fc.forecast(action="resolve", forecast_id=f["forecast_id"], outcome="yes"))
    assert again.startswith("Error")                  # a settled forecast stays settled


def test_a_forecast_cannot_be_voided_before_it_is_due(env):
    # Due a month from now: a fixed date passes with time and the test would stop testing this.
    f = _due_forecast(resolve_by=(datetime.date.today() + datetime.timedelta(days=30)).isoformat())
    out = run(fc.forecast(action="resolve", forecast_id=f["forecast_id"], outcome="void"))
    assert out.startswith("Error") and "going badly still counts" in out
    assert store.get(f["forecast_id"])["state"] == store.OPEN


def test_a_due_forecast_can_be_voided_and_is_counted(env):
    fid = store.add(claim="c", criteria="criterion text", domain="other", raw_p=0.6,
                    stated_p=0.6, method="web", check_query="q", resolve_by=time.time() - 60)
    out = run(fc.forecast(action="resolve", forecast_id=fid, outcome="void"))
    assert out.startswith("Confirmed")
    assert "1 that couldn't be settled" in fc.record_text()


# ------------------------------------------------------------------ the record
def test_record_with_nothing_logged():
    assert fc.record_text() == "No forecasts logged yet."


def test_record_reports_score_bands_areas_and_says_too_early(env):
    _resolved("markets", 0.7, 1, n=3)
    _resolved("markets", 0.7, 0, n=2)
    _resolved("tech", 0.3, 0, n=2)
    _add()
    text = fc.record_text()
    assert "7 resolved forecast(s), 1 open" in text
    assert "Brier" in text and "Skill vs always saying the base rate" in text
    assert "60%-80%: 3 of 5 true" in text
    assert "markets" in text and "tech" in text
    assert "Too early to tell" in text


def test_record_compares_gut_with_corrected_numbers(env):
    _resolved("markets", 0.9, 0, n=3, stated_p=0.6)
    assert "self-correction is helping" in fc.record_text()


def test_record_is_the_record_action():
    assert run(fc.forecast(action="record")) == fc.record_text()


# ------------------------------------------------------------------ wiring
def test_the_tool_is_registered_and_dispatched():
    from backend.brain import engine
    from backend.tools.registry import TOOLS
    assert engine.TOOL_DISPATCH["forecast"] is fc.forecast
    assert "forecast" in {t["name"] for t in TOOLS}
    assert "forecast" in engine._PERSISTENCE_TOOLS


def test_a_logged_claim_without_the_tool_is_caught():
    from backend.brain import engine
    reply = "Call it 62%. Logged it as a forecast -- we'll know on the 29th."
    assert engine._unbacked_claims(reply, set(), 0)
    assert not engine._unbacked_claims(reply, {"forecast"}, 1)
    assert not engine._unbacked_claims("I haven't logged it as a forecast yet.", set(), 0)


def test_pending_notices_reach_the_next_turn():
    from backend.brain import engine
    assert "forecasts.pop_pending_notices()" in inspect.getsource(engine.chat)


def test_forecast_questions_get_the_nudge():
    from backend.brain import personality as p
    for q in ("How likely is it that the Fed cuts?", "what are the odds NVDA beats",
              "Do you think it will rain on Saturday?"):
        assert p.is_forecast_request(q), q
    assert not p.is_forecast_request("I like the odd couple")
    stable, volatile = p.build_system_prompt("what are the odds NVDA beats?")
    assert p._FORECAST_FRAME in volatile and p._FORECAST_FRAME not in stable


def test_resolution_reuses_watches_read_only_evidence():
    src = inspect.getsource(fc.evaluate)
    assert "gather_evidence(" in src
    assert "watches" in inspect.getsource(fc) and "quote_in_evidence" in inspect.getsource(
        fc.parse_resolution)

"""Real forecast shapes in isolated SQLite. Empty and thin are normal, unavailable is different."""
from datetime import datetime, timedelta
import logging
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.brain import forecasts as F
from backend.mobile import record as R, routes, goals_view
from backend.storage import forecasts as S

LOCAL_TZ = ZoneInfo("Europe/Paris")
at = lambda day: datetime.fromisoformat(day).replace(tzinfo=LOCAL_TZ).timestamp()
EXAMPLES = [
    ("The FOMC will leave the target range unchanged at its next meeting", "The next statement retains the previous target range", "markets", .731, .67, 1),
    ("The next stable CPython release will use deferred annotations by default", "The final release notes document deferred annotation evaluation as the default", "tech", .917, .84, 0),
    ("The home side will win the cup match", "The official final result records a home win", "sports", .435, .48, 1),
    ("The committee will reject the proposed amendment", "The published vote records more votes against than for", "politics", .195, .2, 0),
    ("A launch attempt will be postponed for weather", "The operator announces a weather scrub on launch day", "science", .3947, .4, 1),
]


def add(i=0, *, resolved=True, when="2026-10-01T00:15:00", outcome=None):
    claim, criteria, domain, raw, stated, hit = EXAMPLES[i % len(EXAMPLES)]
    due = ((datetime.fromisoformat(when) - timedelta(days=2)).replace(hour=23, minute=59).isoformat()
           if resolved else "2026-10-31T23:59:00")
    fid = S.add(claim=claim, criteria=criteria, domain=domain, raw_p=raw, stated_p=stated, method="web",
                check_query=claim + " official result 2026", resolve_by=at(due),
                source_quote="A forecast stated during the September review.", now=at("2026-09-20T12:30:00"))
    assert fid
    if resolved:
        assert S.resolve(fid, outcome=hit if outcome is None else outcome, resolver="web",
                         quote="The official report confirms the published result.", notice="", now=at(when))
    return fid


def test_empty_and_open_only_have_no_score_and_match_discord():
    empty = R.view()
    assert empty["available"] and empty["summary"] == {"n": 0} and not empty["settled"]
    assert empty["text"] == F.record_text() == "No forecasts logged yet."
    late = add(resolved=False); early = add(1, resolved=False)
    S.update(early, resolve_by=at("2026-10-20T23:59:00"))
    v = R.view()
    assert v["summary"] == {"n": 0} and not v["bands"] and not v["by_month"]
    assert [r["id"] for r in v["open"]] == [early, late]
    assert "Nothing has resolved yet" in v["text"] and v["text"] == F.record_text()
    assert R.hub()["record"] == {"settled": 0, "brier": None, "early": True}


def test_thin_realistic_record_scores_both_numbers_and_exposes_the_saved_source():
    for i in range(5): add(i, when="2026-09-30T23:55:00" if i % 2 else "2026-10-01T00:15:00")
    waiting = add(3, resolved=False)
    void = add(2, resolved=False); S.update(void, state=S.UNRESOLVABLE)
    withdrawn = add(4, resolved=False); S.update(withdrawn, state=S.WITHDRAWN)
    rows = S.query(state=S.RESOLVED)
    v = R.view()
    assert v["summary"] == F.summarize(rows) and v["summary_gut"] == F.summarize(rows, "raw_p")
    assert v["bands"] == F.bands(rows) and v["bands_gut"] == F.bands(rows, "raw_p")
    assert v["early"] and v["threshold"] == 30 and v["caution"] in F.record_text()
    assert v["text"] == F.record_text() and v["void_count"] == v["withdrawn_count"] == 1
    assert [r["id"] for r in v["open"]] == [waiting]
    assert [m["month"] for m in v["by_month"]] == ["2026-09", "2026-10"]
    # UTC 30 Sep 22:15 is October in Paris, not September.
    assert v["by_month"][1]["n"] == 3
    assert v["settled"][0]["criteria"] and v["settled"][0]["resolution_quote"] and v["settled"][0]["check_query"]
    assert {d["domain"] for d in v["by_domain"]} == {r["domain"] for r in rows}


@pytest.mark.parametrize("n,early", [(1, True), (29, True), (30, False)])
def test_discord_threshold_and_single_outcome_baseline(n, early):
    for i in range(n): add(i, outcome=1)
    v = R.view()
    assert v["early"] is early and v["summary"]["skill"] is None
    assert bool(v["caution"]) is early
    assert R.hub()["record"]["early"] is early


def test_threshold_comes_from_the_canonical_module(monkeypatch):
    add(); monkeypatch.setattr(F, "_EARLY_RECORD", 1)
    assert not R.view()["early"] and R.view()["threshold"] == 1


def test_storage_failure_is_logged_and_not_reported_as_an_empty_record(monkeypatch, caplog):
    monkeypatch.setattr(S, "_conn", lambda: None)
    with caplog.at_level(logging.WARNING): v = R.view()
    assert not v["available"] and v["notes"] and "unavailable" in caplog.text
    assert v["text"] != "No forecasts logged yet."
    assert "record" not in R.hub()


def test_sql_error_is_logged_and_strict_read_raises(monkeypatch, caplog):
    class Broken:
        def execute(self, *args): raise RuntimeError("disk read failed")
    monkeypatch.setattr(S, "_conn", lambda: Broken())
    assert S.query() == []
    with pytest.raises(RuntimeError): S.query(strict=True)
    assert "disk read failed" in caplog.text


def test_scoring_failure_discards_partial_scores_and_logs(monkeypatch, caplog):
    add()
    monkeypatch.setattr(F, "bands", lambda *args: (_ for _ in ()).throw(ValueError("invalid band input")))
    v = R.view()
    assert not v["available"] and v["summary"] == {"n": 0} and not v["settled"]
    assert "invalid band input" in caplog.text


def test_hub_parts_are_independent(monkeypatch, caplog):
    monkeypatch.setattr(goals_view, "hub", lambda: (_ for _ in ()).throw(RuntimeError("goals off")))
    assert "record" in routes._hub_view() and "goals" not in routes._hub_view()
    assert "goals off" in caplog.text
    monkeypatch.setattr(goals_view, "hub", lambda: {"goals": {"active": 1}})
    monkeypatch.setattr(R, "hub", lambda: (_ for _ in ()).throw(ImportError("record absent")))
    assert routes._hub_view()["goals"]["active"] == 1 and "record" not in routes._hub_view()
    assert "record absent" in caplog.text


def test_hub_survives_the_record_module_being_absent(monkeypatch, caplog):
    import sys
    monkeypatch.setitem(sys.modules, "backend.mobile.record", None)
    monkeypatch.setattr(goals_view, "hub", lambda: {"goals": {"active": 2}})
    v = routes._hub_view()
    assert v["goals"]["active"] == 2 and "record" not in v
    assert v["notes"] and "record" in caplog.text


def test_routes_are_authenticated_conditional_and_change_after_resolution(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", "record-test-token")
    app = FastAPI(); app.include_router(routes.router); c = TestClient(app)
    assert c.get("/mobile/v1/record").status_code == 401
    auth = {"Authorization": "Bearer record-test-token"}
    fid = add(resolved=False)
    r = c.get("/mobile/v1/record", headers=auth); assert r.status_code == 200
    conditional = {**auth, "If-None-Match": r.headers["etag"]}
    assert c.get("/mobile/v1/record", headers=conditional).status_code == 304
    S.resolve(fid, outcome=1, resolver="conversation", quote="Yes, confirmed in the official statement.", notice="", now=at("2026-10-01T00:15:00"))
    changed = c.get("/mobile/v1/record", headers=conditional)
    assert changed.status_code == 200 and changed.json()["summary"]["n"] == 1
    assert c.get("/mobile/v1/hub", headers=auth).json()["record"]["settled"] == 1

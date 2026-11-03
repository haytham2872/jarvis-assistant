"""The phone's Portfolio tab (slice 3): the ledger as data, read from the same book
`portfolio show` reads. Exact figures stay decimal strings; derived percentages are rounded."""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.brain import portfolio as P
from backend.market import data
from backend.mobile import ledger as L
from backend.storage import approvals, portfolio as S
from backend.tools import policy

URL = "https://example.com/close"


def buy(symbol="NVDA", side="buy", qty="10", price="100", day="2026-09-01"):
    i = P.resolve_instrument(symbol)
    S.record_trades([{"instrument": i, "side": side, "shares": qty, "price": price, "currency": i["currency"],
                      "fees": "0", "native_price": price, "native_fees": "0", "traded_on": day,
                      "quote": "synthetic user fixture", "dedup_key": f"{symbol}:{side}:{qty}:{price}:{day}"}],
                    run_id="fixture", now=datetime(2026, 9, 1, tzinfo=data.LOCAL_TZ).timestamp())


def close(symbol, value, day):
    i = P.resolve_instrument(symbol)
    S.save_mark(symbol, {"day": day, "close": value, "currency": i["currency"], "source": "fixture", "url": URL})


def rate(day, value="0.9"):
    S.save_fx([{"pair": "USD/EUR", "day": day, "rate": value, "source": "ECB", "url": URL}])


@pytest.fixture
def nvda():
    rate("2026-09-01")
    rate("2026-09-03", "0.92")
    buy()
    close("NVDA", "150", "2026-09-02")
    close("NVDA", "165", "2026-09-03")


def position(v, symbol="NVDA"):
    return next(p for p in v["positions"] if p["symbol"] == symbol)


def test_a_position_carries_the_ledgers_exact_figures_and_the_days_move(nvda):
    v = L.view()
    p = position(v)
    assert (p["shares"], p["average_cost"], p["cost_basis"]) == ("10", "100", "1000")
    assert (p["close"], p["close_date"], p["prev_close"], p["day_change_pct"]) == ("165", "2026-09-03", "150", "10.00")
    assert (p["value"], p["pnl"], p["pnl_pct"]) == ("1650", "650", "65.00")
    assert (p["value_base"], p["pnl_base"], p["weight_pct"]) == ("1518", "618", "100.00")
    assert p["closes"] == [["2026-09-02", "150"], ["2026-09-03", "165"]]
    assert p["close_source"] == "fixture" and p["close_url"] == URL and p["held_for_split"] is False
    assert [(t["side"], t["shares"], t["price"], t["traded_on"]) for t in p["trades"]] == [("buy", "10", "100", "2026-09-01")]
    assert v["total"] == {"value_base": "1518", "pnl_base": "618", "close_date": "2026-09-03", "complete": True, "note": ""}


def test_the_total_moves_with_its_history(nvda):
    v = L.view()
    assert [h["date"] for h in v["history"]] == ["2026-09-02", "2026-09-03"]
    assert v["history"][-1]["value_base"] == "1518"
    assert v["day"] == {"date": "2026-09-03", "change_base": "168", "change_pct": "12.44"}


def test_weights_and_a_total_only_when_every_close_is_from_the_same_day(nvda):
    buy("TSLA", qty="2", price="200")
    close("TSLA", "250", "2026-09-02")                       # a day behind NVDA's
    v = L.view()
    assert position(v, "TSLA")["value_base"] == "450"           # 500 at the 09-01 rate: 0.92 is from 09-03
    assert position(v, "NVDA")["weight_pct"] == "77.13" and position(v, "TSLA")["weight_pct"] == "22.87"
    assert v["total"]["complete"] is False and v["total"]["value_base"] is None
    assert "different days" in v["total"]["note"]


def test_sold_out_and_tracked_only_are_kept_apart(nvda):
    buy("TSLA", qty="2", price="200")
    buy("TSLA", side="sell", qty="2", price="260", day="2026-09-02")
    policy.start_scope(["My thesis on AMD: their data centre share keeps growing"])
    S.set_thesis(P.resolve_instrument("AMD"), "data centre share keeps growing", "owner",
                 "My thesis on AMD: their data centre share keeps growing", "a year")
    close("AMD", "140", "2026-09-03")
    v = L.view()
    assert [p["symbol"] for p in v["positions"]] == ["NVDA"]
    assert v["closed"] == [{"symbol": "TSLA", "name": P.resolve_instrument("TSLA")["name"], "currency": "USD",
                            "realised": "120"}]
    assert [(w["symbol"], w["close"], w["close_date"]) for w in v["watching"]] == [("AMD", "140", "2026-09-03")]


def test_a_thesis_shows_its_conditions_and_how_far_the_close_is(nvda, tmp_path, monkeypatch):
    from backend.tools import vault
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "nvda-thesis.md").write_text("# NVDA thesis\n", encoding="utf-8")
    quote = "I hold NVDA because data centre demand keeps compounding"
    tid = S.set_thesis(P.resolve_instrument("NVDA"), "data centre demand keeps compounding", "owner", quote, "two years")
    P.add_condition(tid, "price_below", "wrong_if", "120", "", "", None, "wrong if NVDA closes below 120")
    (t,) = L.view()["theses"]
    assert (t["symbol"], t["why"], t["source"], t["horizon"], t["status"]) == (
        "NVDA", "data centre demand keeps compounding", "owner", "two years", "open")
    (c,) = t["conditions"]
    assert (c["kind"], c["direction"], c["level"], c["state"], c["linked"]) == ("price_below", "wrong_if", "120", "open", "watch")
    assert c["distance_pct"] == "27.27"                      # (165 - 120) / 165
    assert c["effective_level"] is None
    assert t["last_event"]["kind"] == "condition_added" and t["note"] == "topics/nvda-thesis.md"


def test_a_reached_level_reads_as_reached_and_a_settled_one_has_no_distance(nvda):
    quote = "I hold NVDA because data centre demand keeps compounding"
    tid = S.set_thesis(P.resolve_instrument("NVDA"), "demand keeps compounding", "owner", quote, "")
    P.add_condition(tid, "price_above", "right_if", "160", "", "", None, "right if NVDA closes above 160")
    (c,) = L.view()["theses"][0]["conditions"]
    assert c["distance_pct"] == "-3.03"                      # (160 - 165) / 165: above it already


def test_a_split_the_feed_reports_holds_the_figures_and_offers_the_request(nvda):
    S.note_split("NVDA", {"day": "2026-09-04", "numerator": 10, "denominator": 1, "source": "Yahoo", "url": URL})
    close("NVDA", "17", "2026-09-04")                         # in post-split shares
    v = L.view()
    p = position(v)
    assert p["held_for_split"] is True and p["close"] == "165" and p["closes"][-1] == ["2026-09-03", "165"]
    (s,) = v["splits"]
    assert (s["symbol"], s["ratio"], s["split_date"], s["shares_before"], s["shares_after"], s["approval"]) == (
        "NVDA", "10-for-1", "2026-09-04", "10", "100", None)
    assert v["total"]["complete"] is True                    # still one close date: the held one
    rid = approvals.create(context="portfolio", tool="portfolio", arguments={"action": "confirm_split"},
                           preview="Record NVDA's split", risk="own", why="")
    S.mark_split_asked("NVDA", "2026-09-04", rid)
    assert L.view()["splits"][0]["approval"]["id"] == rid


def test_an_expired_request_is_not_offered(nvda, monkeypatch):
    S.note_split("NVDA", {"day": "2026-09-04", "numerator": 10, "denominator": 1, "source": "Yahoo", "url": URL})
    rid = approvals.create(context="portfolio", tool="portfolio", arguments={"action": "confirm_split"},
                           preview="Record NVDA's split", risk="own", why="", ttl_s=-1)
    S.mark_split_asked("NVDA", "2026-09-04", rid)
    assert L.view()["splits"][0]["approval"] is None


def test_an_empty_ledger_and_an_unreadable_one(monkeypatch):
    v = L.view()
    assert v["positions"] == [] and v["total"]["complete"] is False and "error" not in v
    monkeypatch.setattr(S, "connection", lambda: (_ for _ in ()).throw(ValueError("storage unavailable")))
    v = L.view()
    assert v["error"] == "The portfolio could not be read just now." and v["positions"] == []


def test_a_broken_history_leaves_the_positions(nvda, monkeypatch):
    monkeypatch.setattr(S, "daily", lambda **_: (_ for _ in ()).throw(RuntimeError("calendar")))
    v = L.view()
    assert position(v)["value"] == "1650" and v["history"] == [] and v["day"] is None


def test_the_route_needs_the_token(monkeypatch, nvda):
    from backend.mobile import routes
    from backend.mobile.test_routes import AUTH, TOKEN
    monkeypatch.setenv("MOBILE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    assert client.get("/mobile/v1/portfolio").status_code == 401
    assert client.get("/mobile/v1/portfolio", headers=AUTH).json()["positions"][0]["symbol"] == "NVDA"


def test_after_a_confirmed_split_the_chart_and_the_day_start_at_it(nvda):
    S.note_split("NVDA", {"day": "2026-09-04", "numerator": 10, "denominator": 1, "source": "Yahoo", "url": URL})
    close("NVDA", "17", "2026-09-04")
    S.confirm_split("NVDA", "2026-09-04", "yes, record the split", run_id=None)
    p = position(L.view())
    assert (p["shares"], p["close"], p["held_for_split"]) == ("100", "17", False)
    assert p["closes"] == [["2026-09-04", "17"]]               # 150 and 165 were in the old shares
    assert p["prev_close"] is None and p["day_change_pct"] is None


def test_the_history_leaves_out_a_day_held_for_a_split(nvda):
    S.note_split("NVDA", {"day": "2026-09-04", "numerator": 10, "denominator": 1, "source": "Yahoo", "url": URL})
    close("NVDA", "17", "2026-09-04")
    v = L.view()
    assert [h["date"] for h in v["history"]] == ["2026-09-02", "2026-09-03"]
    assert v["day"] is None                                    # the newest day has no total


def test_a_settled_condition_has_no_distance(nvda):
    quote = "I hold NVDA because data centre demand keeps compounding"
    tid = S.set_thesis(P.resolve_instrument("NVDA"), "demand keeps compounding", "owner", quote, "")
    P.add_condition(tid, "price_below", "wrong_if", "170", "", "", None, "wrong if NVDA closes below 170")
    S.connection().execute("UPDATE thesis_conditions SET state='hit'")
    (c,) = L.view()["theses"][0]["conditions"]
    assert c["state"] == "hit" and c["distance_pct"] is None


def test_a_revised_thesis_keeps_its_conditions_and_names_the_revision(nvda):
    quote = "I hold NVDA because data centre demand keeps compounding"
    i = P.resolve_instrument("NVDA")
    tid = S.set_thesis(i, "demand keeps compounding", "owner", quote, "")
    P.add_condition(tid, "price_below", "wrong_if", "120", "", "", None, "wrong if NVDA closes below 120")
    S.set_thesis(i, "demand compounds and margins hold", "owner", "now it's demand plus margins", "",
                 status="revised", previous_id=tid)
    (t,) = L.view()["theses"]
    assert t["why"] == "demand compounds and margins hold" and [c["level"] for c in t["conditions"]] == ["120"]
    assert t["last_event"]["kind"] == "revised"


def test_a_decided_request_is_not_offered_again(nvda):
    import asyncio

    from backend.tools import approval_flow
    S.note_split("NVDA", {"day": "2026-09-04", "numerator": 10, "denominator": 1, "source": "Yahoo", "url": URL})
    rid = approvals.create(context="portfolio", tool="portfolio", arguments={"action": "confirm_split"},
                           preview="Record NVDA's split", risk="own", why="")
    S.mark_split_asked("NVDA", "2026-09-04", rid)
    asyncio.run(approval_flow.deny(rid, "phone"))
    (s,) = L.view()["splits"]
    assert s["approval"] is None                               # denied: the split itself still waits


def test_a_split_before_any_close_leaves_no_chart_rather_than_post_split_closes():
    rate("2026-09-01")
    buy()
    S.note_split("NVDA", {"day": "2026-09-02", "numerator": 10, "denominator": 1, "source": "Yahoo", "url": URL})
    close("NVDA", "17", "2026-09-02")                          # the first close is already in new shares
    p = position(L.view())
    assert p["close"] is None and p["closes"] == [] and p["held_for_split"] is True


def test_a_held_stock_a_close_behind_the_others_says_the_split_is_why(nvda):
    buy("TSLA", qty="2", price="200")
    close("TSLA", "250", "2026-09-02")
    close("TSLA", "260", "2026-09-03")
    S.note_split("NVDA", {"day": "2026-09-03", "numerator": 10, "denominator": 1, "source": "Yahoo", "url": URL})
    v = L.view()
    assert position(v)["close_date"] == "2026-09-02"            # NVDA held a day behind TSLA
    assert v["total"]["complete"] is False and v["total"]["note"] == "A split is waiting for your yes."


def test_the_history_and_the_authors_are_given_as_the_ledger_has_them(nvda):
    S.set_thesis(P.resolve_instrument("NVDA"), "the market overprices the next cycle", "my analysis",
                 "my analysis of NVDA", "6 months")
    v = L.view()
    assert v["history"][-1] == {"date": "2026-09-03", "value_base": "1518", "profit_base": "618"}
    assert [t["source"] for t in v["theses"]] == ["my analysis"]


def test_conditions_follow_every_revision_and_a_later_hit_is_the_latest_event(nvda):
    i = P.resolve_instrument("NVDA")
    v1 = S.set_thesis(i, "demand keeps compounding", "owner", "I hold NVDA because demand keeps compounding", "")
    P.add_condition(v1, "price_below", "wrong_if", "120", "", "", None, "wrong if NVDA closes below 120")
    v2 = S.set_thesis(i, "demand and margins", "owner", "demand and margins now", "", status="revised", previous_id=v1)
    P.add_condition(v2, "price_above", "right_if", "200", "", "", None, "right if NVDA closes above 200")
    S.set_thesis(i, "demand, margins and buybacks", "owner", "add buybacks", "", status="revised", previous_id=v2)
    S.event(v1, "condition_hit", "NVDA closed below 120", "system", "hit:test")
    (t,) = L.view()["theses"]
    assert t["why"] == "demand, margins and buybacks"
    assert sorted(c["level"] for c in t["conditions"]) == ["120", "200"]
    assert t["last_event"]["kind"] == "condition_hit"           # recorded on the first version, still the latest

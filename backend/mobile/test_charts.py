"""The phone's portfolio charts (next plan, slice 3): split-adjusted history, trades and levels
in today's shares, the total in euros over time, the cache, and the two proposals a dragged
line makes. No network: the feed functions are replaced."""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.brain import portfolio as P
from backend.market import data
from backend.mobile import charts as C
from backend.mobile.devserver import create_app
from backend.storage import display, portfolio as S, watches as W

NY = ZoneInfo("America/New_York")
URL = "https://example.com/close"


def yahoo(symbol, rows, currency="USD"):
    """A Yahoo chart body: (day, close) rows at 09:30 New York."""
    ts = [int(datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=NY).timestamp()) for d, _ in rows]
    return {"chart": {"result": [{"meta": {"symbol": symbol, "currency": currency, "exchangeTimezoneName": "America/New_York"},
                                  "timestamp": ts, "indicators": {"quote": [{"close": [c for _, c in rows]}]}}]}}


def buy(symbol="NVDA", side="buy", qty="10", price="100", day="2026-09-01", fees="0"):
    i = P.resolve_instrument(symbol)
    S.record_trades([{"instrument": i, "side": side, "shares": qty, "price": price, "currency": i["currency"],
                      "fees": fees, "native_price": price, "native_fees": fees, "traded_on": day,
                      "quote": "synthetic user fixture", "dedup_key": f"{symbol}:{side}:{qty}:{price}:{day}"}],
                    run_id="fixture", now=datetime(2026, 9, 1, tzinfo=data.LOCAL_TZ).timestamp())


def rate(day, value="0.9"):
    S.save_fx([{"pair": "USD/EUR", "day": day, "rate": value, "source": "ECB", "url": URL}])


@pytest.fixture
def feed(monkeypatch):
    """Replaces Yahoo and the ECB: closes and splits per symbol, rates per currency."""
    state = {"closes": {}, "splits": {}, "fx": {}, "calls": [], "fail": False}

    async def history(instrument, weekly=False):
        state["calls"].append(("history", instrument["symbol"], weekly))
        if state["fail"]:
            raise RuntimeError("feed down")
        return [{"day": d, "close": c} for d, c in state["closes"].get(instrument["symbol"], [])]

    async def splits(instrument, since):
        state["calls"].append(("splits", instrument["symbol"], since))
        if state["fail"]:
            raise RuntimeError("feed down")
        return [s for s in state["splits"].get(instrument["symbol"], []) if s["day"] >= since]

    async def fx(currency, start, end):
        state["calls"].append(("fx", currency, start))
        if state["fail"]:
            raise RuntimeError("feed down")
        return [{"day": d, "rate": r} for d, r in state["fx"].get(currency, [])]

    monkeypatch.setattr(data, "get_history", history)
    monkeypatch.setattr(data, "get_splits", splits)
    monkeypatch.setattr(data, "get_fx_history", fx)
    monkeypatch.setattr(C, "_today", lambda: date(2026, 10, 16))
    return state


def split(day, num="10", den="1"):
    return {"day": day, "numerator": num, "denominator": den, "source": "yahoo", "url": URL}


# ---------------------------------------------------------------- parsing and arithmetic

def test_history_is_read_as_given_adjusted_and_checked_against_the_instrument():
    i = P.resolve_instrument("NVDA")
    body = yahoo("NVDA", [("2024-06-07", Decimal("120.888")), ("2024-06-10", None), ("2024-06-11", Decimal("121.79"))])
    assert data.parse_history(body, i) == [{"day": "2024-06-07", "close": "120.888"}, {"day": "2024-06-11", "close": "121.79"}]
    with pytest.raises(ValueError):
        data.parse_history(yahoo("TSLA", [("2024-06-07", Decimal("1"))]), i)
    with pytest.raises(ValueError):
        data.parse_history(yahoo("NVDA", [("2024-06-07", Decimal("1"))], currency="EUR"), i)


def test_ecb_history_is_euros_per_unit():
    text = "KEY,TIME_PERIOD,OBS_VALUE\nX,2026-10-15,1.25\nX,2026-10-14,bad\nX,2026-10-16,1.0\n"
    assert data.parse_fx_history(text, "USD") == [{"day": "2026-10-15", "rate": "0.8"}, {"day": "2026-10-16", "rate": "1"}]
    with pytest.raises(ValueError):
        data.parse_fx_history("KEY,TIME_PERIOD,OBS_VALUE\n", "USD")


def test_a_share_held_before_a_split_becomes_its_ratio_in_todays_shares_and_the_ex_date_is_already_split():
    splits = [split("2021-07-20", "4"), split("2024-06-10", "10")]
    assert C.factor(splits, "2021-01-01") == 40
    assert C.factor(splits, "2024-06-07") == 10
    assert C.factor(splits, "2024-06-10") == 1         # bought on the ex-date: post-split shares
    assert C.factor([], "2024-06-07") == 1


def test_trades_average_cost_and_levels_are_moved_into_todays_shares():
    i = P.resolve_instrument("NVDA")
    trades = [{"id": 1, "side": "buy", "shares": "10", "native_price": "1000", "native_fees": "5", "traded_on": "2024-06-03"},
              {"id": 2, "side": "split", "shares": "10", "native_price": "0", "native_fees": "0", "traded_on": "2024-06-10"},
              {"id": 3, "side": "buy", "shares": "100", "native_price": "120", "native_fees": "0", "traded_on": "2024-06-12"},
              {"id": 4, "side": "sell", "shares": "50", "native_price": "130", "native_fees": "0", "traded_on": "2024-07-01"}]
    hist = {"closes": [("2024-07-01", Decimal("130"))], "splits": [split("2024-06-10")], "stale": False}
    conds = [{"id": 7, "thesis_id": 2, "kind": "price_below", "direction": "wrong_if", "level": "900", "state": "open",
              "criterion": "NVDA closes below 900", "local_date": "2024-06-05", "due_date": ""},
             {"id": 8, "thesis_id": 2, "kind": "dated_event", "direction": "right_if", "level": None, "state": "open",
              "criterion": "Rubin ships", "local_date": "2024-06-05", "due_date": "2026-12-01"}]
    out = C.compose_holding(i, trades, hist, {}, conds, [], "1Y", "2024-01-01")
    assert [(t["id"], t["shares"], t["price"]) for t in out["trades"]] == [
        (1, "100.000000", "100.0000"), (3, "100.000000", "120.0000"), (4, "50.000000", "130.0000")]
    # basis 10005 + 12000 = 22005 for 200 shares; selling 50 removes a quarter: 16503.75 for 150
    assert out["shares"] == "150.000000" and out["average_cost"] == "110.0250"
    (level,) = out["levels"]
    assert (level["level"], level["set_level"], level["distance_pct"]) == ("90.0000", "900", "30.77")
    assert out["dated"] == [{"id": 8, "thesis_id": 2, "direction": "right_if", "day": "2026-12-01",
                             "criterion": "Rubin ships", "state": "open"}]


def test_a_position_sold_out_has_no_average_cost():
    i = P.resolve_instrument("NVDA")
    trades = [{"id": 1, "side": "buy", "shares": "10", "native_price": "100", "native_fees": "0", "traded_on": "2026-09-01"},
              {"id": 2, "side": "sell", "shares": "10", "native_price": "110", "native_fees": "0", "traded_on": "2026-09-02"}]
    out = C.compose_holding(i, trades, {"closes": [], "splits": [], "stale": False}, {}, [], [], "1Y", "2026-01-01")
    assert out["average_cost"] is None and out["shares"] is None


def test_level_text_is_a_price_as_said():
    assert C.level_text(165) == "165" and C.level_text("165.456") == "165.46" and C.level_text(0.12345) == "0.1235"
    with pytest.raises(ValueError):
        C.level_text(-3)
    with pytest.raises(ValueError):
        C.level_text("abc")


# ---------------------------------------------------------------- the total

def test_the_total_follows_shares_bought_mid_range_carries_closes_and_rates_and_leaves_gaps_empty(feed):
    rate("2026-09-01", "0.9")
    buy("NVDA", qty="10", price="100", day="2026-09-01")
    buy("AIR.PA", qty="2", price="150", day="2026-09-03")
    feed["closes"] = {"NVDA": [("2026-08-31", "95"), ("2026-09-01", "100"), ("2026-09-02", "110"), ("2026-09-03", "120")],
                      "AIR.PA": [("2026-09-03", "160"), ("2026-09-04", "170")]}
    feed["fx"] = {"USD": [("2026-09-01", "0.9"), ("2026-09-03", "0.8")]}
    import asyncio
    out = asyncio.run(C.total("ALL"))
    days = {p["day"]: p for p in out["points"]}
    assert "2026-08-31" not in days                           # ALL starts at the first trade
    assert days["2026-09-01"]["value_base"] == "900.00" and days["2026-09-01"]["cost_base"] == "900.00"
    assert days["2026-09-02"]["value_base"] == "990.00"       # rate of 09-01 carried
    assert days["2026-09-03"]["value_base"] == "1280.00"      # 10*120*0.8 + 2*160
    assert days["2026-09-04"]["value_base"] == "1300.00"      # NVDA's 120 carried over its missing day
    assert days["2026-09-04"]["cost_base"] == "1200.00"
    assert out["gaps"] == 0 and out["stale"] is False and out["first_trade"] == "2026-09-01"


def test_a_split_after_the_trade_keeps_the_value_continuous(feed):
    rate("2026-09-01", "1")
    buy("NVDA", qty="10", price="1000", day="2026-09-01")
    feed["closes"] = {"NVDA": [("2026-09-01", "100"), ("2026-09-02", "105")]}   # Yahoo: already in today's shares
    feed["splits"] = {"NVDA": [split("2026-09-02")]}
    feed["fx"] = {"USD": [("2026-09-01", "1")]}
    import asyncio
    pts = asyncio.run(C.total("1Y"))["points"]
    assert [p["value_base"] for p in pts] == ["10000.00", "10500.00"]   # 10 shares at 1000, no tenfold jump


def test_a_day_without_a_rate_is_left_empty_rather_than_drawn_wrong(feed):
    rate("2026-09-01", "0.9")
    buy("NVDA", day="2026-09-01")
    feed["closes"] = {"NVDA": [("2026-09-01", "100"), ("2026-09-02", "101")]}
    feed["fx"] = {"USD": [("2026-09-02", "0.9")]}
    import asyncio
    out = asyncio.run(C.total("1Y"))
    assert [p["value_base"] for p in out["points"]] == [None, "909.00"] and out["gaps"] == 1


def test_no_trades_is_said_plainly(feed):
    import asyncio
    out = asyncio.run(C.total("1Y"))
    assert out["points"] == [] and out["note"] == "No trades yet." and feed["calls"] == []


def test_all_starts_at_the_first_trade_or_five_years_back_for_a_stock_never_traded():
    today = date(2026, 10, 18)
    assert C._window_start("ALL", "2026-09-01", today) == "2026-09-01"
    assert C._window_start("ALL", None, today) == "2021-10-18"
    assert C._window_start("1M", "2026-10-01", today) == "2026-10-01"     # never before the first trade
    assert C._window_start("1M", None, today) == "2026-09-17"


def test_a_bad_range_is_refused():
    with pytest.raises(ValueError):
        C._range("2W")


# ---------------------------------------------------------------- the cache

def test_a_fresh_series_is_not_fetched_again_and_a_failed_fetch_serves_the_old_copy_marked_stale(feed, monkeypatch):
    import asyncio
    i = P.resolve_instrument("NVDA")
    feed["closes"] = {"NVDA": [("2026-09-01", "100")]}
    first = asyncio.run(C.history(i, "2026-08-01"))
    assert first["stale"] is False and len([c for c in feed["calls"] if c[0] == "history"]) == 1
    asyncio.run(C.history(i, "2026-08-01"))
    assert len([c for c in feed["calls"] if c[0] == "history"]) == 1          # fresh: served from the cache
    rows, fetched, extra = display.load("close:NVDA:d")
    display.save("close:NVDA:d", rows, extra, now=fetched - C.FRESH_S - 1)    # six hours old
    feed["fail"] = True
    old = asyncio.run(C.history(i, "2026-08-01"))
    assert old["stale"] is True and old["closes"] == [("2026-09-01", Decimal("100"))]


def test_splits_older_than_the_cached_copy_covers_are_fetched_again(feed):
    import asyncio
    i = P.resolve_instrument("NVDA")
    feed["closes"] = {"NVDA": [("2026-09-01", "100")]}
    asyncio.run(C.history(i, "2026-08-01"))
    asyncio.run(C.history(i, "2024-01-01"))                                    # an older trade: needs older splits
    assert [c for c in feed["calls"] if c[0] == "splits"] == [("splits", "NVDA", "2026-08-01"), ("splits", "NVDA", "2024-01-01")]


def test_nothing_cached_and_the_feed_down_is_unavailable(feed):
    import asyncio
    feed["fail"] = True
    with pytest.raises(C.Unavailable):
        asyncio.run(C.history(P.resolve_instrument("NVDA"), "2026-08-01"))


# ---------------------------------------------------------------- the routes

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOBILE_TOKEN", "test-token")
    with TestClient(create_app()) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c


def thesis(symbol="NVDA"):
    quote = f"I hold {symbol} because data centre demand keeps compounding"
    return S.set_thesis(P.resolve_instrument(symbol), "data centre demand keeps compounding", "owner", quote, "")


def test_history_and_a_holding_route_and_history_is_not_read_as_a_symbol(client, feed):
    rate("2026-09-01")
    buy()
    feed["closes"] = {"NVDA": [("2026-09-01", "100"), ("2026-09-02", "110")]}
    feed["fx"] = {"USD": [("2026-09-01", "0.9")]}
    r = client.get("/mobile/v1/portfolio/history?range=1Y")
    assert r.status_code == 200 and [p["value_base"] for p in r.json()["points"]] == ["900.00", "990.00"]
    h = client.get("/mobile/v1/portfolio/nvda?range=1Y")
    assert h.status_code == 200
    body = h.json()
    assert body["symbol"] == "NVDA" and body["history"] == [["2026-09-01", "100.0000"], ["2026-09-02", "110.0000"]]
    assert body["average_cost"] == "100.0000" and body["position"]["symbol"] == "NVDA"
    assert client.get("/mobile/v1/portfolio/ZZZZ").status_code == 404
    assert client.get("/mobile/v1/portfolio/history?range=2W").status_code == 400
    assert client.get("/mobile/v1/portfolio/nvda?range=1Y", headers={"If-None-Match": h.headers["etag"]}).status_code == 304
    assert client.get("/mobile/v1/portfolio/history?range=1Y", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_the_feed_down_with_nothing_cached_is_503_not_a_crash(client, feed):
    buy()
    feed["fail"] = True
    assert client.get("/mobile/v1/portfolio/NVDA").status_code == 503
    total = client.get("/mobile/v1/portfolio/history").json()     # the total says so rather than drawing nothing silently
    assert total["points"] == [] and total["note"] == "The price history could not be fetched just now."


def test_a_dragged_level_becomes_one_watch_however_often_it_is_tapped(client, feed):
    buy()
    r = client.post("/mobile/v1/portfolio/NVDA/watch", json={"direction": "below", "level": 165.004})
    assert r.status_code == 200 and r.json()["new"] is True and "NVDA closes below 165 USD" in r.json()["text"]
    again = client.post("/mobile/v1/portfolio/NVDA/watch", json={"direction": "below", "level": 165})
    assert again.json()["new"] is False and again.json()["watch_id"] == r.json()["watch_id"]
    (w,) = W.list_active()
    assert (w["subject"], w["condition"], w["method"]) == ("NVDA", "NVDA closes below 165 USD", "web")
    assert client.post("/mobile/v1/portfolio/NVDA/watch", json={"direction": "sideways", "level": 1}).status_code == 409
    assert client.post("/mobile/v1/portfolio/NVDA/watch", json={"direction": "below", "level": -1}).status_code == 400
    assert client.post("/mobile/v1/portfolio/ZZZZ/watch", json={"direction": "below", "level": 1}).status_code == 404


def test_a_dragged_level_becomes_a_thesis_condition_through_the_ledgers_own_checks(client, feed):
    buy()
    tid = thesis()
    r = client.post("/mobile/v1/portfolio/NVDA/condition",
                    json={"thesis_id": tid, "direction": "wrong_if", "kind": "price_below", "level": 158.5})
    assert r.status_code == 200, r.text
    assert r.json()["text"].startswith("Added to the thesis: Wrong if NVDA closes below 158.5")
    (c,) = S.conditions(tid)
    assert (c["kind"], c["direction"], c["level"], c["watch_id"] is not None) == ("price_below", "wrong_if", "158.5", True)
    again = client.post("/mobile/v1/portfolio/NVDA/condition",
                        json={"thesis_id": tid, "direction": "wrong_if", "kind": "price_below", "level": 158.5})
    assert again.json()["new"] is False and len(S.conditions(tid)) == 1


def test_a_condition_needs_an_open_thesis_on_that_stock(client, feed):
    buy()
    buy("TSLA", qty="1", price="300")
    other = thesis("TSLA")
    for body, code in (({"thesis_id": other, "direction": "wrong_if", "kind": "price_below", "level": 1}, 409),
                       ({"thesis_id": 999, "direction": "wrong_if", "kind": "price_below", "level": 1}, 409),
                       ({"thesis_id": "1", "direction": "wrong_if", "kind": "price_below", "level": 1}, 400),
                       ({"thesis_id": other, "direction": "maybe", "kind": "price_below", "level": 1}, 409)):
        assert client.post("/mobile/v1/portfolio/NVDA/condition", json=body).status_code == code
    assert S.conditions() == []


def test_the_approved_words_do_not_outlive_the_tap(client, feed):
    from backend.tools import policy
    buy()
    tid = thesis()
    before = (policy.current_caller.get(), policy.owner_words(), policy.call_quote.get())
    client.post("/mobile/v1/portfolio/NVDA/condition",
                json={"thesis_id": tid, "direction": "wrong_if", "kind": "price_below", "level": 150})
    assert (policy.current_caller.get(), policy.owner_words(), policy.call_quote.get()) == before


def test_the_approved_words_do_not_outlive_the_tap_in_the_same_task(feed):
    # A test client runs each request in a copied context, so a leak never shows from outside:
    # look from inside the task that made the call.
    import asyncio
    from backend.tools import policy
    buy()
    tid = thesis()

    async def tap_then_look():
        # a known starting state, set inside this task: an earlier test may leave turn words behind
        policy.current_caller.set("unattended")
        policy.turn_words.set([])
        policy.call_quote.set("")
        await C.propose_condition("NVDA", tid, "wrong_if", "price_below", 150)
        return policy.current_caller.get(), policy.owner_words(), policy.call_quote.get()

    caller, words, quote = asyncio.run(tap_then_look())
    assert caller == "unattended" and words == [] and quote == ""

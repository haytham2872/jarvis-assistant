"""The review fixes of 3 October 2026, each reproduced first:

  * a stock split nobody recorded read as a crash and fired a "wrong if" alert;
  * recording a trade refused 9 of 16 plain ways of saying it;
  * a wrong entry could never be cancelled;
  * a close missed once (both feeds down, or /pause) stayed a hole for good;
  * the close fetch ran inside chronos's 30-second loop, so a slow feed held up reminders;
  * an event condition whose watch expired stayed "open" forever.

Model and network faked throughout; zero API spend.
"""
import asyncio
import inspect
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from backend.brain import portfolio as P
from backend.market import data
from backend.storage import portfolio as S, watches as W, forecasts as F
from backend.tools import gateway, outcomes, policy


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    async def rates(day):
        return None
    async def no_splits(i, since):
        return []
    monkeypatch.setattr(P, "_rates", rates)
    monkeypatch.setattr(data, "get_splits", no_splits)
    monkeypatch.setattr(P, "_notifier", None)
    monkeypatch.setattr(P, "_cycle_lock", asyncio.Lock())
    monkeypatch.setattr(P, "_split_next_try", {})
    policy.start_scope([])


def NVDA():
    return P.resolve_instrument("NVDA")


def seed(symbol="NVDA", side="buy", qty="10", price="100", day="2026-09-01", key=None):
    i = P.resolve_instrument(symbol)
    return S.record_trades([{"instrument": i, "side": side, "shares": qty, "price": price,
                            "currency": i["currency"], "fees": "0", "native_price": price,
                            "native_fees": "0", "traded_on": day, "quote": "synthetic user fixture",
                            "dedup_key": key or f"{symbol}:{side}:{qty}:{price}:{day}"}], run_id="fixture",
                           now=datetime(2026, 9, 1, tzinfo=data.LOCAL_TZ).timestamp())[0]


def mark(close, day, symbol="NVDA"):
    return S.save_mark(symbol, {"day": day, "close": close, "currency": "USD",
                                "source": "fixture", "url": "https://example.com/close"})


# =====================================================================================
# 1. Recording: the plain ways he says it are accepted, the rest still refused
# =====================================================================================

ACCEPTED = [
    ("I have 200 shares of NVDA at 118", "NVDA", "opening", "200", "118"),
    ("I hold 200 NVDA at $118", "NVDA", "opening", "200", "118"),
    ("I own 200 nvidia shares at an average cost of 118", "NVDA", "opening", "200", "118"),
    ("my NVDA position is 200 shares at 118", "NVDA", "opening", "200", "118"),
    ("I'm in NVDA with 200 shares, bought at 118", "NVDA", "opening", "200", "118"),
    ("200 NVDA at 118 average", "NVDA", "opening", "200", "118"),
    ("I have 200 NVDA, average price 118 dollars", "NVDA", "opening", "200", "118"),
    ("I have 200 NVDA at 118, not 120 like you said", "NVDA", "opening", "200", "118"),
    ("I still hold 200 NVDA at 118 although I might trim later", "NVDA", "opening", "200", "118"),
    ("I've got 200 shares of Nvidia, cost basis 118", "NVDA", "opening", "200", "118"),
    ("I've had 200 NVDA at 118 since last year", "NVDA", "opening", "200", "118"),
    ("Record this: 200 NVDA at 118.", "NVDA", "opening", "200", "118"),
    ("I have 1,000 NVDA at 118.50", "NVDA", "opening", "1000", "118.50"),
    ("100 shares of NVDA at 100", "NVDA", "opening", "100", "100"),
    ("I bought 50 Take-Two at 160 this morning", "TTWO", "buy", "50", "160"),
    ("bought 50 TTWO today for 160 each", "TTWO", "buy", "50", "160"),
    ("just picked up 50 shares of Take Two at 160", "TTWO", "buy", "50", "160"),
    ("added 10 more NVDA @ 230", "NVDA", "buy", "10", "230"),
    ("I sold 50 NVDA at 230", "NVDA", "sell", "50", "230"),
    ("sold 50 of my NVDA shares at 230", "NVDA", "sell", "50", "230"),
    ("trimmed my NVDA by 25 shares at 240", "NVDA", "sell", "25", "240"),
    ("I have 200 NVDA at 118 and 50 Take-Two at 160", "TTWO", "opening", "50", "160"),
    ("I sold 10 NVDA at 150 and 20 TTWO at 170", "TTWO", "sell", "20", "170"),
    ("I don't have TTWO any more, but I have 200 NVDA at 118", "NVDA", "opening", "200", "118"),
    ("NVDA split 10 for 1", "NVDA", "split", "10", "0"),
    ("I have 200 NVDA, which I bought at 118", "NVDA", "opening", "200", "118"),
    # held out: written after the code and run once before any change
    ("Put down 15 shares of Tesla at 250 for me", "TSLA", "opening", "15", "250"),
    ("I'm holding 40 TSLA, cost 210 each", "TSLA", "opening", "40", "210"),
    ("Bought 12 AMD at 155 earlier", "AMD", "buy", "12", "155"),
    ("I picked up another 5 NVDA at 231.40", "NVDA", "buy", "5", "231.40"),
    ("Sold my 30 TTWO at 182 this afternoon", "TTWO", "sell", "30", "182"),
    ("My Apple holding: 25 shares, average 190", "AAPL", "opening", "25", "190"),
    ("I own 3 shares of Microsoft at $410", "MSFT", "opening", "3", "410"),
    ("I have 0.5 NVDA at 230", "NVDA", "opening", "0.5", "230"),
]

REFUSED = [
    ("If I have 200 NVDA at 118", "NVDA", "opening", "200", "118"),
    ("You said I have 200 NVDA at 118", "NVDA", "opening", "200", "118"),
    ("I have 200 NVDA at 118?", "NVDA", "opening", "200", "118"),
    ("Should I buy 200 NVDA at 118?", "NVDA", "buy", "200", "118"),
    ("I'm thinking of buying 200 NVDA at 118", "NVDA", "buy", "200", "118"),
    ("I want to buy 200 NVDA at 118", "NVDA", "buy", "200", "118"),
    ("I'll buy 200 NVDA at 118 tomorrow", "NVDA", "buy", "200", "118"),
    ("I used to have 200 NVDA at 118", "NVDA", "opening", "200", "118"),
    ("I had 200 NVDA at 118", "NVDA", "opening", "200", "118"),
    ("I do not own 200 NVDA at 118", "NVDA", "opening", "200", "118"),
    ("I never bought 200 NVDA at 118", "NVDA", "buy", "200", "118"),
    ("my brother has 200 NVDA at 118", "NVDA", "opening", "200", "118"),
    ("If I have 200 NVDA and 50 TTWO at 160", "TTWO", "opening", "50", "160"),
    ("I have 200 NVDA at 118 and 50 TTWO at 160", "NVDA", "opening", "50", "160"),
    ("I have 118 NVDA at 200", "NVDA", "opening", "200", "118"),       # figures swapped
    ("I have 200 NVDA at 118", "NVDA", "sell", "200", "118"),
    ("I sold 50 NVDA at 230", "NVDA", "buy", "50", "230"),
    ("I have 200 NVDA at 118 and 30 PLTR at 50", "NVDA", "opening", "30", "50"),
    ("I have some NVDA at 118", "NVDA", "opening", "200", "118"),
    ("I have 200 NVDA, 118 of them bought last year", "NVDA", "opening", "200", "118"),  # 118 is no price
    # held out (the last two were accepted on that first run, and fixed)
    ("Would it be smart to buy 20 AMD at 150", "AMD", "buy", "20", "150"),
    ("If NVDA drops I'll buy 50 at 180", "NVDA", "buy", "50", "180"),
    ("My dad owns 100 Tesla at 200", "TSLA", "opening", "100", "200"),
    ("I sold all my TSLA", "TSLA", "sell", "40", "250"),
    ("What happens to my P&L if I hold 100 NVDA at 150", "NVDA", "opening", "100", "150"),
    ("Do I still have 40 TSLA at 210", "TSLA", "opening", "40", "210"),
    ("Pretend I have 100 NVDA at 100", "NVDA", "opening", "100", "100"),
]


def verdict(message, symbol, side, shares, price):
    policy.start_scope([message])
    i = P.resolve_instrument(symbol)
    try:
        P.verify_trade({"symbol": symbol, "side": side, "shares": shares, "price": price, "quote": message}, i)
        return None
    except ValueError as exc:
        return str(exc)


@pytest.mark.parametrize("case", ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_plain_statements_of_a_holding_are_accepted(case):
    assert verdict(*case) is None


@pytest.mark.parametrize("case", REFUSED, ids=[c[0] for c in REFUSED])
def test_questions_hypotheticals_denials_and_wrong_figures_are_refused(case):
    assert verdict(*case)


def test_a_backdated_trade_may_name_its_date_in_words(monkeypatch):
    from backend.storage import lifelog
    monkeypatch.setattr(lifelog, "local_date", lambda ts=None: "2026-10-03")
    message = "I bought 5 NVDA at 120 on 12 September"
    policy.start_scope([message])
    reply = run(P.portfolio("record_trade", trades=[{"symbol": "NVDA", "side": "buy", "shares": "5", "price": "120",
                                                     "traded_on": "2026-09-12", "quote": message}]))
    assert reply.startswith("Confirmed:"), reply
    assert S.history("NVDA")["trades"][0]["traded_on"] == "2026-09-12"
    policy.start_scope(["I bought 5 NVDA at 120"])
    assert run(P.portfolio("record_trade", trades=[{"symbol": "NVDA", "side": "buy", "shares": "5", "price": "120",
                                                    "traded_on": "2026-09-14", "quote": "I bought 5 NVDA at 120"}])).startswith("Error:")


def test_fees_must_be_said_as_fees():
    message = "I bought 10 NVDA at 200 plus 5 in fees"
    policy.start_scope([message])
    row = {"symbol": "NVDA", "side": "buy", "shares": "10", "price": "200", "fees": "5", "quote": message}
    assert run(P.portfolio("record_trade", trades=[row])).startswith("Confirmed:")
    policy.start_scope(["I bought 10 NVDA at 200 and 5 more later"])
    row = {**row, "quote": "I bought 10 NVDA at 200 and 5 more later"}
    assert run(P.portfolio("record_trade", trades=[row])).startswith("Error:")


# =====================================================================================
# 2. A wrong entry is cancelled, never edited
# =====================================================================================

def record(message, **trade):
    policy.start_scope([message])
    row = {"symbol": "NVDA", "side": "opening", "shares": "200", "price": "118", "quote": message, **trade}
    return run(P.portfolio("record_trade", trades=[row]))


def test_correcting_a_wrong_trade_cancels_it_and_records_the_right_one():
    assert record("I have 200 NVDA at 118").startswith("Confirmed:")
    tid = S.history("NVDA")["trades"][0]["id"]
    fix = "that's wrong, I have 20 NVDA at 118 not 200"
    policy.start_scope([fix])
    reply = run(P.portfolio("correct_trade", trade_id=tid, quote=fix, trades=[
        {"symbol": "NVDA", "side": "opening", "shares": "20", "price": "118", "quote": fix}]))
    assert reply.startswith("Confirmed:") and f"#{tid}" in reply, reply
    assert S.positions("NVDA")[0]["shares"] == "20"
    rows = S.history("NVDA")["trades"]
    assert [(r["shares"], r["voided"]) for r in rows] == [("200", 1), ("20", 0)]   # both kept
    void = S.connection().execute("SELECT quote FROM trade_voids").fetchone()[0]
    assert void == fix


def test_reproduced_live_a_correction_need_not_repeat_the_stock_or_the_unchanged_price():
    """Live, 3 October: "that's wrong, it's 20 shares not 200, same price" names neither NVDA
    nor 118, so the replacement was refused and the correction failed."""
    assert record("my NVDA position is 200 shares at 118").startswith("Confirmed:")
    tid = S.history("NVDA")["trades"][0]["id"]
    fix = "that's wrong, it's 20 shares not 200, same price"
    policy.start_scope([fix])
    for replacement in ({"symbol": "NVDA", "side": "opening", "shares": "20", "price": "118", "quote": fix},):
        reply = run(P.portfolio("correct_trade", trade_id=tid, quote=fix, trades=[replacement]))
    assert reply.startswith("Confirmed:"), reply
    assert S.positions("NVDA")[0]["shares"] == "20" and Decimal(S.positions("NVDA")[0]["average_cost"]) == 118


@pytest.mark.parametrize("change,ok", [
    ({"shares": "20"}, True),                       # only what changed; the rest from trade #1
    ({"shares": "25"}, False),                      # 25 is not in his words
    ({"shares": "20", "price": "121"}, False),      # a changed price must be in his words
    ({"shares": "20", "side": "sell"}, False),      # nor did he say it was a sale
    ({"shares": "20", "currency": "EUR"}, False),   # nor a different currency
])
def test_a_replacement_takes_only_unchanged_fields_from_the_corrected_trade(change, ok):
    record("my NVDA position is 200 shares at 118")
    tid = S.history("NVDA")["trades"][0]["id"]
    fix = "that's wrong, it's 20 shares not 200, same price"
    policy.start_scope([fix])
    reply = run(P.portfolio("correct_trade", trade_id=tid, quote=fix, trades=[change]))
    assert reply.startswith("Confirmed:") is ok, reply
    assert S.positions("NVDA")[0]["shares"] == ("20" if ok else "200")


def test_a_replacement_cannot_turn_a_buy_into_a_sale_he_did_not_say():
    """Only a different side is at stake here: holdings cover the sale, so nothing else refuses it."""
    seed(side="opening", qty="100", price="100")
    assert record("I bought 20 NVDA at 118", side="buy", shares="20").startswith("Confirmed:")
    buy = S.history("NVDA")["trades"][-1]["id"]
    fix = "that's wrong, it was 30 not 20"
    policy.start_scope([fix])
    assert run(P.portfolio("correct_trade", trade_id=buy, quote=fix, trades=[{"shares": "30", "side": "sell"}])).startswith("Error:")
    assert run(P.portfolio("correct_trade", trade_id=buy, quote=fix, trades=[{"shares": "30"}])).startswith("Confirmed:")
    assert S.positions("NVDA")[0]["shares"] == "130"

def test_a_correction_needs_his_words_saying_it_is_wrong():
    record("I have 200 NVDA at 118")
    tid = S.history("NVDA")["trades"][0]["id"]
    policy.start_scope(["please record this one too"])
    assert run(P.portfolio("correct_trade", trade_id=tid, quote="please record this one too")).startswith("Error:")
    policy.start_scope([])
    assert run(P.portfolio("correct_trade", trade_id=tid, quote="that's wrong, cancel it")).startswith("Error:")
    assert S.positions("NVDA")[0]["shares"] == "200"


def test_a_correction_that_breaks_a_later_sale_records_nothing():
    buy = seed(qty="10")
    seed(side="sell", qty="5", price="120", day="2026-09-10")
    policy.start_scope(["that buy was a mistake, remove it"])
    reply = run(P.portfolio("correct_trade", trade_id=buy, quote="that buy was a mistake, remove it"))
    assert reply.startswith("Error:") and "exceeds" in reply
    assert S.positions("NVDA")[0]["shares"] == "5"
    assert not S.connection().execute("SELECT 1 FROM trade_voids").fetchone()


def test_a_cancelled_trade_cannot_be_cancelled_twice_and_leaves_every_figure():
    tid = seed(qty="10", price="100")
    mark("150", "2026-09-02")
    S.void_trade(tid, "that was wrong", "that was wrong", run_id=None)
    with pytest.raises(ValueError, match="already cancelled"):
        S.void_trade(tid, "wrong again", "wrong again", run_id=None)
    p = S.positions("NVDA")[0]
    assert p["shares"] is None and p["value"] is None
    assert all(r["unrealised"] in (None, "0") for r in S.position_daily("NVDA"))
    # the same figures can be recorded again afterwards: a new fact, not the cancelled one
    assert record("I have 10 NVDA at 100", shares="10", price="100").startswith("Confirmed:")
    assert S.positions("NVDA")[0]["shares"] == "10"


def test_trade_voids_are_append_only():
    import sqlite3
    tid = seed()
    S.void_trade(tid, "wrong", "wrong", run_id=None)
    conn = S.connection()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with conn:
            conn.execute("DELETE FROM trade_voids")


def test_show_lists_trade_numbers_so_a_correction_can_name_one():
    tid = seed()
    assert f"#{tid} buy 10 at 100 USD on 2026-09-01" in P.show_text("NVDA")


# =====================================================================================
# 3. A split nobody recorded holds the figures; one tap records it
# =====================================================================================

SPLIT = {"day": "2026-10-01", "numerator": "10", "denominator": "1", "source": "yahoo",
         "url": "https://query1.finance.yahoo.com/v8/finance/chart/NVDA"}


def condition_below(level, when=datetime(2026, 9, 29, 12, tzinfo=data.LOCAL_TZ)):
    q = "My NVDA thesis is datacenter demand keeps growing"
    policy.start_scope([q])
    assert run(P.portfolio("set_thesis", symbol="NVDA", why="datacenter demand keeps growing", quote=q)).startswith("Confirmed:")
    tid = S.current_theses()[0]["id"]
    c = f"wrong if NVDA closes below {level}"
    policy.start_scope([c])
    import time as _time
    real = _time.time
    _time.time = lambda: when.timestamp()
    try:
        assert run(P.portfolio("add_condition", thesis_id=tid, kind="price_below", level=str(level), quote=c)).startswith("Confirmed:")
    finally:
        _time.time = real
    return S.conditions()[0]


@pytest.fixture
def split_world(monkeypatch):
    """10 NVDA at $1,000, a 'wrong if below 800' condition, then a real 10-for-1 split the
    ledger was never told about: the next close is $105."""
    seed(qty="10", price="1000", day="2026-09-01")
    condition_below(800)
    mark("1000", "2026-09-30")
    sent, asked = [], []
    async def tell(text):
        sent.append(text)
        return True
    monkeypatch.setattr(P, "_notifier", tell)
    async def splits(i, since):
        return [SPLIT]
    monkeypatch.setattr(data, "get_splits", splits)
    from backend.tools import approval_flow
    async def request(**kw):
        asked.append(kw)
        return {"id": 41}
    monkeypatch.setattr(approval_flow, "request", request)
    mark("105", "2026-10-01")
    return {"sent": sent, "asked": asked}


def settle(now=datetime(2026, 10, 2, 9, tzinfo=data.LOCAL_TZ)):
    run(P._check_splits(now, now.timestamp()))
    run(P._ask_about_splits(now.timestamp()))
    run(P.check_conditions(now.timestamp()))


def test_reproduced_an_unrecorded_split_no_longer_reads_as_a_crash(split_world):
    settle()
    assert split_world["sent"] == []                              # no false "wrong if" alert
    p = S.positions("NVDA")[0]
    assert p["mark_date"] == "2026-09-30" and Decimal(p["value"]) == 10000   # held at the last close before it
    assert S.conditions()[0]["state"] == "open"
    assert "split on 2026-10-01" in P.show_text() and "held" in P.show_text()
    assert "figures on hold" in P.brief_line()
    assert [s["state"] for s in S.splits("NVDA")] == ["pending"]


def test_he_is_asked_once_with_one_tap(split_world):
    settle()
    settle()
    [ask] = split_world["asked"]
    assert ask["tool"] == "portfolio" and ask["args"] == {"action": "confirm_split", "symbol": "NVDA",
                                                          "split_date": "2026-10-01"}
    assert ask["risk"] == policy.OWN and "10 shares become 100" in ask["label"]
    assert policy.preview("portfolio", ask["args"], ask["label"]) == ask["label"]
    assert S.splits("NVDA")[0]["approval_id"] == 41


def test_approving_records_the_split_and_scales_the_condition(split_world):
    settle()
    result = run(gateway.execute("portfolio", {"action": "confirm_split", "symbol": "NVDA", "split_date": "2026-10-01"},
                                 dispatch={"portfolio": P.portfolio}, caller=policy.APPROVED))
    assert result.outcome == outcomes.OK, result.result
    p = S.positions("NVDA")[0]
    assert p["shares"] == "100" and Decimal(p["value"]) == 10500 and Decimal(p["cost_basis"]) == 10000
    run(P.check_conditions(datetime(2026, 10, 2, 9, tzinfo=data.LOCAL_TZ).timestamp()))
    assert split_world["sent"] == []                              # 105 is above 80, the level in new shares
    assert "now 80" in P.show_text("NVDA")
    mark("75", "2026-10-02")
    run(P.check_conditions(datetime(2026, 10, 3, 9, tzinfo=data.LOCAL_TZ).timestamp()))
    [alert] = split_world["sent"]
    assert "level 80 after a split" in alert and "close 75" in alert
    # tapping twice (or confirming in words after tapping) changes nothing
    again = run(gateway.execute("portfolio", {"action": "confirm_split", "symbol": "NVDA", "split_date": "2026-10-01"},
                                dispatch={"portfolio": P.portfolio}, caller=policy.APPROVED))
    assert "already recorded" in again.result and S.positions("NVDA")[0]["shares"] == "100"


def test_confirming_needs_his_words_or_his_tap(split_world):
    settle()
    policy.start_scope(["what's my portfolio doing"])
    assert run(P.portfolio("confirm_split", symbol="NVDA", quote="the split is real")).startswith("Error:")
    assert S.positions("NVDA")[0]["shares"] == "10"
    policy.start_scope(["yes, record the NVDA split"])
    assert run(P.portfolio("confirm_split", symbol="NVDA", quote="yes, record the NVDA split")).startswith("Confirmed:")
    assert S.positions("NVDA")[0]["shares"] == "100"


def test_dismissing_a_wrong_report_lets_the_closes_count(split_world):
    settle()
    policy.start_scope(["that split is wrong, there was no split"])
    assert run(P.portfolio("dismiss_split", symbol="NVDA", quote="that split is wrong, there was no split")).startswith("Confirmed:")
    assert S.positions("NVDA")[0]["mark_date"] == "2026-10-01"
    run(P.check_conditions(datetime(2026, 10, 2, 9, tzinfo=data.LOCAL_TZ).timestamp()))
    assert len(split_world["sent"]) == 1                           # he said the close is real


def test_a_split_he_entered_himself_or_from_before_his_first_trade_is_not_asked(monkeypatch):
    seed(qty="10", price="1000", day="2026-09-01")
    seed(side="split", qty="10", price="0", day="2026-10-02")
    mark("105", "2026-10-02")
    async def splits(i, since):
        return [SPLIT, {**SPLIT, "day": "2024-06-10"}]
    monkeypatch.setattr(data, "get_splits", splits)
    run(P._check_splits(datetime(2026, 10, 3, tzinfo=data.LOCAL_TZ), 0))
    assert S.splits("NVDA") == []


def test_cancelling_a_confirmed_split_puts_it_back_on_hold(split_world):
    settle()
    run(gateway.execute("portfolio", {"action": "confirm_split", "symbol": "NVDA", "split_date": "2026-10-01"},
                        dispatch={"portfolio": P.portfolio}, caller=policy.APPROVED))
    tid = S.splits("NVDA")[0]["trade_id"]
    S.void_trade(tid, "that split was a mistake", "that split was a mistake", run_id=None)
    assert S.splits("NVDA")[0]["state"] == "pending"
    assert S.positions("NVDA")[0]["mark_date"] == "2026-09-30"


def test_a_trade_on_the_split_day_is_already_in_post_split_shares():
    """A split takes effect at the open of its ex-date, so a buy that day is in new shares,
    even when the split is recorded after the buy (as a confirmation always is)."""
    seed(qty="10", price="1000", day="2026-09-01")
    seed(qty="5", price="100", day="2026-10-01")
    seed(side="split", qty="10", price="0", day="2026-10-01")
    assert S.positions("NVDA")[0]["shares"] == "105"


def test_a_split_sized_jump_waits_for_the_split_check(monkeypatch):
    seed(qty="10", price="1000", day="2026-09-01")
    condition_below(800)
    mark("1000", "2026-09-30")
    sent = []
    async def tell(text):
        sent.append(text)
        return True
    monkeypatch.setattr(P, "_notifier", tell)
    async def down(i, since):
        raise ValueError("feed unreachable")
    monkeypatch.setattr(data, "get_splits", down)
    mark("400", "2026-10-01")                                     # -60%: a crash, or a 5-for-2 split?
    settle()
    assert sent == [] and S.conditions()[0]["state"] == "open"
    async def none(i, since):
        return []
    monkeypatch.setattr(data, "get_splits", none)
    monkeypatch.setattr(P, "_split_next_try", {})
    settle()
    [alert] = sent                                                # checked: no split, so it is real
    assert "close 400" in alert


# =====================================================================================
# 4. A close missed once is fetched later
# =====================================================================================

def no_reviews(monkeypatch):
    async def nothing(now):
        pass
    monkeypatch.setattr(P, "reviews_cycle", nothing)


def test_reproduced_a_missing_close_is_fetched_when_the_feed_is_back(monkeypatch):
    from backend.brain import pause
    monkeypatch.setattr(pause, "is_paused", lambda *a, **kw: False)
    no_reviews(monkeypatch)
    seed()
    feed = {"up": False}
    async def closes(i, day):
        if not feed["up"]:
            raise ValueError("all sources failed")
        return {"day": day, "close": "200", "currency": "USD", "source": "fixture", "url": "https://example.com"}
    monkeypatch.setattr(data, "get_closes", closes)
    run(P.cycle(datetime(2026, 9, 2, 23, tzinfo=data.LOCAL_TZ)))
    assert S.connection().execute("SELECT state FROM portfolio_fetches WHERE local_date='2026-09-01'").fetchone()[0] == "missing"
    feed["up"] = True
    run(P.cycle(datetime(2026, 9, 3, 3, tzinfo=data.LOCAL_TZ)))       # too soon for the 6-hour retry
    assert [m["local_date"] for m in S.history("NVDA")["marks"]] == ["2026-09-02"]
    run(P.cycle(datetime(2026, 9, 3, 6, tzinfo=data.LOCAL_TZ)))
    assert [m["local_date"] for m in S.history("NVDA")["marks"]] == ["2026-09-01", "2026-09-02"]


def test_closes_skipped_during_a_pause_are_fetched_after_resume(monkeypatch):
    from backend.brain import pause
    paused = {"yes": True}
    monkeypatch.setattr(pause, "is_paused", lambda *a, **kw: paused["yes"])
    skipped = []
    monkeypatch.setattr(pause, "record_skip", lambda *a: skipped.append(a))
    no_reviews(monkeypatch)
    seed()
    async def closes(i, day):
        return {"day": day, "close": "200", "currency": "USD", "source": "fixture", "url": "https://example.com"}
    monkeypatch.setattr(data, "get_closes", closes)
    when = datetime(2026, 9, 1, 23, tzinfo=data.LOCAL_TZ)
    run(P.cycle(when))
    run(P.cycle(when))
    assert len(skipped) == 1 and "fetched after /resume" in skipped[0][1]
    assert not S.history("NVDA")["marks"]
    paused["yes"] = False
    run(P.cycle(when + timedelta(minutes=1)))
    assert [m["local_date"] for m in S.history("NVDA")["marks"]] == ["2026-09-01"]


def test_a_breach_on_a_backfilled_day_still_counts(monkeypatch):
    seed(qty="10", price="200", day="2026-09-01")
    condition_below(180, when=datetime(2026, 9, 1, 12, tzinfo=data.LOCAL_TZ))
    sent = []
    async def tell(text):
        sent.append(text)
        return True
    monkeypatch.setattr(P, "_notifier", tell)
    S.split_checked("NVDA", "2026-09-30")
    mark("190", "2026-09-01")
    mark("185", "2026-09-03")
    run(P.check_conditions(datetime(2026, 9, 4, tzinfo=data.LOCAL_TZ).timestamp()))
    assert sent == []
    mark("170", "2026-09-02")                                     # filled in late
    run(P.check_conditions(datetime(2026, 9, 4, 1, tzinfo=data.LOCAL_TZ).timestamp()))
    [alert] = sent
    assert "close 170" in alert and "2026-09-02" in alert


def test_a_day_that_never_comes_back_is_tried_less_often(monkeypatch):
    from backend.brain import pause
    monkeypatch.setattr(pause, "is_paused", lambda *a, **kw: False)
    no_reviews(monkeypatch)
    seed()
    async def fail(i, day):
        raise ValueError("all sources failed")
    monkeypatch.setattr(data, "get_closes", fail)
    conn = S.connection()
    with conn:
        conn.execute("INSERT INTO portfolio_fetches VALUES ('NVDA','2026-09-01','missing',12,0,'x',0)")
    when = datetime(2026, 9, 2, 23, tzinfo=data.LOCAL_TZ)
    run(P.cycle(when))
    row = conn.execute("SELECT * FROM portfolio_fetches WHERE local_date='2026-09-01'").fetchone()
    assert row["attempts"] == 13 and row["next_at"] - when.timestamp() == P._SLOW_RETRY_S


# =====================================================================================
# 5. Its own loop, so a slow feed never delays a reminder
# =====================================================================================

def test_chronos_no_longer_waits_on_the_price_feed():
    from backend.brain import chronos
    assert "portfolio" not in inspect.getsource(chronos._cycle)


def test_the_portfolio_runs_its_own_loop():
    from backend import main
    assert '"portfolio":  portfolio.run_loop' in inspect.getsource(main)
    src = inspect.getsource(P.run_loop)
    assert 'name="portfolio"' in src and "pausable" not in src.split("run_periodic")[1]


# =====================================================================================
# 6. An event condition that never happened is settled, not open forever
# =====================================================================================

def dated(monkeypatch, probability=""):
    q = "My NVDA thesis is datacenter demand keeps growing"
    policy.start_scope([q])
    assert run(P.portfolio("set_thesis", symbol="NVDA", why="datacenter demand keeps growing", quote=q)).startswith("Confirmed:")
    tid = S.current_theses()[0]["id"]
    import time as _time
    real, stamp = _time.time, datetime(2026, 10, 1, 12, tzinfo=data.LOCAL_TZ).timestamp()
    c = "wrong if revenue falls below 40% on 2026-10-20" + (" with probability 60%" if probability else "")
    policy.start_scope([c])
    _time.time = lambda: stamp
    try:
        assert run(P.portfolio("add_condition", thesis_id=tid, kind="metric", criterion="revenue falls below 40%",
                               due_date="2026-10-20", probability=probability, quote=c)).startswith("Confirmed:")
    finally:
        _time.time = real
    return S.conditions()[0]


def test_an_expired_watch_settles_its_condition_as_not_seen(monkeypatch):
    c = dated(monkeypatch)
    W.update(c["watch_id"], state=W.EXPIRED)
    run(P.check_conditions(datetime(2026, 10, 29, tzinfo=data.LOCAL_TZ).timestamp()))
    run(P.check_conditions(datetime(2026, 10, 29, 1, tzinfo=data.LOCAL_TZ).timestamp()))
    assert S.conditions()[0]["state"] == "not_seen"
    assert [e["kind"] for e in S.history("NVDA")["events"]].count("condition_not_seen") == 1
    assert not S.active_conditions()


def test_an_unsettleable_forecast_settles_its_condition_as_unresolved(monkeypatch):
    c = dated(monkeypatch, probability="60%")
    F.update(c["forecast_id"], state=F.UNRESOLVABLE)
    run(P.check_conditions(datetime(2026, 10, 29, tzinfo=data.LOCAL_TZ).timestamp()))
    assert S.conditions()[0]["state"] == "unresolved"


# =====================================================================================
# The gateway tells a tool who is calling
# =====================================================================================

def test_the_gateway_tells_the_tool_who_is_calling():
    seen = []
    async def tool(action=""):
        seen.append(policy.current_caller.get())
        return "done"
    token = policy.current_caller.set(policy.LIVE)
    try:
        run(gateway.execute("portfolio", {"action": "show"}, dispatch={"portfolio": tool}, caller=policy.APPROVED))
        assert seen == [policy.APPROVED]
        assert policy.current_caller.get() == policy.LIVE          # put back afterwards
    finally:
        policy.current_caller.reset(token)


def test_reproduced_live_a_confirmed_split_reported_as_a_recorded_trade_is_not_a_false_claim():
    from backend.brain import engine as C
    reply = "The NVDA split is recorded as trade #3, dated 2026-10-04."   # live wording
    ok = {"portfolio", "portfolio:confirm_split"}
    assert not C._unbacked_claims(reply, ok, 1, asked_to_save=True)
    assert C._unbacked_claims(reply, {"portfolio", "portfolio:show"}, 1, asked_to_save=True)


def test_reproduced_live_describing_the_ledger_he_was_shown_is_not_a_claim():
    """Live: "how many NVDA shares do I have now?" answered from the portfolio block, with
    "the split was recorded", was flagged and sent through a correction pass."""
    from backend.brain import engine as C
    reply = "You have 200: your position was recorded at 20 and the split was recorded on the 4th."
    assert not C._unbacked_claims(reply, {"portfolio:context"}, 0)
    assert C._unbacked_claims(reply, set(), 0)
    # when he asked to record something, describing the ledger cannot stand in for doing it
    assert C._unbacked_claims(reply, {"portfolio:context"}, 0, asked_to_save=True)
    src = inspect.getsource(C.chat)
    reset = src.index("_reset_turn_stats()")
    assert src.index('_turn_tools_ok.add("portfolio:context")') > reset   # not wiped by the reset


def test_confirming_without_naming_the_stock_works_when_only_one_is_waiting(split_world):
    settle()
    policy.start_scope(["yes, record the split"])
    assert run(P.portfolio("confirm_split", quote="yes, record the split")).startswith("Confirmed:")
    # the repeat the model made live, also without the stock: says it is done, not an error
    again = run(P.portfolio("confirm_split", split_date="2026-10-01", quote="yes, record the split"))
    assert again.startswith("Confirmed:") and "already recorded" in again


def test_new_claims_need_the_matching_successful_call():
    from backend.brain import engine as C
    assert C._unbacked_claims("I've cancelled trade #3 and recorded the right one.", {"portfolio:show"}, 1, asked_to_save=True)
    assert not C._unbacked_claims("I've cancelled trade #3 and recorded the right one.", {"portfolio:correct_trade"}, 1)
    assert C._unbacked_claims("I've recorded the split.", set(), 0)
    assert not C._unbacked_claims("I've recorded the split.", {"portfolio:confirm_split"}, 1)


# =====================================================================================
# 7. 3 October, live: a true "good night" turned into "nothing is in the ledger"
# =====================================================================================
# The holdings were recorded; the reply to "good night" said so; the claim check called that
# unbacked (nothing was recorded in THAT turn) and told JARVIS to call the tool; the redo was
# refused because the figures were in the previous message; he told Sam the ledger was
# empty. Claim-check halves are in test_claim_check.py.

HELD = ("I hold 1.14339682 NVDA at an average price of $228.68; 0.24335794 TTWO at an average price "
        "of $238.13; 0.02822711 TSLA at an average price of $426.89")
FIGURES = [("NVDA", "1.14339682", "228.68"), ("TTWO", "0.24335794", "238.13"), ("TSLA", "0.02822711", "426.89")]


def held_rows(quote=HELD, figures=FIGURES):
    return [{"symbol": s, "side": "opening", "shares": q, "price": p, "quote": quote} for s, q, p in figures]


def live_count():
    return sum(1 for s, _, _ in FIGURES for t in S.trade_rows(s) if not t["voided"])


def test_reproduced_live_a_redo_from_an_earlier_message_says_the_trades_are_already_there():
    policy.start_scope([HELD])
    assert run(P.portfolio("record_trade", trades=held_rows())).startswith("Confirmed:")
    policy.start_scope(["i'm still really tired so i'm going to sleep now, good night"])
    again = run(P.portfolio("record_trade", trades=held_rows()))
    assert again.startswith("Already in the ledger"), again
    assert all(f"#{n} " in again for n in (1, 2, 3)) and "NVDA opening 1.14339682 at 228.68" in again
    assert not outcomes.is_failure("portfolio", again)       # a read of what is there, not an error
    assert live_count() == 3


def test_a_restated_holding_is_reported_not_refused_and_never_doubled():
    """Live, the next turn: "I hold 1.14339682 NVDA at 228.68, ..." said again."""
    policy.start_scope([HELD])
    run(P.portfolio("record_trade", trades=held_rows()))
    restated = "I hold 1.14339682 NVDA at 228.68, 0.24335794 TTWO at 238.13, 0.02822711 TSLA at 426.89"
    policy.start_scope([restated])
    reply = run(P.portfolio("record_trade", trades=held_rows(restated)))
    assert reply.startswith("Already in the ledger"), reply
    assert live_count() == 3


def test_a_trade_not_in_the_ledger_from_an_earlier_message_is_still_refused():
    policy.start_scope([HELD])
    run(P.portfolio("record_trade", trades=held_rows()))
    policy.start_scope(["good night"])
    other = run(P.portfolio("record_trade", trades=held_rows(figures=[("NVDA", "2", "228.68")])))
    assert other.startswith("Error:") and "portfolio show" in other     # and it says where to look
    assert live_count() == 3


def test_a_batch_records_the_new_trade_and_names_the_one_already_there():
    policy.start_scope([HELD])
    run(P.portfolio("record_trade", trades=held_rows(figures=FIGURES[:1])))
    policy.start_scope([HELD])
    reply = run(P.portfolio("record_trade", trades=held_rows()))
    assert reply.startswith("Confirmed: recorded and verified trades #2, #3.") and "Already in the ledger" in reply, reply
    assert live_count() == 3


@pytest.mark.parametrize("changed", [{"traded_on": "2026-09-14"}, {"fees": "5"}, {"currency": "EUR"}])
def test_a_buy_is_the_same_trade_only_with_the_same_day_fees_and_currency(changed, monkeypatch):
    from backend.storage import lifelog
    monkeypatch.setattr(lifelog, "local_date", lambda ts=None: "2026-10-03")
    policy.start_scope(["I bought 5 NVDA at 120"])
    buy = {"symbol": "NVDA", "side": "buy", "shares": "5", "price": "120", "quote": "I bought 5 NVDA at 120"}
    assert run(P.portfolio("record_trade", trades=[buy])).startswith("Confirmed:")
    assert P._already_recorded(buy)["id"] == 1
    assert P._already_recorded({**buy, **changed}) is None


def test_a_buy_he_states_again_in_a_new_message_is_still_a_new_buy():
    """Unchanged by the fix: only an opening, or a trade his current words do not back, is
    reported as already there. A second buy he states is recorded."""
    policy.start_scope(["I bought 5 NVDA at 120"])
    buy = {"symbol": "NVDA", "side": "buy", "shares": "5", "price": "120", "quote": "I bought 5 NVDA at 120"}
    run(P.portfolio("record_trade", trades=[buy]))
    policy.start_scope(["I bought 5 NVDA at 120 again this afternoon"])
    reply = run(P.portfolio("record_trade", trades=[{**buy, "quote": "I bought 5 NVDA at 120 again this afternoon"}]))
    assert reply.startswith("Confirmed: recorded and verified trades #2."), reply


def test_a_buy_redone_from_an_earlier_message_is_reported_as_already_there(monkeypatch):
    from backend.storage import lifelog
    monkeypatch.setattr(lifelog, "local_date", lambda ts=None: "2026-10-03")
    said = "I bought 5 NVDA at 120"
    policy.start_scope([said])
    buy = {"symbol": "NVDA", "side": "buy", "shares": "5", "price": "120", "quote": said}
    run(P.portfolio("record_trade", trades=[buy]))
    policy.start_scope(["good night"])
    again = run(P.portfolio("record_trade", trades=[buy]))
    assert again.startswith("Already in the ledger"), again
    assert "#1 NVDA buy 5 at 120" in again and len(S.trade_rows("NVDA")) == 1


def test_a_cancelled_trade_is_not_already_there():
    policy.start_scope(["I have 200 NVDA at 118"])
    run(P.portfolio("record_trade", trades=[{"symbol": "NVDA", "side": "opening", "shares": "200",
                                             "price": "118", "quote": "I have 200 NVDA at 118"}]))
    fix = "that's wrong, I have 20 NVDA at 118 not 200"
    policy.start_scope([fix])
    run(P.portfolio("correct_trade", trade_id=1, quote=fix, trades=[{"shares": "20"}]))
    policy.start_scope(["good night"])
    stale = {"symbol": "NVDA", "side": "opening", "shares": "200", "price": "118", "quote": "I have 200 NVDA at 118"}
    assert P._already_recorded(stale) is None
    assert run(P.portfolio("record_trade", trades=[stale])).startswith("Error:")
    assert S.positions("NVDA")[0]["shares"] == "20"

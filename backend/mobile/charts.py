"""The phone's portfolio charts (next plan, slice 3): the total over time, and one holding's
price with his trades, average cost, thesis levels and forecasts on it.

Drawing data, not figures. Prices are Yahoo's split-adjusted closes, so a year reads in today's
shares; trades and levels are moved into today's shares with the same split list, so a line, a
trade marker and a level always agree. Every exact figure on the screen still comes from the
ledger (mobile/ledger.py). History is cached per stock (storage/display.py) and fetched again
after six hours; when the feed fails, the cached copy is served and marked stale.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import re
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from backend.market import data
from backend.storage import display

log = logging.getLogger(__name__)
FRESH_S = 6 * 3600
RANGES = {"1M": 31, "6M": 183, "1Y": 366, "ALL": None}
_NEVER_TRADED_DAYS = 1826
_ADDING = ("buy", "opening")
_TRADED = ("buy", "sell", "opening")


class NotFound(LookupError):
    pass


class Unavailable(RuntimeError):
    pass


def _today() -> date:
    return datetime.now(data.LOCAL_TZ).date()


def _range(value: str) -> str:
    r = (value or "1Y").upper()
    if r not in RANGES:
        raise ValueError("range is 1M, 6M, 1Y or ALL")
    return r


def _window_start(r: str, first: str | None, today: date) -> str:
    """Where a range starts: never before the first trade, for ALL the first trade itself (five
    years for a stock never traded, which he only watches)."""
    days = RANGES[r]
    start = (today - timedelta(days=days)).isoformat() if days else (first or (today - timedelta(days=_NEVER_TRADED_DAYS)).isoformat())
    return max(start, first) if first and days else start


def _s(value: Decimal | None, places: int = 2) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal(1).scaleb(-places)), "f")


def factor(splits: list[dict], after: str) -> Decimal:
    """How many of today's shares one share held on `after` became: every split after that
    day. A trade dated on the ex-date is already in post-split shares."""
    out = Decimal(1)
    for s in splits:
        if s["day"] > after:
            out *= Decimal(s["numerator"]) / Decimal(s["denominator"])
    return out


# ---------------------------------------------------------------- cached feeds

async def _cached(series: str, fetch, still_good) -> tuple[list[tuple[str, str]], dict, bool]:
    """A fresh cached series, else a new fetch, else the stale copy. Raises Unavailable when
    there is nothing to show at all."""
    rows, fetched_at, extra = await asyncio.to_thread(display.load, series)
    if fetched_at is not None and time.time() - fetched_at < FRESH_S and still_good(extra):
        return rows, extra, False
    try:
        new_rows, new_extra = await fetch()
        await asyncio.to_thread(display.save, series, new_rows, new_extra)
        return new_rows, new_extra, False
    except Exception as exc:
        log.warning("charts: %s not fetched (%s)", series, type(exc).__name__)
        if rows:
            return rows, extra, True
        raise Unavailable("The price history could not be fetched just now.") from None


async def history(instrument: dict, start: str) -> dict:
    """Adjusted closes from `start` to today, and every split since `start`."""
    today = _today()
    year_ago = (today - timedelta(days=360)).isoformat()

    async def daily():
        rows = await data.get_history(instrument)
        splits = await data.get_splits(instrument, start)
        return [(r["day"], r["close"]) for r in rows], {"splits": splits, "since": start}

    d_rows, d_extra, stale = await _cached(f"close:{instrument['symbol']}:d", daily,
                                           lambda extra: extra.get("since", "9999") <= start)
    rows = list(d_rows)
    if start < year_ago:                         # older than the daily year: weekly before it
        async def weekly():
            return [(r["day"], r["close"]) for r in await data.get_history(instrument, weekly=True)], {}
        w_rows, _, w_stale = await _cached(f"close:{instrument['symbol']}:w", weekly, lambda extra: True)
        first_daily = rows[0][0] if rows else today.isoformat()
        rows = [r for r in w_rows if r[0] < first_daily] + rows
        stale = stale or w_stale
    return {"closes": [(d, Decimal(c)) for d, c in rows if d >= start],
            "splits": [s for s in d_extra.get("splits", []) if s["day"] >= start] if d_extra.get("since", start) <= start
            else d_extra.get("splits", []), "stale": stale}


async def rates(currency: str, start: str) -> tuple[list[tuple[str, Decimal]], bool]:
    """Euros per unit of `currency`, by ECB reference date, from a week before `start`."""
    if currency == data.BASE:
        return [], False
    begin = (date.fromisoformat(start) - timedelta(days=7)).isoformat()

    async def fetch():
        rows = await data.get_fx_history(currency, begin, _today().isoformat())
        return [(r["day"], r["rate"]) for r in rows], {"since": begin}

    rows, _, stale = await _cached(f"fx:{currency}", fetch, lambda extra: extra.get("since", "9999") <= begin)
    return [(d, Decimal(v)) for d, v in rows], stale


def _at(series: list[tuple[str, Decimal]], day: str, days: list[str] | None = None) -> Decimal | None:
    """The last value on or before `day` (a close carried over a holiday, a rate over a weekend)."""
    keys = days if days is not None else [d for d, _ in series]
    i = bisect.bisect_right(keys, day) - 1
    return series[i][1] if i >= 0 else None


# ---------------------------------------------------------------- the total

def _traded() -> dict[str, tuple[dict, list[dict], list[tuple]]]:
    """Each stock ever traded: its instrument, its live trades, and its ledger book rows
    (with each trade's own ECB rate, for the cost in the base currency)."""
    from backend.storage import portfolio as store
    conn = store.connection()
    out = {}
    for i in store.instruments():
        trades = [dict(t) for t in conn.execute(
            "SELECT * FROM live_trades WHERE instrument=? ORDER BY traded_on, id", (i["symbol"],))]
        if not any(t["side"] in _TRADED for t in trades):
            continue
        book = []
        for t in trades:
            fx = store.fx_rate(i["currency"], t["traded_on"], _conn=conn)
            book.append((t["side"], t["shares"], t["native_price"], t["native_fees"],
                         fx["rate"] if fx else None, t["traded_on"], t["id"]))
        out[i["symbol"]] = (i, trades, book)
    return out


def _cost_base(book: list[tuple], day: str) -> Decimal | None:
    from backend.storage import portfolio as store
    folded = store._fold([row for row in book if row[5] <= day])
    return Decimal(folded["cost_base"]) if folded["cost_base"] is not None else None


def compose_total(traded: dict, hist: dict, fx: dict, r: str, start: str) -> dict:
    """Value and cost in the base currency on every session from `start`. A day where a held stock has no
    close or no rate yet is left empty rather than drawn wrong."""
    days = sorted({d for h in hist.values() for d, _ in h["closes"] if d >= start})
    points, gaps = [], 0
    for day in days:
        value = cost = Decimal(0)
        complete = True
        for symbol, (instrument, trades, book) in traded.items():
            h = hist[symbol]
            shares = sum((Decimal(t["shares"]) * factor(h["splits"], t["traded_on"])
                          * (1 if t["side"] in _ADDING else -1))
                         for t in trades if t["side"] in _TRADED and t["traded_on"] <= day)
            if shares:
                close = _at(h["closes"], day)
                rate = Decimal(1) if instrument["currency"] == data.BASE else _at(fx.get(instrument["currency"], []), day)
                if close is None or rate is None:
                    complete = False
                else:
                    value += shares * close * rate
            c = _cost_base(book, day)
            if c is None:
                complete = False
            else:
                cost += c
        if not complete:
            gaps += 1
        points.append({"day": day, "value_base": _s(value) if complete else None,
                       "cost_base": _s(cost) if complete else None})
    return {"range": r, "currency": data.BASE, "points": points, "gaps": gaps}


async def total(range_value: str = "1Y") -> dict:
    """GET /portfolio/history."""
    r = _range(range_value)
    traded = await asyncio.to_thread(_traded)
    if not traded:
        return {"range": r, "currency": data.BASE, "points": [], "gaps": 0, "stale": False,
                "note": "No trades yet."}
    first = min(t["traded_on"] for _, trades, _ in traded.values() for t in trades if t["side"] in _TRADED)
    start = _window_start(r, first, _today())
    symbols = list(traded)
    found = await asyncio.gather(*(history(traded[s][0], start) for s in symbols), return_exceptions=True)
    if any(isinstance(f, Exception) for f in found):
        return {"range": r, "currency": data.BASE, "points": [], "gaps": 0, "stale": False,
                "note": "The price history could not be fetched just now."}
    hist = dict(zip(symbols, found))
    currencies = sorted({traded[s][0]["currency"] for s in symbols} - {data.BASE})
    got = await asyncio.gather(*(rates(c, start) for c in currencies), return_exceptions=True)
    fx = {c: g[0] for c, g in zip(currencies, got) if not isinstance(g, Exception)}
    out = await asyncio.to_thread(compose_total, traded, hist, fx, r, start)
    out["stale"] = any(h["stale"] for h in hist.values()) or any(not isinstance(g, Exception) and g[1] for g in got)
    out["note"] = "" if len(fx) == len(currencies) else "Euro rates could not be fetched; some days are missing."
    out["first_trade"] = first
    return out


# ---------------------------------------------------------------- one holding

def instrument_of(symbol: str) -> dict:
    """A stock the ledger knows (held, sold or watched); nothing else."""
    from backend.storage import portfolio as store
    found = next((i for i in store.instruments() if i["symbol"].casefold() == (symbol or "").casefold()), None)
    if found is None:
        raise NotFound("That stock is not in your portfolio.")
    return found


def _ledger_part(symbol: str) -> dict:
    from backend.mobile import ledger
    view = ledger.view()
    pick = lambda rows: next((x for x in rows if x["symbol"] == symbol), None)  # noqa: E731
    return {"position": pick(view.get("positions", [])), "watching": pick(view.get("watching", [])),
            "closed": pick(view.get("closed", [])),
            "theses": [t for t in view.get("theses", []) if t["symbol"] == symbol],
            "split": next((s for s in view.get("splits", []) if s["symbol"] == symbol), None)}


def _forecasts(symbol: str, name: str, linked: set[int]) -> list[dict]:
    from backend.storage import forecasts as fs
    word = re.compile(rf"\b{re.escape(symbol)}\b")
    first = (name or "").split()[0] if name else ""
    by_name = re.compile(rf"\b{re.escape(first)}\b", re.I) if len(first) >= 4 else None
    out = []
    for f in fs.query():
        claim = f.get("claim") or ""
        if f["forecast_id"] in linked or word.search(claim) or (by_name and by_name.search(claim)):
            out.append({"id": f["forecast_id"], "claim": claim, "p": f.get("stated_p"), "state": f["state"],
                        "outcome": f.get("outcome"),
                        "made": datetime.fromtimestamp(f["created_at"], data.LOCAL_TZ).date().isoformat(),
                        "due": datetime.fromtimestamp(f["resolve_by"], data.LOCAL_TZ).date().isoformat() if f.get("resolve_by") else None,
                        "settled": datetime.fromtimestamp(f["resolved_at"], data.LOCAL_TZ).date().isoformat() if f.get("resolved_at") else None})
    return out


def compose_holding(instrument: dict, trades: list[dict], hist: dict, ledger_part: dict, conds: list[dict],
                    forecasts: list[dict], r: str, start: str) -> dict:
    splits = hist["splits"]
    marks, basis, shares = [], Decimal(0), Decimal(0)
    for t in trades:
        if t["side"] not in _TRADED:
            continue                              # the ledger's split rows: the factor already covers them
        f = factor(splits, t["traded_on"])
        qty, price, fees = Decimal(t["shares"]), Decimal(t["native_price"]), Decimal(t["native_fees"])
        if t["side"] in _ADDING:
            shares += qty * f
            basis += qty * price + fees
        elif shares:
            removed = basis * (qty * f) / shares
            basis -= removed
            shares -= qty * f
        marks.append({"id": t["id"], "day": t["traded_on"], "side": t["side"],
                      "shares": _s(qty * f, 6), "price": _s(price / f, 4), "fees": _s(fees, 2)})
    closes = hist["closes"]
    last = closes[-1][1] if closes else None
    levels = []
    for c in conds:
        if not c["kind"].startswith("price_") or not c.get("level"):
            continue
        level = Decimal(c["level"]) / factor(splits, c["local_date"])
        gap = None
        if last is not None and c["state"] == "open":
            gap = (last - level if c["kind"] == "price_below" else level - last) / last * 100
        levels.append({"id": c["id"], "thesis_id": c["thesis_id"], "kind": c["kind"], "direction": c["direction"],
                       "level": _s(level, 4), "set_level": c["level"], "state": c["state"],
                       "criterion": c["criterion"], "distance_pct": _s(gap, 2)})
    dated = [{"id": c["id"], "thesis_id": c["thesis_id"], "direction": c["direction"], "day": c["due_date"],
              "criterion": c["criterion"], "state": c["state"]}
             for c in conds if not c["kind"].startswith("price_") and c.get("due_date")]
    return {
        "symbol": instrument["symbol"], "name": instrument["name"], "currency": instrument["currency"],
        "range": r, "start": start,
        "history": [[d, _s(c, 4)] for d, c in closes],
        "splits": [{"day": s["day"], "ratio": f"{s['numerator']}-for-{s['denominator']}"} for s in splits],
        "trades": marks, "average_cost": _s(basis / shares, 4) if shares else None,
        "shares": _s(shares, 6) if shares else None,
        "levels": levels, "dated": dated, "forecasts": forecasts,
        "stale": hist["stale"], **ledger_part,
    }


def _holding_records(symbol: str) -> tuple[list[dict], list[dict], dict]:
    from backend.storage import portfolio as store
    conn = store.connection()
    trades = [dict(t) for t in conn.execute(
        "SELECT * FROM live_trades WHERE instrument=? ORDER BY traded_on, id", (symbol,))]
    roots = {r[0]: r[1] or r[0] for r in conn.execute("SELECT id, root_id FROM theses WHERE instrument=?", (symbol,))}
    current = {t["root_id"] or t["id"]: t["id"] for t in store.current_theses(symbol, open_only=True)}
    conds = []
    for c in store.conditions():
        root = roots.get(c["thesis_id"])
        if root in current:
            conds.append({**c, "thesis_id": current[root]})
    return trades, conds, _ledger_part(symbol)


async def holding(symbol: str, range_value: str = "1Y") -> dict:
    """GET /portfolio/{symbol}."""
    r = _range(range_value)
    instrument = await asyncio.to_thread(instrument_of, symbol)
    trades, conds, ledger_part = await asyncio.to_thread(_holding_records, instrument["symbol"])
    traded = [t["traded_on"] for t in trades if t["side"] in _TRADED]
    first = min(traded) if traded else None
    start = _window_start(r, first if r == "ALL" else None, _today())
    split_since = min(start, first) if first else start
    hist = await history(instrument, split_since)
    hist = {**hist, "closes": [(d, c) for d, c in hist["closes"] if d >= start]}
    linked = {c["forecast_id"] for c in conds if c.get("forecast_id")}
    forecasts = await asyncio.to_thread(_forecasts, instrument["symbol"], instrument["name"], linked)
    return await asyncio.to_thread(compose_holding, instrument, trades, hist, ledger_part, conds, forecasts, r, start)


# ---------------------------------------------------------------- proposals from a dragged line

def level_text(level) -> str:
    """A price as he would say it: cents above $1, four places below."""
    value = data.number(level)
    step = Decimal("0.01") if value >= 1 else Decimal("0.0001")
    return format(value.quantize(step, rounding=ROUND_HALF_UP).normalize(), "f")


class Refused(ValueError):
    """The ledger or the watch store said no; the reason is for the owner."""


async def propose_watch(symbol: str, direction: str, level) -> dict:
    """POST /portfolio/{symbol}/watch: a daily check that the close crossed a level he dragged
    to and confirmed. A plain watch (no thesis): the ledger only checks closes for theses."""
    from backend.brain import watches
    from backend.storage import watches as wstore
    if direction not in ("below", "above"):
        raise Refused("Choose below or above.")
    instrument = await asyncio.to_thread(instrument_of, symbol)
    price = level_text(level)
    sym = instrument["symbol"]
    condition = f"{sym} closes {direction} {price} {instrument['currency']}"
    same = next((w for w in await asyncio.to_thread(wstore.list_active)
                 if w["subject"] == sym and w["condition"] == condition), None)
    if same:
        return {"text": f"Already watching: {condition}.", "watch_id": same["watch_id"], "new": False}
    text = await watches.watch_action("add", subject=sym, condition=condition, method="web",
                                      query=f"{sym} {instrument['name']} stock price close today", every="day")
    if text.startswith("Error:"):
        raise Refused(text[6:].strip())
    made = next((w for w in await asyncio.to_thread(wstore.list_active)
                 if w["subject"] == sym and w["condition"] == condition), None)
    return {"text": f"Watching: {condition}. Checked once a day; you'll get a message when it happens.",
            "watch_id": made["watch_id"] if made else None, "new": True}


async def propose_condition(symbol: str, thesis_id: int, direction: str, kind: str, level) -> dict:
    """POST /portfolio/{symbol}/condition: a price condition on one of his open theses. His tap
    on the sheet is his decision, so the sentence it stands for is passed as his words to the
    ledger's own checks (the level and the direction must be in them)."""
    from backend.brain import portfolio as P
    from backend.storage import portfolio as store
    from backend.tools import policy
    if direction not in ("wrong_if", "right_if") or kind not in ("price_below", "price_above"):
        raise Refused("Choose wrong if or right if, below or above.")
    instrument = await asyncio.to_thread(instrument_of, symbol)
    sym = instrument["symbol"]
    theses = await asyncio.to_thread(store.current_theses, sym, open_only=True)
    if not any(t["id"] == thesis_id for t in theses):
        raise Refused(f"That is not an open thesis on {sym}.")
    price = level_text(level)
    sentence = f"{'wrong' if direction == 'wrong_if' else 'right'} if {sym} closes {'below' if kind == 'price_below' else 'above'} {price}"
    tokens = (policy.current_caller.set(policy.APPROVED), policy.call_quote.set(sentence),
              policy.turn_words.set([sentence]))
    try:
        text = await P.portfolio(action="add_condition", thesis_id=thesis_id, kind=kind, direction=direction,
                                 level=price, quote=sentence)
    finally:
        policy.turn_words.reset(tokens[2])
        policy.call_quote.reset(tokens[1])
        policy.current_caller.reset(tokens[0])
    if text.startswith("Error:"):
        raise Refused(text[6:].strip())
    already = "already recorded" in text
    return {"text": (f"Already on the thesis: {sentence}." if already
                     else f"Added to the thesis: {sentence[0].upper()}{sentence[1:]}. Checked at every close."),
            "new": not already}

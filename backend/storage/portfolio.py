"""The exact portfolio book. Immutable facts, Decimal arithmetic, code-owned views.

No holdings are seeded here. The tool verifies current user evidence before calling
record_trades. Price/FX writers have no trading capability. Storage failures raise;
the tool/scheduler reports them rather than confirming an unrecorded holding.

Facts are never edited. A trade recorded wrongly is cancelled by a row in `trade_voids`
(with his words), and every reader goes through `live_trades`, which leaves voided trades
out. A split the price feed reports but the ledger does not have is a row in
`portfolio_splits`; while it is pending, that stock's figures stop at the last close
before it, so an unrecorded split can never read as a crash.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from backend.market.data import BASE, number, sessions
from backend.storage import db, lifelog

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
 symbol TEXT PRIMARY KEY, exchange TEXT NOT NULL, name TEXT NOT NULL,
 currency TEXT NOT NULL, price_key TEXT NOT NULL, ts REAL NOT NULL, local_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
 id INTEGER PRIMARY KEY, instrument TEXT NOT NULL REFERENCES instruments(symbol),
 side TEXT NOT NULL CHECK(side IN ('buy','sell','opening','split')),
 shares TEXT NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL, fees TEXT NOT NULL,
 native_price TEXT NOT NULL, native_fees TEXT NOT NULL, conversion_rate TEXT,
 traded_on TEXT NOT NULL, quote TEXT NOT NULL, run_id TEXT, dedup_key TEXT NOT NULL UNIQUE,
 ts REAL NOT NULL, local_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument,traded_on,id);
CREATE TABLE IF NOT EXISTS trade_voids (
 trade_id INTEGER PRIMARY KEY REFERENCES trades(id), quote TEXT NOT NULL,
 reason TEXT NOT NULL, run_id TEXT, ts REAL NOT NULL, local_date TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS live_trades AS
 SELECT t.* FROM trades t WHERE NOT EXISTS (SELECT 1 FROM trade_voids v WHERE v.trade_id=t.id);
CREATE TABLE IF NOT EXISTS marks (
 id INTEGER PRIMARY KEY, instrument TEXT NOT NULL REFERENCES instruments(symbol),
 local_date TEXT NOT NULL, close TEXT NOT NULL, currency TEXT NOT NULL,
 source TEXT NOT NULL, url TEXT NOT NULL, ts REAL NOT NULL,
 UNIQUE(instrument,local_date)
);
CREATE TABLE IF NOT EXISTS fx (
 id INTEGER PRIMARY KEY, pair TEXT NOT NULL, local_date TEXT NOT NULL, rate TEXT NOT NULL,
 source TEXT NOT NULL, url TEXT NOT NULL, ts REAL NOT NULL, UNIQUE(pair,local_date)
);
CREATE TABLE IF NOT EXISTS theses (
 id INTEGER PRIMARY KEY, instrument TEXT NOT NULL REFERENCES instruments(symbol),
 root_id INTEGER, previous_id INTEGER REFERENCES theses(id), why TEXT NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('owner','my analysis')), quote TEXT NOT NULL,
 horizon TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','revised','broken','closed','abandoned')),
 ts REAL NOT NULL, local_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thesis_conditions (
 id INTEGER PRIMARY KEY, thesis_id INTEGER NOT NULL REFERENCES theses(id),
 kind TEXT NOT NULL CHECK(kind IN ('price_below','price_above','dated_event','metric')),
 direction TEXT NOT NULL CHECK(direction IN ('wrong_if','right_if')), level TEXT,
 criterion TEXT NOT NULL, due_date TEXT NOT NULL DEFAULT '',
 watch_id TEXT, forecast_id INTEGER, state TEXT NOT NULL DEFAULT 'open',
 ts REAL NOT NULL, local_date TEXT NOT NULL,
 UNIQUE(thesis_id,kind,direction,criterion,due_date)
);
CREATE TABLE IF NOT EXISTS thesis_events (
 id INTEGER PRIMARY KEY, thesis_id INTEGER NOT NULL REFERENCES theses(id),
 condition_id INTEGER REFERENCES thesis_conditions(id), kind TEXT NOT NULL,
 reason TEXT NOT NULL, source TEXT NOT NULL, ts REAL NOT NULL, local_date TEXT NOT NULL,
 event_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS portfolio_fetches (
 instrument TEXT NOT NULL REFERENCES instruments(symbol), local_date TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
 reason TEXT NOT NULL DEFAULT '', ts REAL NOT NULL, PRIMARY KEY(instrument,local_date)
);
CREATE TABLE IF NOT EXISTS portfolio_reviews (
 condition_id INTEGER PRIMARY KEY REFERENCES thesis_conditions(id), state TEXT NOT NULL,
 job_id TEXT, reason TEXT NOT NULL DEFAULT '', ts REAL NOT NULL, local_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_splits (
 instrument TEXT NOT NULL REFERENCES instruments(symbol), split_date TEXT NOT NULL,
 ratio TEXT NOT NULL, numerator TEXT NOT NULL, denominator TEXT NOT NULL,
 source TEXT NOT NULL, url TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','confirmed','dismissed')),
 approval_id INTEGER, asked INTEGER NOT NULL DEFAULT 0, next_ask_at REAL NOT NULL DEFAULT 0,
 trade_id INTEGER, decided_at REAL, ts REAL NOT NULL, local_date TEXT NOT NULL,
 PRIMARY KEY(instrument,split_date)
);
CREATE TABLE IF NOT EXISTS portfolio_split_checks (
 instrument TEXT PRIMARY KEY REFERENCES instruments(symbol), checked_through TEXT NOT NULL,
 ts REAL NOT NULL
);
CREATE VIEW IF NOT EXISTS v_positions AS
 SELECT i.*, json_extract(b.book,'$.shares') AS shares,
 json_extract(b.book,'$.average_cost') AS average_cost,
 json_extract(b.book,'$.cost_basis') AS cost_basis,
 json_extract(b.book,'$.cost_base') AS cost_base,
 json_extract(b.book,'$.realised') AS realised,
 json_extract(b.book,'$.realised_base') AS realised_base,
 m.close AS last_mark, m.local_date AS mark_date, m.source AS mark_source, m.url AS mark_url,
 f.rate AS fx_rate, f.local_date AS fx_date,
 decimal_mul(json_extract(b.book,'$.shares'),m.close) AS value,
 decimal_sub(decimal_mul(json_extract(b.book,'$.shares'),m.close),json_extract(b.book,'$.cost_basis')) AS unrealised,
 decimal_mul(decimal_mul(json_extract(b.book,'$.shares'),m.close),CASE WHEN i.currency='EUR' THEN '1' ELSE f.rate END) AS value_base,
 decimal_sub(decimal_mul(decimal_mul(json_extract(b.book,'$.shares'),m.close),CASE WHEN i.currency='EUR' THEN '1' ELSE f.rate END),json_extract(b.book,'$.cost_base')) AS unrealised_base
 FROM instruments i
 LEFT JOIN (SELECT t.instrument, portfolio_book(t.side,t.shares,t.native_price,t.native_fees,
 CASE WHEN i.currency='EUR' THEN '1' ELSE f.rate END,t.traded_on,t.id) AS book
 FROM live_trades t JOIN instruments i ON i.symbol=t.instrument
 LEFT JOIN fx f ON f.id=(SELECT id FROM fx WHERE pair=i.currency||'/EUR' AND local_date<=t.traded_on
 AND local_date>=date(t.traded_on,'-7 days') ORDER BY local_date DESC LIMIT 1)
 GROUP BY t.instrument) b ON b.instrument=i.symbol
 LEFT JOIN marks m ON m.id=(SELECT id FROM marks WHERE instrument=i.symbol
 AND local_date>=COALESCE((SELECT max(traded_on) FROM live_trades WHERE instrument=i.symbol AND side='split'),'0001-01-01')
 AND local_date<COALESCE((SELECT min(split_date) FROM portfolio_splits WHERE instrument=i.symbol AND state='pending'),'9999-12-31')
 ORDER BY local_date DESC LIMIT 1)
 LEFT JOIN fx f ON f.id=(SELECT id FROM fx WHERE pair=i.currency||'/EUR' AND local_date<=m.local_date
 AND local_date>=date(m.local_date,'-7 days') ORDER BY local_date DESC LIMIT 1);
CREATE VIEW IF NOT EXISTS v_portfolio_daily AS
 SELECT d.local_date, json_extract(d.book,'$.value_base') AS value_base,
 json_extract(d.book,'$.cost_base') AS cost_base,
 json_extract(d.book,'$.realised_base') AS realised_base,
 json_extract(d.book,'$.profit_base') AS profit_base,
 json_extract(d.book,'$.change_base') AS change_base
 FROM (SELECT local_date,portfolio_daily(local_date) AS book FROM marks GROUP BY local_date) d;
"""

# The views total in the base currency (market.data.BASE, JARVIS_BASE_CURRENCY): an amount
# already in it counts as is, any other is converted with the pair "X/BASE".
SCHEMA = SCHEMA.replace("i.currency='EUR'", f"i.currency='{BASE}'").replace("||'/EUR'", f"||'/{BASE}'")

# A positions view without this text predates the current one and is replaced in place; so
# is one built for another base currency.
_VIEW_MARKER = "portfolio_splits"
_APPEND_ONLY = ("trades", "trade_voids", "marks", "fx", "theses", "thesis_events")


def _fold(rows: list[tuple]) -> dict:
    shares = basis = realised = Decimal(0)
    base_cost_basis = base_realised = Decimal(0)
    # A split applies before anything else dated the same day: a trade on the ex-date
    # is already in post-split shares.
    for side, qty, price, fees, rate, day, tid in sorted(rows, key=lambda r: (r[5], r[0] != "split", r[6])):
        qty, price, fees = Decimal(qty), Decimal(price), Decimal(fees)
        fx = Decimal(rate) if rate is not None else None
        if side == "split":
            shares *= qty                 # qty is numerator / denominator, basis is unchanged
        elif side in ("buy", "opening"):
            shares += qty
            cost = qty * price + fees
            basis += cost
            base_cost_basis = base_cost_basis + cost * fx if base_cost_basis is not None and fx is not None else None
        else:
            if qty > shares:
                raise ValueError("sell exceeds owned shares")
            removed = basis * qty / shares
            proceeds = qty * price - fees
            realised += proceeds - removed
            if base_cost_basis is not None and fx is not None:
                removed_base = base_cost_basis * qty / shares
                base_realised = base_realised + proceeds * fx - removed_base if base_realised is not None else None
                base_cost_basis -= removed_base
            else:
                base_cost_basis = base_realised = None
            basis -= removed
            shares -= qty
            if not shares:
                basis = base_cost_basis = Decimal(0)
    return {"shares": str(shares), "cost_basis": str(basis),
            "average_cost": str(basis / shares) if shares else "0",
            "realised": str(realised), "cost_base": str(base_cost_basis) if base_cost_basis is not None else None,
            "realised_base": str(base_realised) if base_realised is not None else None}


class _Book:
    def __init__(self):
        self.rows = []

    def step(self, *args):
        self.rows.append(args)

    def finalize(self):
        return json.dumps(_fold(self.rows))


def _binary(a, b, operation):
    if a is None or b is None:
        return None
    return str(operation(Decimal(a), Decimal(b)))


# Connections already set up, by id, holding the connection itself so a closed one's id can
# never be mistaken for a new one's.
_ready: dict[int, object] = {}


def connection():
    conn = db.connect()
    if conn is None:
        raise ValueError("portfolio storage unavailable; nothing recorded")
    if _ready.get(id(conn)) is conn:
        return conn
    # executescript commits implicitly: only initialise outside an owned transaction.
    if conn.in_transaction:
        return conn
    conn.create_aggregate("portfolio_book", 7, _Book)
    conn.create_function("decimal_mul", 2, lambda a, b: _binary(a, b, lambda x, y: x*y))
    conn.create_function("decimal_sub", 2, lambda a, b: _binary(a, b, lambda x, y: x-y))
    conn.create_function("portfolio_daily", 1, lambda day: json.dumps(daily(_conn=conn, day=day)[-1]))
    conn.executescript(SCHEMA)
    view = conn.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='v_positions'").fetchone()
    if view and (_VIEW_MARKER not in view[0] or f"||'/{BASE}'" not in view[0]):
        # Upgrade the derived view atomically; immutable facts remain intact.
        begin = SCHEMA.index('CREATE VIEW IF NOT EXISTS v_positions AS')
        end = SCHEMA.index('CREATE VIEW IF NOT EXISTS v_portfolio_daily AS')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DROP VIEW v_positions')
            conn.execute(SCHEMA[begin:end].strip())
    for table in _APPEND_ONLY:
        for verb in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS portfolio_{table}_{verb.lower()} "
                         f"BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'append-only portfolio history'); END")
    _ready[id(conn)] = conn
    return conn


def instruments(*, _conn=None) -> list[dict]:
    conn = _conn if _conn is not None else connection()
    return [dict(r) for r in conn.execute("SELECT * FROM instruments ORDER BY symbol")]


def ensure_instrument(instrument: dict, *, now=None):
    conn = connection()
    ts, day = lifelog.stamp(now)
    # A different exchange/currency for an existing symbol is an error, never an overwrite.
    row = conn.execute("SELECT * FROM instruments WHERE symbol=?", (instrument["symbol"],)).fetchone()
    if row and any(row[k] != instrument[k] for k in ("exchange", "currency", "price_key")):
        raise ValueError("instrument conflicts with the existing exchange/currency")
    conn.execute("INSERT OR IGNORE INTO instruments VALUES (?,?,?,?,?,?,?)",
                 tuple(instrument[k] for k in ("symbol", "exchange", "name", "currency", "price_key")) + (ts, day))


def fx_rate(currency: str, day: str, *, _conn=None) -> dict | None:
    if currency == BASE:
        return {"rate": "1", "local_date": day, "source": BASE, "url": ""}
    conn = _conn if _conn is not None else connection()
    row = conn.execute("SELECT * FROM fx WHERE pair=? AND local_date<=? AND local_date>=? "
                               "ORDER BY local_date DESC LIMIT 1",
                               (f"{currency}/{BASE}", day, (date.fromisoformat(day)-timedelta(days=7)).isoformat())).fetchone()
    return dict(row) if row else None


def save_fx(rows: list[dict], *, now=None):
    conn = connection()
    ts, _ = lifelog.stamp(now)
    with conn:
        for row in rows:
            day = date.fromisoformat(row["day"]).isoformat()
            value = str(number(row["rate"]))
            if not row["source"] or not row["url"]:
                raise ValueError("FX requires source and URL")
            conn.execute("INSERT OR IGNORE INTO fx(pair,local_date,rate,source,url,ts) VALUES (?,?,?,?,?,?)",
                         (row["pair"], day, value, row["source"], row["url"], ts))


def _book_rows(conn, symbol: str) -> list[tuple]:
    return [(r["side"], r["shares"], r["native_price"], r["native_fees"], None, r["traded_on"], r["id"])
            for r in conn.execute("SELECT * FROM live_trades WHERE instrument=?", (symbol,))]


def _validate(conn, symbol: str) -> None:
    """The complete dated history of one stock must still add up (no sale beyond holdings)."""
    try:
        _fold(_book_rows(conn, symbol))
    except ValueError as exc:
        raise ValueError(f"{symbol}: {exc}") from None


def _insert_trades(conn, rows: list[dict], run_id: str | None, ts: float, day: str) -> list[int]:
    """Inside an owned transaction: insert, then check each touched history still adds up."""
    ids = []
    for row in rows:
        i = row["instrument"]
        ensure_instrument(i, now=ts)
        old = conn.execute("SELECT id FROM trades WHERE dedup_key=?", (row["dedup_key"],)).fetchone()
        if old:
            ids.append(old["id"])
            continue
        if row["side"] == "opening" and conn.execute("SELECT 1 FROM live_trades WHERE instrument=?", (i["symbol"],)).fetchone():
            raise ValueError(f"{i['symbol']} already has trades; an opening balance would double-count it. Record the new buy/sell.")
        params = (i["symbol"], row["side"], row["shares"], row["price"], row["currency"], row["fees"],
                  row["native_price"], row["native_fees"], row.get("conversion_rate"), row["traded_on"],
                  row["quote"], run_id, row["dedup_key"], ts, day)
        cur = conn.execute("INSERT INTO trades(instrument,side,shares,price,currency,fees,native_price,native_fees,"
                           "conversion_rate,traded_on,quote,run_id,dedup_key,ts,local_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", params)
        # Validate the complete dated history, including backdated sells and splits.
        _validate(conn, i["symbol"])
        ids.append(cur.lastrowid)
    return ids


def record_trades(rows: list[dict], *, run_id: str | None, now=None) -> list[int]:
    conn = connection()
    ts, day = lifelog.stamp(now)
    with conn:
        conn.execute("BEGIN IMMEDIATE")       # serialize oversell and opening-balance checks
        return _insert_trades(conn, rows, run_id, ts, day)


def trade(trade_id: int) -> dict | None:
    row = connection().execute(
        "SELECT t.*, v.trade_id IS NOT NULL AS voided FROM trades t "
        "LEFT JOIN trade_voids v ON v.trade_id=t.id WHERE t.id=?", (trade_id,)).fetchone()
    return dict(row) if row else None


def void_trade(trade_id: int, quote: str, reason: str, *, run_id: str | None,
               replacements: list[dict] | None = None, now=None) -> list[int]:
    """Cancel one recorded trade, and record what replaces it, in one transaction.

    The trade row stays (history is never edited); a void row with his words takes it out of
    every figure. Refused when the corrected history would have a sale exceeding what was
    held -- the whole correction then records nothing. Returns the replacement trades' ids."""
    conn = connection()
    ts, day = lifelog.stamp(now)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if row is None:
            raise ValueError(f"no recorded trade #{trade_id}")
        if conn.execute("SELECT 1 FROM trade_voids WHERE trade_id=?", (trade_id,)).fetchone():
            raise ValueError(f"trade #{trade_id} is already cancelled")
        conn.execute("INSERT INTO trade_voids(trade_id,quote,reason,run_id,ts,local_date) VALUES (?,?,?,?,?,?)",
                     (trade_id, quote, lifelog.short(reason), run_id, ts, day))
        ids = _insert_trades(conn, replacements or [], run_id, ts, day)
        _validate(conn, row["instrument"])      # the corrected history must still add up
        if row["side"] == "split":
            conn.execute("UPDATE portfolio_splits SET state='pending',trade_id=NULL,decided_at=NULL "
                         "WHERE trade_id=?", (trade_id,))
    return ids


def positions(symbol: str = "") -> list[dict]:
    rows = connection().execute("SELECT * FROM v_positions" + (" WHERE symbol=?" if symbol else "") + " ORDER BY symbol",
                                (symbol,) if symbol else ())
    return [dict(r) for r in rows]


def save_mark(symbol: str, mark: dict, *, now=None) -> bool:
    conn = connection()
    ts, _ = lifelog.stamp(now)
    instrument = conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone()
    if not instrument or instrument["currency"] != mark["currency"]:
        raise ValueError("mark currency does not match instrument")
    day = date.fromisoformat(mark["day"]).isoformat()
    close = str(number(mark["close"]))
    if not mark["source"] or not mark["url"]:
        raise ValueError("mark requires source and URL")
    with conn:
        cur = conn.execute("INSERT OR IGNORE INTO marks(instrument,local_date,close,currency,source,url,ts) VALUES (?,?,?,?,?,?,?)",
                           (symbol, day, close, mark["currency"], mark["source"], mark["url"], ts))
    return bool(cur.rowcount)


# ---------------------------------------------------------------------------
# Splits the feed reports
# ---------------------------------------------------------------------------

def first_trade_day(symbol: str) -> str | None:
    return connection().execute("SELECT min(traded_on) FROM live_trades WHERE instrument=?", (symbol,)).fetchone()[0]


def checked_through(symbol: str) -> str | None:
    row = connection().execute("SELECT checked_through FROM portfolio_split_checks WHERE instrument=?",
                               (symbol,)).fetchone()
    return row[0] if row else None


def split_checked(symbol: str, through: str, *, now=None) -> None:
    conn = connection()
    ts, _ = lifelog.stamp(now)
    with conn:
        conn.execute("INSERT INTO portfolio_split_checks VALUES (?,?,?) ON CONFLICT(instrument) DO UPDATE "
                     "SET checked_through=max(checked_through,excluded.checked_through),ts=excluded.ts",
                     (symbol, through, ts))


def _ratio_of(split: dict) -> Decimal:
    return Decimal(str(split["numerator"])) / Decimal(str(split["denominator"]))


def note_split(symbol: str, split: dict, *, now=None) -> bool:
    """Record a split the feed reports, unless the ledger already has it. True when new.

    "Already has it" is a split trade within a week of the same date with the same ratio
    (the owner may have entered it himself, on the day he heard about it)."""
    conn = connection()
    ts, day = lifelog.stamp(now)
    ratio = _ratio_of(split)
    when = date.fromisoformat(split["day"])
    for t in conn.execute("SELECT * FROM live_trades WHERE instrument=? AND side='split'", (symbol,)):
        if abs((date.fromisoformat(t["traded_on"]) - when).days) <= 7 and Decimal(t["shares"]) == ratio:
            return False
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO portfolio_splits(instrument,split_date,ratio,numerator,denominator,"
            "source,url,ts,local_date) VALUES (?,?,?,?,?,?,?,?,?)",
            (symbol, when.isoformat(), format(ratio.normalize(), "f"), str(split["numerator"]),
             str(split["denominator"]), split["source"], split["url"], ts, day))
    return bool(cur.rowcount)


def splits(symbol: str = "", state: str = "") -> list[dict]:
    sql, args = "SELECT * FROM portfolio_splits WHERE 1=1", []
    if symbol:
        sql += " AND instrument=?"
        args.append(symbol)
    if state:
        sql += " AND state=?"
        args.append(state)
    return [dict(r) for r in connection().execute(sql + " ORDER BY split_date", args)]


def pending_split_day(symbol: str) -> str | None:
    """The earliest unconfirmed split: from this day on, the stock's figures are held."""
    return connection().execute("SELECT min(split_date) FROM portfolio_splits WHERE instrument=? AND state='pending'",
                                (symbol,)).fetchone()[0]


def mark_split_asked(symbol: str, split_date: str, approval_id: int | None, *, retry_at: float = 0) -> None:
    conn = connection()
    with conn:
        if approval_id:
            conn.execute("UPDATE portfolio_splits SET asked=1,approval_id=? WHERE instrument=? AND split_date=?",
                         (approval_id, symbol, split_date))
        else:
            conn.execute("UPDATE portfolio_splits SET next_ask_at=? WHERE instrument=? AND split_date=?",
                         (retry_at, symbol, split_date))


def confirm_split(symbol: str, split_date: str, quote: str, *, run_id: str | None, now=None) -> int:
    """Record the split as a trade (shares multiply, cost unchanged) and close the notice."""
    conn = connection()
    ts, day = lifelog.stamp(now)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute("SELECT * FROM portfolio_splits WHERE instrument=? AND split_date=?",
                         (symbol, split_date)).fetchone()
        if s is None:
            raise ValueError(f"no split of {symbol} on {split_date} is waiting")
        if s["state"] == "confirmed":
            return s["trade_id"]
        if s["state"] != "pending":
            raise ValueError(f"the {symbol} split on {split_date} was dismissed")
        instrument = dict(conn.execute("SELECT * FROM instruments WHERE symbol=?", (symbol,)).fetchone())
        [tid] = _insert_trades(conn, [{
            "instrument": instrument, "side": "split", "shares": s["ratio"], "price": "0",
            "currency": instrument["currency"], "fees": "0", "native_price": "0", "native_fees": "0",
            "conversion_rate": "1", "traded_on": split_date, "quote": quote,
            "dedup_key": f"split:{symbol}:{split_date}:{s['ts']}"}], run_id, ts, day)
        conn.execute("UPDATE portfolio_splits SET state='confirmed',trade_id=?,decided_at=? "
                     "WHERE instrument=? AND split_date=?", (tid, ts, symbol, split_date))
    return tid


def dismiss_split(symbol: str, split_date: str, *, now=None) -> None:
    conn = connection()
    ts, _ = lifelog.stamp(now)
    with conn:
        cur = conn.execute("UPDATE portfolio_splits SET state='dismissed',decided_at=? "
                           "WHERE instrument=? AND split_date=? AND state='pending'", (ts, symbol, split_date))
    if not cur.rowcount:
        raise ValueError(f"no split of {symbol} on {split_date} is waiting")


def split_factor(symbol: str, after: str, through: str) -> Decimal:
    """How many shares one share became through recorded splits dated in (after, through]."""
    factor = Decimal(1)
    for (ratio,) in connection().execute(
            "SELECT shares FROM live_trades WHERE instrument=? AND side='split' AND traded_on>? AND traded_on<=?",
            (symbol, after, through)):
        factor *= Decimal(ratio)
    return factor


# ---------------------------------------------------------------------------
# Theses
# ---------------------------------------------------------------------------

def current_theses(symbol: str = "", *, open_only=False) -> list[dict]:
    sql = "SELECT * FROM theses t WHERE NOT EXISTS (SELECT 1 FROM theses n WHERE n.previous_id=t.id)"
    args = []
    if symbol:
        sql += " AND instrument=?"
        args.append(symbol)
    if open_only:
        sql += " AND status IN ('open','revised')"
    return [dict(r) for r in connection().execute(sql + " ORDER BY id", args)]


def event(thesis_id: int, kind: str, reason: str, source: str, key: str, *, condition_id=None, now=None):
    conn = connection()
    ts, day = lifelog.stamp(now)
    conn.execute("INSERT OR IGNORE INTO thesis_events(thesis_id,condition_id,kind,reason,source,ts,local_date,event_key)"
                 " VALUES (?,?,?,?,?,?,?,?)", (thesis_id, condition_id, kind, lifelog.short(reason), source, ts, day, key))


def set_thesis(instrument: dict, why: str, source: str, quote: str, horizon: str,
               status="open", previous_id=None, *, now=None) -> int:
    conn = connection()
    ts, day = lifelog.stamp(now)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_instrument(instrument, now=ts)
        old = conn.execute("SELECT * FROM theses WHERE id=?", (previous_id,)).fetchone() if previous_id else None
        if not previous_id and conn.execute("SELECT 1 FROM theses t WHERE instrument=? AND source=? "
                                            "AND status IN ('open','revised','broken') AND NOT EXISTS "
                                            "(SELECT 1 FROM theses n WHERE n.previous_id=t.id)",
                                            (instrument["symbol"],source)).fetchone():
            raise ValueError("this author already has a current thesis; use revise_thesis with its id")
        if previous_id and (old is None or old["instrument"] != instrument["symbol"] or
                            old["source"] != source or conn.execute("SELECT 1 FROM theses WHERE previous_id=?", (previous_id,)).fetchone()):
            raise ValueError("revise/close the current thesis version for this instrument and author")
        if old and old["status"] in ("closed", "abandoned"):
            raise ValueError("thesis is already closed")
        root = (old["root_id"] or old["id"]) if old else None
        cur = conn.execute("INSERT INTO theses(instrument,root_id,previous_id,why,source,quote,horizon,status,ts,local_date)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (instrument["symbol"], root, previous_id, lifelog.short(why), source, quote, horizon, status, ts, day))
        tid = cur.lastrowid
        event(tid, "formed" if old is None else status, why, source, f"version:{tid}", now=ts)
    return tid


def conditions(thesis_id=None) -> list[dict]:
    sql = ("SELECT c.*,t.instrument,t.source AS thesis_source FROM thesis_conditions c JOIN theses t ON t.id=c.thesis_id")
    return [dict(r) for r in connection().execute(sql + (" WHERE c.thesis_id=?" if thesis_id else "") + " ORDER BY c.id",
                                                 (thesis_id,) if thesis_id else ())]


def active_conditions() -> list[dict]:
    """Conditions follow revisions of their root thesis, but stop when that author closes it."""
    conn = connection()
    latest = {t["root_id"] or t["id"]: t for t in current_theses()}
    result = []
    for c in conditions():
        t = conn.execute("SELECT * FROM theses WHERE id=?", (c["thesis_id"],)).fetchone()
        last = latest.get(t["root_id"] or t["id"])
        if c["state"] == "open" and last and last["status"] in ('open','revised'):
            result.append(c)
    return result


def trade_rows(symbol: str) -> list[dict]:
    """Every trade of one stock, cancelled ones included and marked `voided`."""
    return [dict(r) for r in connection().execute(
        "SELECT t.*, v.trade_id IS NOT NULL AS voided, v.reason AS void_reason FROM trades t "
        "LEFT JOIN trade_voids v ON v.trade_id=t.id WHERE t.instrument=? ORDER BY t.traded_on,t.id", (symbol,))]


def history(symbol: str) -> dict:
    conn = connection()
    return {
        "trades": trade_rows(symbol),
        "marks": [dict(r) for r in conn.execute("SELECT * FROM marks WHERE instrument=? ORDER BY local_date", (symbol,))],
        "theses": [dict(r) for r in conn.execute("SELECT * FROM theses WHERE instrument=? ORDER BY id", (symbol,))],
        "events": [dict(r) for r in conn.execute("SELECT e.* FROM thesis_events e JOIN theses t ON e.thesis_id=t.id "
                                               "WHERE t.instrument=? ORDER BY e.id", (symbol,))],
        "splits": [dict(r) for r in conn.execute("SELECT * FROM portfolio_splits WHERE instrument=? ORDER BY split_date", (symbol,))],
        "position_daily": position_daily(symbol),
    }


def position_daily(symbol: str) -> list[dict]:
    """Native-currency realised/unrealised history, including fully sold positions."""
    conn = connection()
    trades = [dict(t) for t in conn.execute("SELECT * FROM live_trades WHERE instrument=? ORDER BY traded_on,id",(symbol,))]
    held_from = pending_split_day(symbol)
    dates = sorted({t['traded_on'] for t in trades} | {r[0] for r in conn.execute("SELECT local_date FROM marks WHERE instrument=?",(symbol,))})
    result = []
    for day in dates:
        book = _fold([(t['side'],t['shares'],t['native_price'],t['native_fees'],None,t['traded_on'],t['id']) for t in trades if t['traded_on']<=day])
        mark = conn.execute("SELECT close FROM marks WHERE instrument=? AND local_date=?",(symbol,day)).fetchone()
        if held_from and day >= held_from:
            mark = None                   # an unrecorded split: this close is in other shares
        held = Decimal(book['shares'])
        unrealised = held*Decimal(mark[0])-Decimal(book['cost_basis']) if mark else Decimal(0) if held==0 else None
        result.append({'date':day,'shares':book['shares'],'realised':book['realised'],
                       'unrealised':str(unrealised) if unrealised is not None else None})
    return result


def daily(*, _conn=None, day=None) -> list[dict]:
    """Cash-flow aware history. Dates must have a close for EVERY held instrument.
    No old close is silently carried forward across a missing session.
    """
    conn = _conn if _conn is not None else connection()
    if day:
        days = [r[0] for r in conn.execute("SELECT DISTINCT local_date FROM marks WHERE local_date<=? ORDER BY local_date DESC LIMIT 2", (day,))][::-1]
    else:
        days = [r[0] for r in conn.execute("SELECT DISTINCT local_date FROM marks ORDER BY local_date")]
    trades = [dict(r) for r in conn.execute("SELECT t.*,i.currency AS native_currency FROM live_trades t "
                                          "JOIN instruments i ON i.symbol=t.instrument ORDER BY traded_on,id")]
    held_from = {r[0]: r[1] for r in conn.execute(
        "SELECT instrument,min(split_date) FROM portfolio_splits WHERE state='pending' GROUP BY instrument")}
    results = []
    previous = None
    previous_day = None
    previous_shares = {}
    for day in days:
        value = basis = realised = Decimal(0)
        complete = True
        gap = False
        current_shares = {}
        for i in instruments(_conn=conn):
            book_rows = []
            for t in trades:
                if t["instrument"] == i["symbol"] and t["traded_on"] <= day:
                    rate = fx_rate(i["currency"], t["traded_on"], _conn=conn)
                    book_rows.append((t["side"], t["shares"], t["native_price"], t["native_fees"],
                                      rate["rate"] if rate else None, t["traded_on"], t["id"]))
            b = _fold(book_rows)
            current_shares[i['symbol']] = Decimal(b['shares'])
            if previous_day and (current_shares[i['symbol']] or previous_shares.get(i['symbol'],0)):
                start = date.fromisoformat(previous_day)+timedelta(days=1)
                # Two expected closes are enough to establish that the prior mark
                # is not the preceding session. Bound calendar work across long gaps.
                end = min(date.fromisoformat(day),start+timedelta(days=14))
                gap |= len(sessions(i,start.isoformat(),end.isoformat()))>1
            if b["cost_base"] is None or b["realised_base"] is None:
                complete = False
                continue
            basis += Decimal(b["cost_base"])
            realised += Decimal(b["realised_base"])
            if Decimal(b["shares"]) == 0:
                continue
            pending = held_from.get(i["symbol"])
            m = None if pending and day >= pending else conn.execute(
                "SELECT * FROM marks WHERE instrument=? AND local_date=?", (i["symbol"], day)).fetchone()
            rate = fx_rate(i["currency"], day, _conn=conn)
            if not m or rate is None:
                complete = False
                continue
            value += Decimal(b["shares"]) * Decimal(m["close"]) * Decimal(rate["rate"])
        profit = value - basis + realised if complete else None
        change = profit - previous if profit is not None and previous is not None and not gap else None
        results.append({"date": day, "value_base": str(value) if complete else None,
                        "cost_base": str(basis) if complete else None,
                        "realised_base": str(realised) if complete else None,
                        "profit_base": str(profit) if profit is not None else None,
                        "change_base": str(change) if change is not None else None})
        previous = profit
        previous_day, previous_shares = day,current_shares
    return results

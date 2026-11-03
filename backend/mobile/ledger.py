"""The phone's Portfolio tab (slice 3 of the 15 October plan): the ledger, read-only.

What the portfolio tool keeps, as data for the app: each position with its exact figures
(decimal strings, never floats) from the last recorded close, the day's move, recent closes
for a small chart, the theses and their "wrong if" / "right if" conditions with how far the
last close is from each price level, a stock split waiting for his yes (and the approval
request that records it), the total in the base currency and its history. Everything comes from
backend/storage/portfolio.py, the same book `portfolio show` reads; nothing here computes a
value the ledger does not. Each part degrades on its own: an unreadable history leaves the
positions in place.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)

CLOSES_SHOWN = 30          # recent closes per position, for its small chart
HISTORY_SHOWN = 90         # marked sessions of the total, for the big one
TRADES_SHOWN = 6


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _s(value: Decimal | None, places: int | None = None) -> str | None:
    """A decimal as a plain string; rounded only where the number is derived (a percentage)."""
    if value is None:
        return None
    if places is not None:
        value = value.quantize(Decimal(1).scaleb(-places))
    return format(value.normalize() if places is None else value, "f")


def _pct(part: Decimal | None, whole: Decimal | None) -> str | None:
    if part is None or whole is None or whole == 0:
        return None
    return _s(part / whole * 100, 2)


def _closes(conn, symbol: str, through: str | None) -> list[list[str]]:
    """Closes since the last recorded split (older ones are in other shares) and before an
    unconfirmed one (newer ones are), newest last."""
    rows = conn.execute(
        "SELECT local_date, close FROM marks WHERE instrument=? "
        "AND local_date>=COALESCE((SELECT max(traded_on) FROM live_trades WHERE instrument=? AND side='split'),'0001-01-01') "
        "AND local_date<COALESCE((SELECT min(split_date) FROM portfolio_splits WHERE instrument=? AND state='pending'),'9999-12-31') "
        + ("AND local_date<=? " if through else "") + "ORDER BY local_date DESC LIMIT ?",
        (symbol, symbol, symbol, *((through,) if through else ()), CLOSES_SHOWN)).fetchall()
    return [[r[0], r[1]] for r in reversed(rows)]


def _position(p: dict, conn, pending: set[str]) -> dict:
    from backend.market import data
    from backend.storage import portfolio as store
    shares = _dec(p["shares"]) or Decimal(0)
    cost = _dec(p["cost_basis"])
    closes = _closes(conn, p["symbol"], p["mark_date"])
    close = _dec(p["last_mark"])
    prev = _dec(closes[-2][1]) if len(closes) >= 2 else None
    day_change = _pct(close - prev, prev) if close is not None and prev is not None else None
    trades = [t for t in store.trade_rows(p["symbol"]) if not t["voided"]][-TRADES_SHOWN:]
    return {
        "symbol": p["symbol"], "name": p["name"], "currency": p["currency"], "exchange": p["exchange"],
        "shares": _s(shares), "average_cost": _s(_dec(p["average_cost"])), "cost_basis": _s(cost),
        "close": _s(close), "close_date": p["mark_date"], "close_source": p["mark_source"] or "",
        "close_url": p["mark_url"] or "",
        "prev_close": _s(prev), "day_change_pct": day_change,
        "value": _s(_dec(p["value"])), "pnl": _s(_dec(p["unrealised"])),
        "pnl_pct": _pct(_dec(p["unrealised"]), cost),
        "value_base": _s(_dec(p["value_base"])), "pnl_base": _s(_dec(p["unrealised_base"])),
        "base_currency": data.BASE,
        "realised": _s(_dec(p["realised"]) or Decimal(0)),
        "held_for_split": p["symbol"] in pending,
        "closes": closes,
        "trades": [{"id": t["id"], "side": t["side"], "shares": _s(_dec(t["shares"])),
                    "price": _s(_dec(t["price"])) if t["side"] != "split" else None,
                    "currency": t["currency"], "traded_on": t["traded_on"]} for t in trades],
    }


def _distance(c: dict, close: Decimal | None, level: Decimal | None) -> str | None:
    """How far the last close is from a price level, in percent of the close: positive while
    the condition has not been reached, zero or negative once it has."""
    if close is None or level is None or close == 0:
        return None
    gap = close - level if c["kind"] == "price_below" else level - close
    return _s(gap / close * 100, 2)


def _theses(conn, closes: dict[str, Decimal | None]) -> list[dict]:
    from backend.brain import portfolio as P
    from backend.storage import lifelog, portfolio as store
    from backend.tools import vault
    today = lifelog.local_date()
    roots = {r[0]: r[1] or r[0] for r in conn.execute("SELECT id, root_id FROM theses")}
    by_root: dict[int, list[dict]] = {}
    for c in store.conditions():
        by_root.setdefault(roots.get(c["thesis_id"], c["thesis_id"]), []).append(c)
    out = []
    for t in store.current_theses(open_only=True):
        root = t["root_id"] or t["id"]
        conds = []
        for c in by_root.get(root, []):
            level = effective = None
            if c["kind"].startswith("price_") and c["level"]:
                level = _dec(c["level"])
                try:
                    effective = P._effective_level(c, today)
                except Exception:
                    effective = level
            conds.append({
                "id": c["id"], "kind": c["kind"], "direction": c["direction"],
                "level": _s(level), "effective_level": _s(effective) if effective != level else None,
                "criterion": c["criterion"], "due_date": c["due_date"], "state": c["state"],
                "distance_pct": _distance(c, closes.get(t["instrument"]), effective)
                if c["state"] == "open" and c["kind"].startswith("price_") else None,
                "linked": "forecast" if c["forecast_id"] else ("watch" if c["watch_id"] else ""),
            })
        last = conn.execute("SELECT kind, reason, local_date FROM thesis_events WHERE thesis_id IN "
                            "(SELECT id FROM theses WHERE id=? OR root_id=?) ORDER BY id DESC LIMIT 1",
                            (root, root)).fetchone()
        note = f"{t['instrument'].lower()}-thesis.md"
        try:
            has_note = vault._resolve("topics", note).is_file()
        except Exception:
            has_note = False
        out.append({
            "id": t["id"], "symbol": t["instrument"], "why": t["why"], "source": t["source"],
            "horizon": t["horizon"] or "", "status": t["status"], "since": t["local_date"],
            "conditions": conds,
            "last_event": {"kind": last[0], "reason": last[1], "date": last[2]} if last else None,
            "note": f"topics/{note}" if has_note else "",
        })
    return out


def _splits() -> list[dict]:
    from backend.storage import approvals, portfolio as store
    out = []
    for s in store.splits(state="pending"):
        held = store.positions(s["instrument"])
        shares = _dec(held[0]["shares"]) if held else None
        approval = None
        if s["approval_id"]:
            row = approvals.get(int(s["approval_id"]))
            if row and row.get("state") == approvals.PENDING and (row.get("expires_at") or 0) > time.time():
                approval = {"id": row["id"], "expires_at": row["expires_at"]}
        out.append({
            "symbol": s["instrument"], "split_date": s["split_date"],
            "ratio": f"{s['numerator']}-for-{s['denominator']}",
            "shares_before": _s(shares), "shares_after": _s(shares * _dec(s["ratio"])) if shares is not None else None,
            "source": s["source"], "approval": approval,
        })
    return out


def _total(held: list[dict]) -> dict:
    values = [_dec(p["value_base"]) for p in held]
    pnls = [_dec(p["pnl_base"]) for p in held]
    dates = {p["close_date"] for p in held}
    complete = bool(held) and all(v is not None for v in values) and len(dates) == 1
    out: dict = {"value_base": None, "pnl_base": None, "close_date": None, "complete": complete, "note": ""}
    if complete:
        out.update(value_base=_s(sum(values)), close_date=next(iter(dates)),
                   pnl_base=_s(sum(pnls)) if all(x is not None for x in pnls) else None)
    elif held:
        out["note"] = ("A split is waiting for your yes." if any(p["held_for_split"] for p in held)
                       else "Total unavailable: a close or a dated rate is missing, or the closes are from different days.")
    return out


def view(now: float | None = None) -> dict:
    """The ledger, as data for the app. Never raises."""
    from backend.market import data
    from backend.storage import portfolio as store
    out: dict = {"as_of": now or time.time(), "base_currency": data.BASE,
                 "positions": [], "watching": [], "closed": [],
                 "theses": [], "splits": [], "history": [], "total": None, "day": None}
    try:
        conn = store.connection()
        rows = store.positions()
        pending = {s["instrument"] for s in store.splits(state="pending")}
    except Exception as exc:
        log.warning("ledger: unreadable (%s)", exc)
        out["error"] = "The portfolio could not be read just now."
        return out
    closes: dict[str, Decimal | None] = {}
    for p in rows:
        closes[p["symbol"]] = _dec(p["last_mark"])
        try:
            shares = _dec(p["shares"])
            traded = conn.execute("SELECT 1 FROM live_trades WHERE instrument=? LIMIT 1", (p["symbol"],)).fetchone()
            if shares:
                out["positions"].append(_position(p, conn, pending))
            elif traded:
                out["closed"].append({"symbol": p["symbol"], "name": p["name"], "currency": p["currency"],
                                      "realised": _s(_dec(p["realised"]) or Decimal(0))})
            else:
                out["watching"].append({"symbol": p["symbol"], "name": p["name"], "currency": p["currency"],
                                        "close": _s(_dec(p["last_mark"])), "close_date": p["mark_date"]})
        except Exception as exc:
            log.warning("ledger: %s unreadable (%s)", p.get("symbol"), exc)
    held = out["positions"]
    total_value = sum((_dec(q["value_base"]) or Decimal(0)) for q in held)
    for p in held:
        # (of what is valued: a position without a euro value has no share of the total)
        p["weight_pct"] = _pct(_dec(p["value_base"]), total_value) if p["value_base"] is not None else None
    out["total"] = _total(held)
    try:
        out["theses"] = _theses(conn, closes)
    except Exception as exc:
        log.warning("ledger: theses unreadable (%s)", exc)
    try:
        out["splits"] = _splits()
    except Exception as exc:
        log.warning("ledger: splits unreadable (%s)", exc)
    if held:
        try:
            daily = store.daily()
            rows = [r for r in daily if r["value_base"] is not None][-HISTORY_SHOWN:]
            out["history"] = [{"date": r["date"], "value_base": _s(_dec(r["value_base"])),
                               "profit_base": _s(_dec(r["profit_base"]))} for r in rows]
            if daily and daily[-1]["value_base"] is not None:
                last = daily[-1]
                prior = _dec(daily[-2]["value_base"]) if len(daily) > 1 else None
                change = _dec(last["change_base"])
                out["day"] = {"date": last["date"], "change_base": _s(change),
                              "change_pct": _pct(change, prior) if change is not None else None}
        except Exception as exc:
            log.warning("ledger: history unreadable (%s)", exc)
    return out

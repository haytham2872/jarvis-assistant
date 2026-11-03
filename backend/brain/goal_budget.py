"""Goal money (docs/archive/GOALS-ROADMAP.md section 8, G1a): charges per session and per standalone call.

Amounts are integer micro-USD. `llm_calls` stays the evidence of observed usage; whether a
goal may spend is decided here, from `goal_charges`:

1. **Reserve before dispatch**, at V4.1 Flash's peak price with no cache discount: for a
   session, max calls x max input per call plus its output budget; for a standalone call
   (planner, checker), its input bound plus its max_tokens. The loop enforces both bounds,
   so the reservation is a real ceiling.
2. **Admit only if both caps hold**: the goal's own budget and the local time-day total across
   all goals, unresolved reservations from earlier days included. Checked and inserted in
   one short transaction with no network await inside it.
3. **Settle from the loop's own per-call usage** (RunResult.call_usage): OpenRouter's
   `usage.cost` when present, else the token counts at the peak cache-miss rate. A session
   that crashed or whose result was lost stays charged at its full reservation (`hold`).

New sessions, restarts and midnight never reset a goal's cap: the goal total is every charge
the goal ever made.
"""
from __future__ import annotations

import logging
import os
import time

from backend.storage import db, lifelog
from backend.storage import goals as store

log = logging.getLogger(__name__)

# V4.1 Flash list price at peak, $ per million tokens (usage._PRICING, verified 2026-09-21).
# No cache discount: a reservation must be a ceiling.
PEAK_IN_PER_M = 0.30
PEAK_OUT_PER_M = 1.20

WARN_FRACTION = 0.8           # The owner's decision (docs/archive/GOALS-ROADMAP.md section 1), sent once


def micro(usd: float) -> int:
    return int(round(float(usd) * 1_000_000))


def usd(microusd: int) -> float:
    return microusd / 1_000_000


def default_budget_microusd() -> int:
    """GOAL_DEFAULT_BUDGET_USD, $1 by default (a candidate set before measurement; G1a's live
    goal records what a goal actually costs)."""
    return micro(_env_float("GOAL_DEFAULT_BUDGET_USD", 1.0))


def daily_cap_microusd() -> int:
    """GOALS_DAILY_BUDGET_USD, $3 by default, across all goals per local day."""
    return micro(_env_float("GOALS_DAILY_BUDGET_USD", 3.0))


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, "") or default))
    except ValueError:
        return default


def ceiling(input_tokens: int, output_tokens: int, calls: int = 1) -> int:
    """Micro-USD for `calls` calls of at most `input_tokens` each plus `output_tokens` in all."""
    return micro((calls * input_tokens * PEAK_IN_PER_M + output_tokens * PEAK_OUT_PER_M) / 1e6)


def cost_of(call_usage: list[dict]) -> int:
    """What a run cost: reported cost where the provider gave one, else the table at peak."""
    total = 0.0
    for c in call_usage or []:
        if c.get("cost") is not None:
            total += float(c["cost"])
        else:
            total += ((c.get("input_tokens") or 0) * PEAK_IN_PER_M
                      + (c.get("output_tokens") or 0) * PEAK_OUT_PER_M) / 1e6
    return micro(total)


_SPENT_SQL = ("COALESCE(SUM(CASE state WHEN 'reserved' THEN reserved_microusd"
              " ELSE settled_microusd END), 0)")


def goal_spent(goal_id: str, conn=None) -> int:
    """Settled + held + still-reserved charges of this goal, ever."""
    conn = conn or db.connect()
    if conn is None:
        return 0
    return int(conn.execute(f"SELECT {_SPENT_SQL} FROM goal_charges WHERE goal_id=?",
                            (goal_id,)).fetchone()[0])


def day_spent(local_date: str | None = None, conn=None) -> int:
    """Today's charges across all goals, plus reservations still open from earlier days."""
    conn = conn or db.connect()
    if conn is None:
        return 0
    day = local_date or lifelog.stamp()[1]
    today = conn.execute(f"SELECT {_SPENT_SQL} FROM goal_charges WHERE local_date=?",
                         (day,)).fetchone()[0]
    older_open = conn.execute("SELECT COALESCE(SUM(reserved_microusd), 0) FROM goal_charges"
                              " WHERE local_date<>? AND state='reserved'", (day,)).fetchone()[0]
    return int(today) + int(older_open)


def reserve(goal_id: str, purpose: str, amount: int,
            session_id: str | None = None) -> tuple[str | None, str]:
    """(charge_id, "") when admitted, else (None, the reason). One short transaction, no
    network await: the check and the insert cannot be split by another writer."""
    conn = db.connect()
    if conn is None:
        return None, "the goal ledger is unavailable"
    try:
        conn.execute("BEGIN IMMEDIATE")
        g = conn.execute("SELECT budget_microusd, state FROM goals WHERE goal_id=?",
                         (goal_id,)).fetchone()
        if g is None or g["state"] in store.ENDED:
            conn.execute("ROLLBACK")
            return None, "the goal has ended"
        spent = goal_spent(goal_id, conn)
        if spent + amount > g["budget_microusd"]:
            conn.execute("ROLLBACK")
            return None, (f"the goal's budget (${usd(g['budget_microusd']):.2f}) would be exceeded: "
                          f"${usd(spent):.3f} spent or reserved, ${usd(amount):.3f} needed")
        day = day_spent(conn=conn)
        if day + amount > daily_cap_microusd():
            conn.execute("ROLLBACK")
            return None, (f"today's goal budget (${usd(daily_cap_microusd()):.2f} across all goals) "
                          f"would be exceeded: ${usd(day):.3f} already")
        charge_id = store.insert_charge(goal_id, purpose, amount, session_id, conn=conn)
        conn.execute("COMMIT")
        return charge_id, ""
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        log.warning("goal_budget: reservation failed (%s)", exc)
        return None, "the goal ledger could not record the reservation"


def settle(charge_id: str, amount: int) -> bool:
    conn = db.connect()
    if conn is None:
        return False
    with conn:
        cur = conn.execute("UPDATE goal_charges SET state='settled', settled_microusd=?,"
                           " settled_at=? WHERE charge_id=? AND state='reserved'",
                           (max(0, int(amount)), time.time(), charge_id))
    return cur.rowcount == 1


def hold(charge_id: str) -> bool:
    """A charge whose call crashed or whose result was lost: charged at the full reservation."""
    conn = db.connect()
    if conn is None:
        return False
    with conn:
        cur = conn.execute("UPDATE goal_charges SET state='held', settled_microusd="
                           "reserved_microusd, settled_at=? WHERE charge_id=? AND"
                           " state='reserved'", (time.time(), charge_id))
    return cur.rowcount == 1


def thresholds_crossed(goal_id: str) -> list[str]:
    """'warn' and/or 'cap' when the goal's spend has reached 80% / 100% of its budget.
    The caller sends each once (keyed goal events)."""
    g = store.get(goal_id)
    if g is None or g["budget_microusd"] <= 0:
        return []
    spent = goal_spent(g["goal_id"])
    out = []
    if spent >= WARN_FRACTION * g["budget_microusd"]:
        out.append("warn")
    if spent >= g["budget_microusd"]:
        out.append("cap")
    return out

"""P4: keeping the plan true when things change -- a skipped block, or a meeting that lands on one.

Two triggers, one way of answering:

  * **A block was skipped** (reported at the evening check-in, `planday.report`): find it a new slot
    later in the same week.
  * **A meeting now overlaps a block** (`scan`, every 30 minutes between 07:00 and 23:00, read-only):
    find the block a slot it does fit.

The new slot is chosen by the planner (`planner.find_slot`: his hours, the kind's own rules, keep-free
windows, the buffer, every other block) -- never by the model. What happens next depends on him:

  * the kind is pre-approved (`auto_move`, "you can move my gym yourself"): JARVIS makes the move with
    `week_plan change` and tells him what and why (undo puts it back);
  * otherwise the move becomes an **approval request** with Approve / Deny buttons (the P1 machinery,
    context "planner"): tapping Approve runs exactly the stored `week_plan change`.

Each proposal is recorded on the block first (a `reshuffle` event whose reason is the key of what
prompted it), so one clash or one skip is never put to him twice, whatever he answers or does not.
Work blocks are not reshuffled: a meeting inside a work block is left for him to say what to do.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backend.brain import planday
from backend.brain import planner as P
from backend.brain import weekplan as W
from backend.storage import lifelog, plans

log = logging.getLogger(__name__)

_MAX_PER_SCAN = 3                 # never a flood of questions from one look at the calendar


@dataclass
class Move:
    uid: str
    plan_id: int
    kind: str
    label: str                    # what he reads: "Move Thursday's gym (17:30-18:30) to Friday 18:00-19:00"
    why: str                      # "'Lunch with Sarah' now overlaps it" / "you skipped it"
    key: str                      # what prompted it; recorded so it is not asked twice
    args: dict = field(default_factory=dict)      # the week_plan change, or {} when nothing fits
    auto: bool = False


def _iso(d: date) -> str:
    return d.isoformat()


def proposed(uid: str, key: str) -> bool:
    """Has a move already been proposed for this block because of this?"""
    key = lifelog.short(key)
    return any(e["event"] == plans.RESHUFFLE and e["reason"] == key for e in plans.events(block_uid=uid))


def days_after(day: date, today: date) -> list[date]:
    """From `day` (or today, if that has passed) to the end of its week, in order: where a skipped
    block can still be done."""
    first = max(day, today)
    return [first + timedelta(days=k) for k in range((6 - first.weekday()) + 1)]


def days_around(day: date, today: date) -> list[date]:
    """The rest of the week (today on) ordered by how near each day is to `day`, that day first:
    where a block a meeting landed on can go."""
    week = [P.monday_of(day) + timedelta(days=k) for k in range(7)]
    return sorted((d for d in week if d >= today), key=lambda d: (abs((d - day).days), d))


def _slot(row: dict, plan: dict, busy: list, days: list[date], now) -> "P.Block | None":
    """The first day in `days` on which a block like `row` fits, and where."""
    rules = P.normalize_rules(plan["rules"])
    kind = row["kind"]
    spec = next((s for s in rules.blocks if s.kind == kind), None)
    start, end = W._eff(row)
    minutes = max(P.STEP, int((end - start) // 60))
    others = [W._block(r) for r in plan["blocks"] if r["status"] != plans.DELETED and r["uid"] != row["uid"]]
    allowed = spec.days if (spec and spec.days_given) else (0, 1, 2, 3, 4)
    for d in days:
        if d.weekday() not in allowed:
            continue
        slot = P.find_slot(kind, minutes, d, rules, busy, others, now=now)
        if slot is not None:
            return slot
    return None


def _move(row: dict, plan: dict, slot: "P.Block | None", why: str, key: str) -> Move:
    s_ts, e_ts = W._eff(row)
    old_day = P.local(W._dt(s_ts)).date()
    old_hm = f"{P.local(W._dt(s_ts)):%H:%M}"
    title = f"{P.day_name(old_day)}'s {row['kind']} ({planday._span(row)})"
    if slot is None:
        return Move(row["uid"], plan["id"], row["kind"], f"{title}: no free slot for it this week", why, key)
    ls, le = P.local(slot.start), P.local(slot.end)
    label = f"Move {title} to {P.day_name(ls.date())} {ls:%H:%M}-{le:%H:%M}"
    args = {"action": "change", "week": plan["week_start"], "kind": row["kind"], "day": _iso(old_day),
            "near": old_hm, "to_day": _iso(ls.date()), "start": f"{ls:%H:%M}",
            "reason": lifelog.short(why, 120)}
    return Move(row["uid"], plan["id"], row["kind"], label, why, key, args,
                auto=row["kind"] in planday.auto_move_kinds())


def _record(m: Move) -> None:
    plans.add_event(m.uid, m.plan_id, plans.RESHUFFLE, "system", reason=m.key)


# ---------------------------------------------------------------------------
# After a skip (called inside week_plan report)
# ---------------------------------------------------------------------------

def after_skip(skipped: list[dict], plan: dict, day: date, now: datetime,
               queue: list | None = None) -> list[str]:
    """Offer each skipped block (not work) a new slot this week. Pre-approved kinds are moved at
    once and reported; the rest are queued for an approval request: in `queue` when the caller
    brings its own (a phone tap), else planday's shared list (`planday.drain_proposals`).
    Returns the lines to tell JARVIS."""
    lines: list[str] = []
    monday = date.fromisoformat(plan["week_start"])
    todo = [r for r in skipped if r["kind"] != "work"
            and not proposed(r["uid"], f"skipped {_iso(day)}")]
    if not todo:
        return lines
    try:
        busy = W._fetch_busy(W._svc(), monday, W._plan_event_ids(plan["blocks"]))
    except W._Fail as fail:
        return [f"I could not read your calendar to find a new slot ({fail})."]
    today = P.local(now).date()
    for row in todo:
        key = f"skipped {_iso(day)}"
        days = days_after(day, today)
        if W._eff(row)[0] > now.timestamp():
            # skipped ahead of time (a phone tap): "not today", and its own slot, still free, is no
            # new slot (it was offered back to him as "move it to where it already is")
            days = [d for d in days if d != day]
        slot = _slot(row, plan, busy, days, now)
        m = _move(row, plan, slot, "you skipped it", key)
        _record(m)
        if slot is None:
            lines.append(f"{m.label}.")          # "Thursday's gym (17:30-18:30): no free slot for it this week."
        elif m.auto:
            result = W.run(W.Ask(**m.args), now)
            first = result.splitlines()[0] if result else ""
            if first.startswith("Moved"):
                # (no "say undo": the slot it left is already past, so undo could not put it back)
                lines.append(f"Rescheduled (you pre-approved moving {row['kind']}): {first}")
            else:
                lines.append(f"I tried to reschedule the skipped {row['kind']} but: {first or result}")
        else:
            (queue if queue is not None else planday._pending_proposals).append(
                {"args": m.args, "label": m.label, "why": m.why, "uid": m.uid})
    return lines


async def put_move(p: dict) -> dict | None:
    """Put one proposed move to him as an approval request: the stored request, or None when it
    could not be recorded."""
    from backend.tools import approval_flow, policy
    return await approval_flow.request(context="planner", tool="week_plan", args=p["args"], risk=policy.OWN,
                                       why=p["why"], label=p["label"])


async def request_move(p: dict) -> str:
    """Put one proposed move to him as an approval request. Returns what to tell JARVIS."""
    row = await put_move(p)
    if row is None:
        return _owner.say(f"I could not record the request ({p['label']}); tell «him» the option in words.")
    return _owner.say(f"Asked «him»: {p['label']} (request #{row['id']}; «he» taps Approve or Deny).")


# ---------------------------------------------------------------------------
# A meeting lands on a block (the 30-minute look, read-only)
# ---------------------------------------------------------------------------

def scan(now: datetime) -> list[Move]:
    """Blocks in the plan (this week on) that a busy event now overlaps, each with the slot it
    could move to. Reads the calendar and the plan; changes nothing. At most `_MAX_PER_SCAN`."""
    today = P.local(now).date()
    found: list[Move] = []
    svc = None
    for week_iso in plans.weeks_with_applied_plans(_iso(P.monday_of(today))):
        plan = plans.applied(week_iso)
        if plan is None:
            continue
        rows = [r for r in plan["blocks"]
                if r["status"] in (plans.SCHEDULED, plans.MOVED) and r["kind"] != "work"
                and W._eff(r)[0] > now.timestamp()]
        if not rows:
            continue
        svc = svc or W._svc()
        monday = date.fromisoformat(week_iso)
        busy = W._fetch_busy(svc, monday, W._plan_event_ids(plan["blocks"]))
        for row in rows:
            block = W._block(row)
            hit, key = None, ""
            for iv in busy:                       # the first overlapping event not already put to him
                if not iv.overlaps(block.start, block.end):
                    continue
                k = f"clash {iv.label} {P.local(iv.start):%Y-%m-%d %H:%M}"
                if not proposed(row["uid"], k):
                    hit, key = iv, k
                    break
            if hit is None:
                continue
            slot = _slot(row, plan, busy, days_around(P.local(block.start).date(), today), now)
            why = f"'{hit.label or 'an event'}' now overlaps it ({P.local(hit.start):%a %H:%M}-{P.local(hit.end):%H:%M})"
            found.append(_move(row, plan, slot, why, key))
            if len(found) >= _MAX_PER_SCAN:
                return found
    return found


async def handle(moves: list[Move], now: datetime, notify) -> list[str]:
    """Act on what `scan` found. Each proposal is recorded first. `notify(text)` messages him."""
    said: list[str] = []
    loop = asyncio.get_event_loop()
    for m in moves:
        _record(m)
        if not m.args:
            text = f"{m.label}, and {m.why}."
            if await notify(text):
                said.append(text)
            continue
        if m.auto:
            result = await loop.run_in_executor(None, W.run, W.Ask(**m.args), now)
            first = result.splitlines()[0] if result else ""
            text = (f"{first} {m.why[0].upper() + m.why[1:]}. You pre-approved moving {m.kind}; say undo to put it back."
                    if first.startswith("Moved") else
                    f"{m.why[0].upper() + m.why[1:]}, and I could not move it: {first or result}")
            if await notify(text):
                said.append(text)
        else:
            line = await request_move({"args": m.args, "label": m.label, "why": m.why, "uid": m.uid})
            said.append(line)
    return said

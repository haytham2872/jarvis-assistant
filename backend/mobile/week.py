"""Next plan, slice 4: the phone's week, using canonical plans and calendar operations."""
from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

from backend.brain import planner as P, weekplan as W
from backend.mobile import today
from backend.storage import plans
from backend.tools import calendar as cal, policy

log = logging.getLogger(__name__)


class Refused(ValueError):
    pass


class Missing(LookupError):
    pass


def day(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use a date such as 2026-10-19.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("That date does not exist.") from None


def clock(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Use a time such as 14:30.")
    if int(value[-2:]) % 15:
        raise ValueError("Choose a quarter-hour: :00, :15, :30 or :45.")
    return value


def _part(notes, label, read, fallback):
    try:
        return read()
    except Exception as exc:
        log.warning("mobile week: %s unreadable (%s)", label, exc)
        notes.append(f"{label} could not be read just now.")
        return fallback


def _block(row, outcomes):
    s, e = W._eff(row)
    return {"uid": row["uid"], "kind": row["kind"], "title": W._title(row["kind"]), "start": s, "end": e,
            "outcome": (outcomes.get(row["uid"]) or {}).get("outcome"), "status": row["status"]}


def view(start: str, days: int = 3, now: datetime | None = None) -> dict:
    """One bounded window. Each unreadable source leaves the others available."""
    first = day(start)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7:
        raise ValueError("Show between one and seven days.")
    now = now or datetime.now(timezone.utc)
    monday = P.monday_of(first).isoformat()
    notes = []
    plan = _part(notes, "Plan", lambda: plans.applied(monday), None)
    draft = _part(notes, "Proposal", lambda: plans.draft(monday), None)
    rows = [r for r in (plan or {}).get("blocks", []) if r["status"] != plans.DELETED]
    last_week = P.monday_of(first + timedelta(days=days - 1)).isoformat()
    if last_week != monday:
        following = _part(notes, "Following plan", lambda: plans.applied(last_week), None)
        rows += [r for r in (following or {}).get("blocks", []) if r["status"] != plans.DELETED]
    told = _part(notes, "Outcomes", lambda: plans.outcomes([r["uid"] for r in rows]), {})
    event_ids = {r["event_id"] for r in rows if r.get("event_id")}
    rules = _part(notes, "Rules", lambda: P.normalize_rules((plan or draft)["rules"]) if plan or draft else None, None)
    kinds = ([{"kind": r.kind, "minutes": r.minutes, "addable": True,
               "start": r.not_before, "end": r.not_after} for r in rules.blocks] if rules else [])
    if rules and rules.has_work:
        kinds.insert(0, {"kind": "work", "minutes": rules.work_min, "addable": False,
                         "start": rules.work_start, "end": rules.work_end})
    result = {"week": monday, "start": start, "now": now.timestamp(), "plan_id": plan["id"] if plan else None,
              "days": [], "rules": {"kinds": kinds, "hours": {"start": rules.not_before if rules else "07:00",
                                                               "end": rules.not_after if rules else "23:00"}},
              "draft": None, "notes": notes}
    result["rules"]["buffer"] = rules.buffer if rules else 0
    for i in range(days):
        d = first + timedelta(days=i)
        own = [_block(r, told) for r in rows if P.local(W._dt(W._eff(r)[0])).date() == d]
        events, why = _part(notes, "Calendar", lambda: today._calendar(d, week=True, plan_events=event_ids), (None, "Calendar unavailable."))
        counts = {"planned": len(own), **{o: sum(b["outcome"] == o for b in own) for o in ("done", "partly", "skipped")}}
        result["days"].append({"date": d.isoformat(), "label": f"{P.day_name(d)[:3]} {d.day}",
                              "blocks": own, "events": events or [], "events_note": why, "counts": counts})
    if draft:
        result["draft"] = {"plan_id": draft["id"], "week": monday,
                           "blocks": [_block(r, {}) for r in draft["blocks"] if r["status"] != plans.DELETED]}
    return result


def _run(ask: W.Ask, now: datetime) -> str:
    text = W.run(ask, now)
    if text.startswith("Error:"):
        raise Refused(text[6:].strip())
    return text


def _owned(rows: list, week: str) -> None:
    """Canonical undo/apply may patch or delete several rows: check their ownership too."""
    for row in rows:
        if not row.get("event_id") or row["status"] == plans.DELETED:
            continue
        event = cal.read_event(W._svc(), row["event_id"])
        private = ((event or {}).get("extendedProperties") or {}).get("private") or {}
        if event is not None and (not cal.is_jarvis_made(event) or private.get(cal.BLOCK_KEY) != row["uid"]
                                  or private.get(cal.PLAN_KEY) != week):
            raise Refused("A calendar event is no longer a block I made. It was left alone; refresh the week.")


def _working(week: str) -> dict:
    applied = plans.applied(week)
    if not applied:
        raise Refused("Apply a week proposal before editing its blocks.")
    if plans.draft(week):
        raise Refused("A proposal is waiting for this week. Apply it before editing blocks.")
    return applied


def _current(week: str, expected) -> dict:
    current = _working(week)
    if expected is not None and expected != current["id"]:
        raise Refused("The week changed. Refresh before moving or adding a block.")
    W._guarded_sync(W._svc(), day(week))
    return _working(week)


def _answer(text: str, start: str, days: int, now: datetime) -> dict:
    # A write may have partly succeeded even when the calendar reports a failure.
    # Never leave the phone showing its optimistic position or an old calendar cache.
    today.clear_calendar_cache()
    warning = (text[text.index("Not done:"):] if "Not done:" in text else
               "\n".join(line for line in text.splitlines() if any(why in line.lower() for why in
                        ("could not", "so i left", "so they were left"))))
    return {"text": warning or ("Already at that time." if text.startswith("That is where") else "Week updated."),
            "warning": warning, "week": view(start, days, now)}


def action(name: str, body: dict, uid: str = "", now: datetime | None = None) -> dict:
    """Called only after route authentication. Keep reads and canonical writes under one lock."""
    now = now or datetime.now(timezone.utc)
    first = day(body.get("view_start") or body.get("week") or body.get("day"))
    count = body.get("days", 3)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 7:
        raise ValueError("Show between one and seven days.")
    monday = P.monday_of(first).isoformat()
    caller = policy.current_caller.set(policy.APPROVED)
    quote = policy.call_quote.set(f"phone: {name} week block {uid} {body}")
    try:
        with W._LOCK:
            if name == "apply":
                week = P.monday_of(day(body.get("week"))).isoformat()
                proposal = plans.draft(week)
                expected = body.get("plan_id")
                if expected is not None and (not proposal or proposal["id"] != expected):
                    raise Refused("That proposal changed. Refresh before applying it.")
                _owned((plans.applied(week) or {}).get("blocks", []), week)
                text = _run(W.Ask(action="apply", week=week, plan_id=proposal["id"] if proposal else 0), now)
            elif name == "undo":
                current = _working(monday)
                if body.get("plan_id") != current["id"]:
                    raise Refused("The week changed since that move. Refresh before undoing it.")
                _owned(current["blocks"], monday)
                text = _run(W.Ask(action="undo", week=monday), now)
            elif name == "move":
                current = _current(monday, body.get("plan_id"))
                row = next((r for r in current["blocks"] if r["uid"] == uid and r["status"] != plans.DELETED), None)
                if row is None:
                    raise Missing("That planned block is no longer in this week.")
                target = day(body.get("day"))
                if P.monday_of(target).isoformat() != monday:
                    raise Refused("Move blocks within the selected week.")
                s = P.local(W._dt(W._eff(row)[0]))
                if row.get("event_id"):
                    event = cal.read_event(W._svc(), row["event_id"])
                    private = ((event or {}).get("extendedProperties") or {}).get("private") or {}
                    if event is None or not cal.is_jarvis_made(event) or private.get(cal.BLOCK_KEY) != uid:
                        raise Refused("That calendar event is no longer a block I made. It was left alone.")
                start = clock(body.get("start"))
                rules = P.normalize_rules(current["rules"])
                new_start = P.at(target, start)
                new_end = P.plus(new_start, int((W._eff(row)[1] - W._eff(row)[0]) / 60))
                lo, hi = (rules.work_start, rules.work_end) if row["kind"] == "work" and rules.has_work else (rules.not_before, rules.not_after)
                if new_start < P.at(target, lo) or new_end > P.at(target, hi):
                    raise Refused(f"That block would fall outside {lo}-{hi}.")
                text = _run(W.Ask(action="change", week=monday, kind=row["kind"], day=s.date().isoformat(),
                                  near=f"{s:%H:%M}", to_day=target.isoformat(), start=start), now)
            elif name == "add":
                current = _current(monday, body.get("plan_id"))
                target, start = day(body.get("day")), clock(body.get("start"))
                if P.monday_of(target).isoformat() != monday:
                    raise Refused("Add a block within the selected week.")
                minutes, kind = body.get("minutes"), body.get("kind")
                if isinstance(minutes, bool) or not isinstance(minutes, int) or not 15 <= minutes <= 600 or minutes % 15:
                    raise ValueError("Choose a length from 15 to 600 minutes, in quarter-hours.")
                rules = P.normalize_rules(current["rules"])
                spec = next((b for b in rules.blocks if b.kind == kind), None)
                if spec is None:
                    raise Refused("Choose one of this week's named block kinds. Work is filled by its weekly rules.")
                block = P.Block(kind, P.at(target, start), P.plus(P.at(target, start), minutes))
                busy = W._fetch_busy(W._svc(), target - timedelta(days=target.weekday()), W._plan_event_ids(current["blocks"]))
                reason = P.check_block(block, [W._block(r) for r in current["blocks"] if r["status"] != plans.DELETED],
                                       busy, rules, now=now, ignore_days=True)
                if reason:
                    raise Refused(reason)
                if P.local(block.end).date() != target:
                    raise Refused("The block must end on the same day.")
                _run(W.Ask(action="add", week=monday, rules={"blocks": [{"kind": kind, "count": 1, "minutes": minutes,
                         "days": P.DAYS[target.weekday()], "not_before": start, "not_after": f"{P.local(block.end):%H:%M}"}]}), now)
                proposal = plans.draft(monday)
                old_uids = {r["uid"] for r in current["blocks"]}
                added = [r for r in (proposal or {}).get("blocks", []) if r["uid"] not in old_uids and r["kind"] == kind
                         and W._eff(r) == (block.start.timestamp(), block.end.timestamp())]
                if not added:
                    if proposal:
                        plans.discard(proposal["id"])
                    raise Refused("That block could not fit at the chosen time. Nothing was added.")
                # A one-off slot must not turn the planner's standing hours into that slot,
                # or refill/move other work. Preserve the applied plan and its rules.
                saved_rules = deepcopy(current["rules"])
                for rule in saved_rules.get("blocks", []):
                    if rule["kind"] == kind:
                        rule["count"] += 1
                plans.discard(proposal["id"])
                pid = plans.create_version(monday, saved_rules, [W._carry(r) for r in current["blocks"]] +
                                           [W._carry(r) for r in added], created_by="owner", note=f"phone: added {kind}",
                                           parent_id=current["id"])
                if pid is None:
                    raise Refused("The new block could not be saved. Nothing was added.")
                text = _run(W.Ask(action="apply", week=monday, plan_id=pid), now)
            else:
                raise ValueError("Unknown week action.")
            return _answer(text, first.isoformat(), count, now)
    finally:
        today.clear_calendar_cache()
        policy.call_quote.reset(quote)
        policy.current_caller.reset(caller)

"""P3: `week_plan` -- the owner's working week, planned by code and kept in his calendar.

**The model says what he wants; code does the exact part.** the owner says "organise my working
hours next week", "add gym four times, not after 20:00", "move Thursday's gym to the evening",
"undo". The model turns that into typed rules and named blocks; `planner.py` places everything
around his real events; this module remembers it (`storage/plans.py`) and keeps the calendar
equal to it. The model never types a time into the calendar for a plan.

Actions:
  propose  plan a week from rules. Nothing is written to the calendar; a draft is stored.
  add      more blocks for a week that has a plan. Blocks already standing do not move; work is
           refilled around the new ones.
  apply    write the latest proposal into the calendar (the only action that needs his word).
  change   one block: a new start / end / length / part of the day / day.   remove   one block.
  undo     take back the last change.   show   the plan with statuses.   sync   notice blocks he
           moved or deleted himself.
  report   (P4) what became of the day's blocks, in his words: done, partly, skipped, moved,
           unknown. Matched to real blocks by code; see planday.py.

Guarantees, each pinned by a test:
  * **Only events it made are ever touched**, and only after checking the live event still
    carries this block's marker and is where it was left. A block the owner moved or deleted in his
    calendar is recorded as *his* change (`seen_moved` / `seen_deleted`) and left alone; a change
    or an undo never overwrites it.
  * **A retried apply leaves no duplicates.** Every block's event id is derived from (week, block,
    generation): writing it twice is a 409 that is recognised, not a second event.
  * **Every version is kept.** A change is a new version with all its blocks; undo applies the
    inverse of the last change onto the current state, so it does not undo things he did himself.
  * **A block that cannot stand is reported with the reason**, never dropped or bent.

All calendar and database work is synchronous and runs in one worker thread per call, under a lock.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from backend.brain import planner as P
from backend.storage import plans
from backend.tools import calendar as cal
from backend.tools import gateway as _gateway

log = logging.getLogger(__name__)

ACTIONS = ("propose", "add", "apply", "change", "remove", "undo", "show", "sync", "report")
_LOCK = threading.RLock()
_SLACK = 1.0                      # seconds: two times this close are the same moment
_MAX_LISTED = 40


class _Fail(Exception):
    """A request that cannot be done; the message says why and what to change."""


@dataclass
class Ask:
    action: str
    week: str = ""
    rules: object = None
    plan_id: int = 0
    kind: str = ""
    day: str = ""
    to_day: str = ""
    near: str = ""
    start: str = ""
    end: str = ""
    minutes: int = 0
    prefer: str = ""
    reason: str = ""
    outcomes: object = None          # report: [{kind, outcome, reason, near}] -- what became of the day's blocks
    said: str = ""                   # report: his words, short


@dataclass
class Report:
    created: int = 0
    moved: int = 0
    removed: int = 0
    kept: int = 0
    failed: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{n} {word}" for n, word in ((self.created, "created"), (self.moved, "moved"),
                                              (self.removed, "removed")) if n]
        if self.kept:
            parts.append(f"{self.kept} unchanged")
        return ", ".join(parts) if parts else "nothing needed changing"

    def lines(self) -> list:
        out = [f"Not done: {f}" for f in self.failed[:_MAX_LISTED]]
        out += [n for n in self.notes[:_MAX_LISTED]]
        return out


# ---------------------------------------------------------------------------
# Time and rows
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def _iso(d: date) -> str:
    return d.isoformat()


def _title(kind: str) -> str:
    return kind.replace("-", " ").replace("_", " ").title()


def _eff(row: dict) -> tuple:
    """Where the block really is: where the owner moved it, else where it was planned."""
    if row.get("live_start") is not None and row.get("live_end") is not None:
        return row["live_start"], row["live_end"]
    return row["start_at"], row["end_at"]


def _planned(row: dict) -> tuple:
    return row["start_at"], row["end_at"]


def _block(row: dict, *, planned: bool = False) -> P.Block:
    s, e = _planned(row) if planned else _eff(row)
    return P.Block(row["kind"], _dt(s), _dt(e))


def _carry(row: dict, **over) -> dict:
    """A stored row as the dict `plans.create_version` takes, keeping identity and state."""
    out = {"uid": row["uid"], "kind": row["kind"], "start": row["start_at"], "end": row["end_at"],
           "event_id": row.get("event_id"), "status": row.get("status") or plans.PLANNED,
           "live_start": row.get("live_start"), "live_end": row.get("live_end")}
    out.update(over)
    out["start_at"], out["end_at"] = out["start"], out["end"]      # readable by the same helpers
    return out


def _fresh(block: P.Block) -> dict:
    return {"kind": block.kind, "start": block.start.timestamp(), "end": block.end.timestamp()}


def _label(row: dict) -> str:
    s, e = _planned(row)
    ls, le = P.local(_dt(s)), P.local(_dt(e))
    return f"{P.day_name(ls.date())} {ls:%H:%M}-{le:%H:%M} {row['kind']}"


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= _SLACK


def _close(span_a: tuple, span_b: tuple) -> bool:
    return _same(span_a[0], span_b[0]) and _same(span_a[1], span_b[1])


def _week_window(monday: date):
    return P.at(monday, "00:00"), P.at(monday + timedelta(days=7), "00:00")


def _resolve_week(spec: str, now: datetime, *, fallback: str):
    """Monday of the week `spec` names ('this', 'next', a date), else of the plan he is talking
    about (`fallback` 'plan') or next week (`fallback` 'next'). None when there is no such plan."""
    today = P.local(now).date()
    text = (spec or "").strip().lower()
    if text in ("this", "this week", "current"):
        return P.monday_of(today)
    if text in ("next", "next week"):
        return P.monday_of(today) + timedelta(days=7)
    if text:
        try:
            return P.monday_of(date.fromisoformat(text))
        except ValueError:
            raise _Fail("week must be 'this', 'next' or a date (YYYY-MM-DD)") from None
    if fallback == "next":
        return P.monday_of(today) + timedelta(days=7)
    newest = plans.newest_week(_iso(P.monday_of(today)))
    return date.fromisoformat(newest) if newest else None


def _week_of(a: Ask, now: datetime, *, fallback: str) -> date:
    if a.plan_id:
        plan = plans.get(int(a.plan_id))
        if plan is None:
            raise _Fail(f"there is no plan #{a.plan_id}")
        return date.fromisoformat(plan["week_start"])
    monday = _resolve_week(a.week, now, fallback=fallback)
    if monday is None:
        raise _Fail(_owner.say("there is no plan yet; propose one first (the hours «he» works, and what to add)"))
    return monday


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------

def _svc():
    try:
        return cal._get_service()
    except Exception as exc:
        raise _Fail(f"the calendar is not available ({exc}); nothing was planned or changed") from None


def _plan_event_ids(*plan_lists) -> set:
    return {r["event_id"] for rows in plan_lists for r in rows if r.get("event_id")}


def _fetch_busy(svc, monday: date, own_ids: set) -> list:
    """The week's real busy time as planner intervals, without the plan's own events."""
    lo, hi = _week_window(monday)
    events, truncated = cal.fetch_events(svc, lo, hi)
    if truncated:
        raise _Fail("that week has more than 250 events, too many for me to plan around safely")
    week = _iso(monday)
    keep = []
    for ev in events:
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        if ev.get("id") in own_ids or (priv.get(cal.PLAN_KEY) == week and priv.get(cal.BLOCK_KEY)):
            continue
        keep.append(ev)
    return [P.Interval(s, e, label) for s, e, label, _id in cal.busy_windows(keep)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _note(row: dict, draft: bool) -> str:
    st = row["status"]
    if st == plans.DELETED:
        return "  (you deleted it from your calendar)"
    if st == plans.MOVED:
        s, _ = _planned(row)
        return f"  (you moved it here from {P.local(_dt(s)):%H:%M})"
    if st == plans.FAILED:
        return "  (not in the calendar: it could not be written)"
    if st == plans.PLANNED and not draft:
        return "  (not written to the calendar yet)"
    return ""


def _render(plan: dict, *, only: date | None = None) -> list:
    draft = plan["state"] == plans.DRAFT
    by_day: dict = {}
    for r in plan["blocks"]:
        s, e = _planned(r) if r["status"] == plans.DELETED else _eff(r)
        by_day.setdefault(P.local(_dt(s)).date(), []).append((s, e, r))
    lines = []
    for d in sorted(by_day):
        if only is not None and d != only:
            continue
        lines.append(P.day_label(d))
        for s, e, r in sorted(by_day[d], key=lambda item: (item[0], item[2]["kind"])):
            lines.append(f"  {P.local(_dt(s)):%H:%M}-{P.local(_dt(e)):%H:%M} {r['kind']}"
                         f"{_note(r, draft)}")
    return lines


def _header(plan: dict) -> str:
    monday = date.fromisoformat(plan["week_start"])
    where = ("in your calendar" if plan["state"] == plans.APPLIED
             else "a proposal: nothing in your calendar yet")
    return f"Plan #{plan['id']} (version {plan['version']}) for the week of {P.day_label(monday)}, {where}:"


def _draft_text(plan_id: int, unplaced, busy_count: int, verb: str) -> str:
    plan = plans.get(plan_id)
    out = [f"{verb}.", _header(plan)] + _render(plan)
    for u in unplaced:
        out.append(f"Could not place {u.wanted - u.placed} of {u.wanted} {u.kind} "
                   f"({u.minutes} min each): {u.reason}")
    out.append(_owner.say(f"Kept clear of {busy_count} event(s) already in «his» calendar."))
    out.append(_owner.say(f"Nothing is in the calendar yet. Show «him» this. To write it, call apply "
                f"(plan_id={plan_id}), copying the words in which «he» asked for it into quote; "
                "without them it becomes a request «he» approves."))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Building versions
# ---------------------------------------------------------------------------

def _adopt(blocks, pool_rows: list) -> list:
    """New placements as version rows, reusing the identity (and calendar event) of an
    identical block from the plan they replace."""
    pool = {(r["kind"], r["start_at"], r["end_at"]): r for r in pool_rows}
    out = []
    for b in blocks:
        row = pool.pop((b.kind, b.start.timestamp(), b.end.timestamp()), None)
        out.append(_carry(row) if row else _fresh(b))
    return out


def _store(monday: date, rules: P.Rules, rows: list, *, note: str, parent: dict | None) -> int:
    pid = plans.create_version(_iso(monday), rules.as_dict(), rows, created_by="owner", note=note,
                               parent_id=parent["id"] if parent else None)
    if pid is None:
        raise _Fail("I could not record the plan (storage is unavailable), so nothing was planned")
    return pid


def _propose(a: Ask, now: datetime) -> str:
    rules = P.normalize_rules(a.rules)
    monday = _week_of(a, now, fallback="next")
    iso = _iso(monday)
    svc = _svc()
    applied = plans.applied(iso)
    old_rows = applied["blocks"] if applied else []
    busy = _fetch_busy(svc, monday, _plan_event_ids(old_rows))
    placement = P.plan_week(monday, rules, busy, now=now)
    rows = _adopt(placement.blocks, old_rows)
    pid = _store(monday, rules, rows, note=f"proposed: {len(rows)} blocks", parent=plans.latest_live(iso))
    return _draft_text(pid, placement.unplaced, len(busy), "Proposed")


def _add(a: Ask, now: datetime) -> str:
    monday = _resolve_week(a.week, now, fallback="plan") if not a.plan_id else _week_of(a, now, fallback="plan")
    if monday is None:
        return _propose(a, now)                  # nothing to add to: this is a first plan
    iso = _iso(monday)
    current = plans.latest_live(iso)
    if current is None:
        return _propose(a, now)
    old_rules = P.normalize_rules(current["rules"]) if current["rules"] else None
    if old_rules is None:
        raise _Fail("the plan has no rules stored; propose a new plan instead")
    merged, extra = P.merge_rules(old_rules, a.rules)
    svc = _svc()
    rows_now = current["blocks"]
    busy = _fetch_busy(svc, monday, _plan_event_ids(rows_now))
    now_ts = now.timestamp()
    # Everything except work that is still to come stands where it is; work is refilled.
    kept = [r for r in rows_now if r["kind"] != "work" or r["start_at"] < now_ts]
    pool = [r for r in rows_now if r not in kept]
    taken = [_block(r) for r in kept if r["status"] != plans.DELETED]
    notes = []
    if extra is None:
        placement = P.Placement(monday, (), ())
    else:
        placement = P.plan_week(monday, extra, busy, now=now, taken=taken)
    rows = [_carry(r) for r in kept] + _adopt(placement.blocks, pool)
    # Existing blocks that the new rules now forbid are not moved; they are named.
    stand = [_block(r) for r in kept if r["status"] != plans.DELETED]
    for r in kept:
        if r["status"] == plans.DELETED:
            continue
        problem = P.check_block(_block(r), [b for b in stand if b.key != _block(r).key], busy, merged,
                                ignore_days=True)
        if problem and not problem.startswith("that is in the past"):
            notes.append(f"{_label(r)} no longer fits the rules: {problem} (use change or remove)")
    pid = _store(monday, merged, rows, note="added: " + (", ".join(
        f"{b.kind} x{b.count}" for b in (extra.blocks if extra else ())) or "rules updated"), parent=current)
    text = _draft_text(pid, placement.unplaced, len(busy), "Added to")
    return text + ("\n" + "\n".join(notes) if notes else "")


# ---------------------------------------------------------------------------
# Writing to the calendar
# ---------------------------------------------------------------------------

def _create(svc, iso: str, plan_id: int, row: dict, others: list, busy: list, rules: P.Rules,
            now: datetime, actor: str, rep: Report) -> None:
    uid, start, end = row["uid"], _dt(row["start_at"]), _dt(row["end_at"])
    block = P.Block(row["kind"], start, end)
    problem = P.check_block(block, [b for b in others if b.key != block.key], busy, rules, now=now,
                            ignore_days=True)
    if problem:
        plans.add_event(uid, plan_id, plans.WRITE_FAILED, actor, new=_planned(row), reason=problem)
        plans.set_block(plan_id, uid, status=plans.FAILED, event_id=None, live_start=None, live_end=None)
        rep.failed.append(f"{_label(row)}: {problem}")
        return
    body = None
    for attempt in range(2):
        eid = cal.event_id_for(iso, uid, plans.generation(uid))
        body = cal.block_body(_title(row["kind"]), start, end, week=iso, uid=uid)
        try:
            outcome = cal.write_event(svc, eid, body)
        except cal.EventIdTaken:
            plans.add_event(uid, plan_id, plans.RESTORED, "system",
                            reason="the event id was reserved by a deleted event; using a new one")
            continue
        except Exception as exc:
            plans.add_event(uid, plan_id, plans.WRITE_FAILED, actor, new=_planned(row), reason=str(exc))
            plans.set_block(plan_id, uid, status=plans.FAILED, event_id=None, live_start=None, live_end=None)
            rep.failed.append(f"{_label(row)}: the calendar refused it ({exc})")
            return
        plans.add_event(uid, plan_id, plans.CREATED, actor, new=_planned(row),
                        reason="" if outcome == "created" else f"found already there ({outcome})")
        plans.set_block(plan_id, uid, event_id=eid, status=plans.SCHEDULED, live_start=None, live_end=None)
        rep.created += 1
        return
    plans.set_block(plan_id, uid, status=plans.FAILED, event_id=None)
    rep.failed.append(f"{_label(row)}: no free event id")


def _move(svc, iso: str, plan_id: int, row: dict, old: dict | None, others: list, busy: list,
          rules: P.Rules, now: datetime, actor: str, rep: Report) -> None:
    uid, eid = row["uid"], row["event_id"]
    expected = _eff(old) if old is not None else _eff(row)
    event = cal.read_event(svc, eid)
    if event is None:                            # gone since the last look: he deleted it
        plans.add_event(uid, plan_id, plans.SEEN_DELETED, "owner", old=expected,
                        reason="deleted in the calendar")
        plans.set_block(plan_id, uid, event_id=None, status=plans.PLANNED, live_start=None, live_end=None)
        row = dict(row, event_id=None, status=plans.PLANNED)
        _create(svc, iso, plan_id, row, others, busy, rules, now, actor, rep)
        return
    span = cal.event_span(event)
    live = (span[0].timestamp(), span[1].timestamp()) if span else None
    if live is None or not _close(live, expected):
        if live is not None:
            plans.add_event(uid, plan_id, plans.SEEN_MOVED, "owner", old=expected, new=live,
                            reason="moved in the calendar")
            plans.set_block(plan_id, uid, status=plans.MOVED, live_start=live[0], live_end=live[1])
        rep.notes.append(f"{_label(row)}: you moved it in your calendar since I set it up, "
                         "so I left it where you put it.")
        return
    block = P.Block(row["kind"], _dt(row["start_at"]), _dt(row["end_at"]))
    problem = P.check_block(block, [b for b in others if b.key != block.key], busy, rules, now=now,
                            ignore_days=True)
    if problem:
        plans.add_event(uid, plan_id, plans.WRITE_FAILED, actor, old=expected, new=_planned(row),
                        reason=problem)
        plans.set_block(plan_id, uid, status=plans.FAILED, live_start=live[0], live_end=live[1])
        rep.failed.append(f"{_label(row)}: {problem} (it stays at {P.local(_dt(live[0])):%H:%M})")
        return
    try:
        cal.patch_event(svc, eid, start=block.start, end=block.end)
    except Exception as exc:
        plans.add_event(uid, plan_id, plans.WRITE_FAILED, actor, old=expected, new=_planned(row), reason=str(exc))
        plans.set_block(plan_id, uid, status=plans.FAILED, live_start=live[0], live_end=live[1])
        rep.failed.append(f"{_label(row)}: the calendar refused the move ({exc})")
        return
    plans.add_event(uid, plan_id, plans.EVENT_MOVED, actor, old=expected, new=_planned(row))
    plans.set_block(plan_id, uid, status=plans.SCHEDULED, live_start=None, live_end=None)
    rep.moved += 1


def _drop(svc, plan_id: int, old: dict, actor: str, rep: Report, reason: str = "") -> None:
    uid, eid = old["uid"], old.get("event_id")
    if eid and old["status"] in (plans.SCHEDULED, plans.MOVED, plans.FAILED):
        try:
            event = cal.read_event(svc, eid)
            priv = ((event or {}).get("extendedProperties") or {}).get("private") or {}
            if event is not None and priv.get(cal.BLOCK_KEY) != uid:
                rep.notes.append(f"{_label(old)}: the calendar event is not the one I made, so I left it.")
            elif event is not None:
                cal.delete_event_raw(svc, eid)
        except Exception as exc:
            rep.failed.append(f"removal of {_label(old)}: the calendar refused ({exc})")
            return
    plans.add_event(uid, plan_id, plans.REMOVED, actor, old=_planned(old), reason=reason)
    rep.removed += 1


def _reconcile(svc, iso: str, plan_id: int, new_rows: list, old_rows: list, busy: list,
               rules: P.Rules, now: datetime, actor: str) -> Report:
    """Make the calendar equal to `new_rows`, touching only what changed and only events this plan
    made. `old_rows` are the rows of the version being replaced (empty when retrying one)."""
    rep = Report()
    old_by_uid = {r["uid"]: r for r in old_rows}
    new_uids = {r["uid"] for r in new_rows}
    others = [_block(r, planned=True) for r in new_rows if r["status"] != plans.DELETED]
    for r in new_rows:
        old = old_by_uid.get(r["uid"])
        changed = old is not None and _planned(old) != _planned(r)
        status, eid = r["status"], r.get("event_id")
        if status == plans.DELETED:
            if changed:
                _create(svc, iso, plan_id, r, others, busy, rules, now, actor, rep)
            else:
                rep.kept += 1
        elif eid and status in (plans.SCHEDULED, plans.MOVED) and not changed:
            rep.kept += 1
        elif eid and status in (plans.SCHEDULED, plans.MOVED, plans.FAILED):
            _move(svc, iso, plan_id, r, old, others, busy, rules, now, actor, rep)
        else:
            _create(svc, iso, plan_id, r, others, busy, rules, now, actor, rep)
    for old in old_rows:
        if old["uid"] not in new_uids:
            _drop(svc, plan_id, old, actor, rep)
    return rep


def _sync_week(svc, monday: date) -> list:
    """Compare the applied plan with the calendar and record what the owner changed himself. Never
    changes the calendar."""
    plan = plans.applied(_iso(monday))
    if plan is None:
        return []
    tracked = [r for r in plan["blocks"] if r.get("event_id") and r["status"] in (plans.SCHEDULED, plans.MOVED)]
    if not tracked:
        return []
    lo, hi = _week_window(monday)
    by_id = {}
    try:
        events, _ = cal.fetch_marked(svc, lo, hi)
        by_id = {e["id"]: e for e in events if e.get("id")}
    except Exception as exc:
        log.debug("week_plan: marked-event listing failed (%s); reading events one by one", exc)
    notes = []
    for r in tracked:
        event = by_id.get(r["event_id"])
        if event is not None and event.get("status") == "cancelled":
            event = None
        elif event is None:
            event = cal.read_event(svc, r["event_id"])
        eff = _eff(r)
        if event is None:
            plans.set_block(plan["id"], r["uid"], status=plans.DELETED, live_start=None, live_end=None)
            plans.add_event(r["uid"], plan["id"], plans.SEEN_DELETED, "owner", old=eff,
                            reason="deleted in the calendar")
            notes.append(f"{_label(r)}: you deleted it from your calendar")
            continue
        span = cal.event_span(event)
        if span is None:
            continue
        live = (span[0].timestamp(), span[1].timestamp())
        if _close(live, eff):
            continue
        if _close(live, _planned(r)):
            plans.set_block(plan["id"], r["uid"], status=plans.SCHEDULED, live_start=None, live_end=None)
            plans.add_event(r["uid"], plan["id"], plans.SEEN_MOVED, "owner", old=eff, new=live,
                            reason="back where it was planned")
            continue
        plans.set_block(plan["id"], r["uid"], status=plans.MOVED, live_start=live[0], live_end=live[1])
        plans.add_event(r["uid"], plan["id"], plans.SEEN_MOVED, "owner", old=eff, new=live,
                        reason="moved in the calendar")
        notes.append(f"{_label(r)}: you moved it to {P.local(span[0]):%a %H:%M}-{P.local(span[1]):%H:%M}")
    return notes


def _guarded_sync(svc, monday: date) -> list:
    try:
        return _sync_week(svc, monday)
    except Exception as exc:
        log.warning("week_plan: sync failed (%s)", exc)
        return []


def _apply_version(svc, monday: date, target: dict, old_rows: list, now: datetime, actor: str) -> tuple:
    """Reconcile the calendar to `target`'s blocks and mark it applied. Returns (report, plan)."""
    iso = _iso(monday)
    rules = P.normalize_rules(target["rules"])
    busy = _fetch_busy(svc, monday, _plan_event_ids(old_rows, target["blocks"]))
    rows = plans.blocks_of(target["id"])
    rep = _reconcile(svc, iso, target["id"], rows, old_rows, busy, rules, now, actor)
    plans.mark_applied(target["id"])
    return rep, plans.get(target["id"])


def _refresh(target: dict, head_rows: list) -> None:
    """A proposal was drafted before the last look at the calendar: for every block it shares with
    the plan in the calendar, take over what is true now (its event, whether he moved or deleted it),
    so the proposal is not applied on top of stale state."""
    by_uid = {r["uid"]: r for r in head_rows}
    for r in target["blocks"]:
        h = by_uid.get(r["uid"])
        if h is not None:
            plans.set_block(target["id"], r["uid"], event_id=h.get("event_id"), status=h["status"],
                            live_start=h.get("live_start"), live_end=h.get("live_end"))


def _apply(a: Ask, now: datetime) -> str:
    monday = _week_of(a, now, fallback="plan")
    iso = _iso(monday)
    svc = _svc()
    target = plans.get(int(a.plan_id)) if a.plan_id else plans.draft(iso)
    head = plans.applied(iso)
    if target is None:
        if head is None:
            raise _Fail("there is no proposal to apply; propose a plan first")
        retry = [r for r in head["blocks"] if r["status"] in (plans.PLANNED, plans.FAILED)]
        if not retry:
            return "Nothing to apply: the plan is already in the calendar and every block is written."
        target, old_rows = head, []
    elif target["state"] in (plans.DISCARDED, plans.SUPERSEDED):
        newest = plans.latest_live(iso)
        raise _Fail(f"plan #{target['id']} was replaced by a newer one"
                    + (f" (#{newest['id']})" if newest else "") + "; apply that one")
    elif target["state"] == plans.APPLIED:
        old_rows = []
    else:
        old_rows = head["blocks"] if head else []
    if old_rows:
        _guarded_sync(svc, monday)
        old_rows = plans.applied(iso)["blocks"]
        _refresh(target, old_rows)
    rep, plan = _apply_version(svc, monday, target, old_rows, now, "owner")
    lines = [f"Applied plan #{plan['id']} (version {plan['version']}) for the week of "
             f"{P.day_label(monday)}: {rep.summary()}."] + rep.lines()
    if rep.failed:
        lines.append("Ask me to apply again to retry what failed.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Editing one block
# ---------------------------------------------------------------------------

def _pick(rows: list, a: Ask, monday: date) -> dict:
    day_d = P.parse_day(a.day, monday) if a.day else None
    if a.day and day_d is None:
        raise _Fail(f"I can't read the day {a.day!r}; use a weekday name or a date (YYYY-MM-DD)")
    by_key = {(r["kind"], _eff(r)[0]): r for r in rows}
    hits = P.find_blocks([_block(r) for r in rows], kind=a.kind, day=day_d, near=a.near)
    hits = [by_key[(b.kind, b.start.timestamp())] for b in hits]
    if not hits:
        listing = "; ".join(_label(r) for r in rows[:12]) or "no blocks"
        raise _Fail(f"no {a.kind or 'block'} on {P.day_name(day_d) if day_d else 'that week'} in the plan "
                    f"(it has: {listing})")
    if len(hits) > 1 and not a.near:
        if a.kind == "work" or all(r["kind"] == "work" for r in hits):
            ordered = sorted(hits, key=lambda r: _eff(r)[0])
            if a.end and not a.start:
                return ordered[-1]
            if a.start and not a.end:
                return ordered[0]
        raise _Fail("several blocks match: " + "; ".join(_label(r) for r in hits[:8])
                    + ". Say which one (near = its start time, HH:MM).")
    return hits[0]


def _working(a: Ask, now: datetime, svc) -> tuple:
    monday = _week_of(a, now, fallback="plan")
    iso = _iso(monday)
    cur = plans.latest_live(iso)
    if cur is None:
        raise _Fail("there is no plan for that week; propose one first")
    if cur["state"] == plans.APPLIED:
        _guarded_sync(svc, monday)
        cur = plans.latest_live(iso)
    return monday, cur


def _finish(svc, monday: date, cur: dict, rows: list, rules: P.Rules, note: str, now: datetime) -> tuple:
    """Store `rows` as the new version. When the plan it changes is in the calendar, the calendar
    follows at once (these are JARVIS's own blocks). Returns (report or None, plan)."""
    pid = _store(monday, rules, rows, note=note, parent=cur)
    new = plans.get(pid)
    if cur["state"] != plans.APPLIED:
        return None, new
    return _apply_version(svc, monday, new, cur["blocks"], now, "owner")


def _change(a: Ask, now: datetime) -> str:
    svc = _svc()
    monday, cur = _working(a, now, svc)
    rules = P.normalize_rules(cur["rules"])
    rows = cur["blocks"]
    target = _pick(rows, a, monday)
    block = _block(target)
    to_day = None
    if a.to_day:
        to_day = P.parse_day(a.to_day, monday)
        if to_day is None:
            raise _Fail(f"I can't read the day {a.to_day!r}; use a weekday name or a date (YYYY-MM-DD)")
    if not (a.start or a.end or a.minutes or a.prefer or to_day):
        raise _Fail("say what to change: start, end, minutes, prefer (morning / afternoon / evening) or to_day")
    if a.prefer and a.prefer.lower() not in ("morning", "afternoon", "evening"):
        raise _Fail("prefer must be morning, afternoon or evening")
    busy = _fetch_busy(svc, monday, _plan_event_ids(rows))
    others = [_block(r) for r in rows if r is not target and r["status"] != plans.DELETED]
    if a.prefer and not a.start:
        day_d = to_day or P.local(block.start).date()
        minutes = a.minutes or block.minutes
        new = P.find_slot(block.kind, minutes, day_d, rules, busy, others, prefer=a.prefer.lower(), now=now)
        if new is None:
            raise _Fail(f"there is no room for {block.kind} ({minutes} min) on {P.day_name(day_d)} in the "
                        f"{a.prefer}; nothing was changed")
    else:
        new = P.edited(block, start=a.start, end=a.end, minutes=int(a.minutes or 0), day=to_day)
        problem = P.check_block(new, others, busy, rules, now=now, ignore_days=bool(to_day))
        if problem:
            raise _Fail(f"{_label(target)} can't go there: {problem}. Nothing was changed.")
    if _close((new.start.timestamp(), new.end.timestamp()), _eff(target)):
        return "That is where it already is; nothing changed."
    new_rows = [(_carry(r, start=new.start.timestamp(), end=new.end.timestamp(),
                        live_start=None, live_end=None) if r is target else _carry(r)) for r in rows]
    note = f"moved {block.kind} to {P.local(new.start):%a %H:%M}" + (f" ({a.reason})" if a.reason else "")
    rep, plan = _finish(svc, monday, cur, new_rows, rules, note, now)
    if rep is not None:
        # A block he skipped and that is now rescheduled: the skip stays in the record, and until he
        # reports on the new slot the block's outcome is "moved" (not a skip still standing on a
        # future date).
        told = plans.outcomes([target["uid"]]).get(target["uid"])
        if told and told["outcome"] == "skipped":
            plans.record_outcome(target["uid"], plan["id"], "moved", "jarvis", now=now.timestamp(),
                                 reason=f"rescheduled after a skip{': ' + a.reason if a.reason else ''}")
    ls, le = P.local(new.start), P.local(new.end)
    same_day = ls.date() == P.local(block.start).date()
    out = [f"Moved {P.day_name(P.local(block.start).date())}'s {block.kind} to "
           f"{'' if same_day else P.day_name(ls.date()) + ' '}{ls:%H:%M}-{le:%H:%M} "
           f"(plan version {plan['version']})."]
    out += _after(rep)
    return "\n".join(out)


def _after(rep: Report | None) -> list:
    if rep is None:
        return ["It is a proposal; nothing is in the calendar until you apply it."]
    out = ["The calendar is updated: " + rep.summary() + "."]
    return out + rep.lines()


def _remove(a: Ask, now: datetime) -> str:
    svc = _svc()
    monday, cur = _working(a, now, svc)
    rules = P.normalize_rules(cur["rules"])
    rows = cur["blocks"]
    target = _pick(rows, a, monday)
    new_rows = [_carry(r) for r in rows if r is not target]
    note = f"removed {target['kind']} {P.local(_dt(_eff(target)[0])):%a %H:%M}" + (f" ({a.reason})" if a.reason else "")
    rep, plan = _finish(svc, monday, cur, new_rows, rules, note, now)
    return "\n".join([f"Removed {_label(target)} (plan version {plan['version']})."] + _after(rep))


def _diff(old_rows: list, new_rows: list) -> tuple:
    o = {r["uid"]: r for r in old_rows}
    n = {r["uid"]: r for r in new_rows}
    added = [n[u] for u in n if u not in o]
    removed = [o[u] for u in o if u not in n]
    moved = [(o[u], n[u]) for u in n if u in o and _planned(o[u]) != _planned(n[u])]
    return added, removed, moved


def _undo(a: Ask, now: datetime) -> str:
    monday = _week_of(a, now, fallback="plan")
    iso = _iso(monday)
    live = plans.latest_live(iso)
    if live is None:
        raise _Fail("there is no plan for that week")
    if live["state"] == plans.DRAFT:
        plans.discard(live["id"])
        head = plans.applied(iso)
        return (f"Dropped the proposal (plan #{live['id']})."
                + (f" Your calendar still has plan version {head['version']}." if head
                   else " There is no plan for that week now."))
    svc = _svc()
    _guarded_sync(svc, monday)
    head = plans.applied(iso)
    target_v = next((v for v in plans.versions(iso)
                     if v["state"] in (plans.APPLIED, plans.SUPERSEDED) and v["undone_by"] is None
                     and v["parent_id"] and v["created_by"] in ("jarvis", "owner")), None)
    if target_v is None:
        return "Nothing to undo: the plan is still as first written."
    w, p = plans.get(target_v["id"]), plans.get(target_v["parent_id"])
    rules = P.normalize_rules(p["rules"])
    added, removed, moved = _diff(p["blocks"], w["blocks"])
    head_by = {r["uid"]: r for r in head["blocks"]}
    rows = [_carry(r) for r in head["blocks"]]
    notes = []
    busy = _fetch_busy(svc, monday, _plan_event_ids(head["blocks"]))

    def others_of(uid):
        return [_block(r) for r in rows if r["uid"] != uid and r["status"] != plans.DELETED]

    for w_row in added:                                     # blocks that version added: take them out
        h = head_by.get(w_row["uid"])
        if h is None:
            continue
        if _planned(h) == _planned(w_row) and h["status"] != plans.MOVED:
            rows = [r for r in rows if r["uid"] != w_row["uid"]]
        else:
            notes.append(f"{_label(h)} was changed since, so I left it.")
    for p_row, w_row in moved:                              # blocks that version moved: move them back
        h = head_by.get(w_row["uid"])
        if h is None or _planned(h) != _planned(w_row) or h["status"] == plans.MOVED:
            notes.append(f"{_label(w_row)} was changed since (by you or by a later change), so I left it.")
            continue
        block = _block(p_row, planned=True)
        problem = P.check_block(block, others_of(w_row["uid"]), busy, rules, now=now, ignore_days=True)
        if problem:
            notes.append(f"could not move {_label(w_row)} back: {problem}.")
            continue
        rows = [(_carry(r, start=p_row["start_at"], end=p_row["end_at"], live_start=None, live_end=None)
                 if r["uid"] == w_row["uid"] else r) for r in rows]
    for p_row in removed:                                   # blocks that version removed: bring them back
        if p_row["uid"] in {r["uid"] for r in rows}:
            continue
        block = _block(p_row, planned=True)
        problem = P.check_block(block, others_of(p_row["uid"]), busy, rules, now=now, ignore_days=True)
        if problem:
            notes.append(f"could not bring back {_label(p_row)}: {problem}.")
            continue
        rows.append(_carry(p_row, event_id=None, status=plans.PLANNED, live_start=None, live_end=None))
    what = w["note"] or f"version {w['version']}"
    pid = _store(monday, rules, rows, note=f"undo of version {w['version']}", parent=head)
    plans.mark_undone(w["id"], pid)
    plans.mark_undone(pid, pid)
    rep, plan = _apply_version(svc, monday, plans.get(pid), head["blocks"], now, "owner")
    return "\n".join([f"Undid version {w['version']} ({what}); the plan is now version {plan['version']}. "
                      f"The calendar: {rep.summary()}."] + notes + rep.lines())


def _show(a: Ask, now: datetime) -> str:
    monday = _week_of(a, now, fallback="plan")
    iso = _iso(monday)
    cur = plans.latest_live(iso)
    if cur is None:
        return f"There is no plan for the week of {P.day_label(monday)}."
    changes = []
    if cur["state"] == plans.APPLIED:
        try:
            changes = _sync_week(_svc(), monday)
        except Exception as exc:
            log.debug("week_plan: show could not sync (%s)", exc)
        cur = plans.get(cur["id"])
    only = None
    if a.day:
        only = P.parse_day(a.day, monday)
        if only is None:
            raise _Fail(f"I can't read the day {a.day!r}; use a weekday name or a date (YYYY-MM-DD)")
    body = _render(cur, only=only) or ["(no blocks that day)"]
    out = [_header(cur)] + body
    head = plans.applied(iso)
    if cur["state"] == plans.DRAFT and head:
        out.append(f"Your calendar still has plan version {head['version']}; this proposal replaces it once applied.")
    if changes:
        out.append("Since I last looked: " + "; ".join(changes) + ".")
    return "\n".join(out)


def _sync(a: Ask, now: datetime) -> str:
    monday = _week_of(a, now, fallback="plan")
    if plans.applied(_iso(monday)) is None:
        return "There is no plan in the calendar for that week, so there is nothing to compare."
    changes = _sync_week(_svc(), monday)
    return ("You changed: " + "; ".join(changes) + ". I've noted it and left it as it is."
            if changes else "The calendar matches the plan: nothing was changed by hand.")


def _report(a: Ask, now: datetime) -> str:
    from backend.brain import planday             # lazy: planday imports this module
    return planday.report(a, now)


_HANDLERS = {"propose": _propose, "add": _add, "apply": _apply, "change": _change, "remove": _remove,
             "undo": _undo, "show": _show, "sync": _sync, "report": _report}


def _take_settings(ask: Ask) -> tuple[Ask, str]:
    """`auto_move` in the rules is a standing pre-approval, not something to place: take it out,
    keep it (plan_settings, so it outlives any one week) and say so. Anything else is left for
    the planner, which refuses fields it does not know."""
    import json
    rules = ask.rules
    if isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except ValueError:
            return ask, ""
    if not isinstance(rules, dict) or "auto_move" not in rules:
        return ask, ""
    from backend.brain import planday
    rest = {k: v for k, v in rules.items() if k != "auto_move"}
    try:
        kinds = planday.set_auto_move(rules["auto_move"])
    except ValueError as exc:
        raise _Fail(str(exc)) from None
    note = (f"Noted: I may move your {', '.join(kinds)} blocks myself when you skip one or a meeting lands on it "
            "(I tell you each time, and undo puts it back). Anything else I ask first."
            if kinds else "Cleared: I ask before moving any block.")
    from dataclasses import replace
    return replace(ask, rules=rest or None), note


def run(ask: Ask, now: datetime | None = None) -> str:
    """The whole tool, synchronously. Never raises."""
    if ask.action not in _HANDLERS:
        return f"Error: unknown action {ask.action!r}; use one of {', '.join(ACTIONS)}."
    with _LOCK:
        try:
            note = ""
            if ask.action in ("propose", "add") and ask.rules:
                ask, note = _take_settings(ask)
                if note and not ask.rules:
                    return note
            out = _HANDLERS[ask.action](ask, now or _now())
            return f"{note}\n{out}" if note else out
        except _Fail as fail:
            return f"Error: {fail}"
        except P.RulesError as err:
            return f"Error: {err}"
        except Exception as exc:
            log.exception("week_plan %s failed", ask.action)
            return f"Error: week_plan {ask.action} failed ({exc}); check the calendar before trusting the plan."


async def week_plan(action: str, week: str = "", rules=None, plan_id: int = 0, kind: str = "",
                    day: str = "", to_day: str = "", near: str = "", start: str = "", end: str = "",
                    minutes: int = 0, prefer: str = "", reason: str = "", outcomes=None, said: str = "",
                    items=None, quote: str = "") -> str:      # quote: evidence for the policy check, removed by the gateway
    ask = Ask(action=action, week=week, rules=rules, plan_id=int(plan_id or 0), kind=(kind or "").strip().lower(),
              day=day, to_day=to_day, near=near, start=start, end=end, minutes=int(minutes or 0),
              prefer=prefer, reason=reason, outcomes=outcomes if outcomes is not None else items,
              said=said or "")
    if action == "report":
        from backend.brain import planday
        planday.drain_proposals()                  # anything left over is not this report's
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run, ask)
    if action == "report":
        extra = await _put_proposals()
        if extra:
            result = f"{result}\n{extra}"
    return result


async def _put_proposals() -> str:
    """A skipped block is offered a new slot: put each proposal to him for a tap."""
    from backend.brain import planday, reshuffle
    lines = []
    for proposal in planday.drain_proposals():
        line = await reshuffle.request_move(proposal)
        if line:
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# What the tool gateway needs to know about an apply
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# What he is told on every turn
# ---------------------------------------------------------------------------

_DRAFT_WAIT_S = 24 * 3600         # an older proposal is no longer "the one we are talking about"


def prompt_line(now: datetime | None = None) -> str:
    """One line for the top of every turn while a proposal waits for his yes, else "".

    Live (2026-09-30): the model sees the words of earlier turns, not its own tool calls, so after
    "Yes, add them to my calendar" it called add or propose AGAIN instead of apply, and the gym
    blocks came out doubled in 2 of 5 trials. Never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        today = P.local(now).date()
        draft = plans.newest_draft(_iso(P.monday_of(today)), now.timestamp() - _DRAFT_WAIT_S)
        if draft is None:
            return ""
        waiting = [r for r in draft["blocks"] if r["status"] == plans.PLANNED]
        if not waiting:
            return ""
        kinds: dict[str, int] = {}
        for r in waiting:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        what = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        monday = date.fromisoformat(draft["week_start"])
        return (_owner.say(f"Week plan: proposal #{draft['id']} for the week of {monday.strftime('%a %d %b')} is drawn and "
                f"waiting for «his» yes ({what} not in «his» calendar yet). If «he» says yes to it, call week_plan "
                "apply with «his» words as quote; do not propose or add again for the same request."))
    except Exception:
        return ""


def prepare_args(args: dict) -> dict:
    """Pin an `apply` to the exact proposal being discussed, so a stored approval request runs
    that proposal and not whichever draft is newest when he taps Approve."""
    if args.get("action") != "apply" or args.get("plan_id"):
        return args
    try:
        monday = _resolve_week(str(args.get("week") or ""), _now(), fallback="plan")
        draft = plans.draft(_iso(monday)) if monday else None
    except Exception:
        return args
    return {**args, "plan_id": draft["id"]} if draft else args


def describe_apply(args: dict) -> str:
    """What applying would put in his calendar, for the approval request he reads."""
    if args.get("action") != "apply":
        return ""
    plan = plans.get(int(args["plan_id"])) if args.get("plan_id") else None
    if plan is None:
        return ""
    lines = _render(plan)
    shown = lines[:24] + ([f"... and {len(lines) - 24} more lines"] if len(lines) > 24 else [])
    return (f"Put this plan in your calendar (week of {P.day_label(date.fromisoformat(plan['week_start']))}, "
            f"{len(plan['blocks'])} blocks):\n" + "\n".join(shown))


_gateway.set_preparer("week_plan", prepare_args)
_gateway.set_describer("week_plan", describe_apply)

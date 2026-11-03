"""P2: /activity -- what JARVIS is doing, holding and waiting on, in one place.

Dots (OpenAI, 29 September 2026) show a person what each of their agents is up to. JARVIS
does things on his own in a dozen places (jobs, research, reminders, timed and night tasks,
watches, forecast checks, approvals) and the owner had no single view of them. This is that
view, and the answer to "what are you working on?" -- the `check_background_tasks` tool
returns the same text.

**Nothing here is tracked separately.** Every line is read from what the owner of that work
already records: background jobs (`background.snapshot()`), approvals, `schedule.md`
(`chronos.agenda()`), the research queue and trail (`research.snapshot()`), watches,
forecasts, runs and tool receipts, and pauses. If a source cannot be read, its section says
so and the others still show; a view that disappears when one table is unhappy is worse than
one that admits a gap.

Sections, in the order he needs them: whether he has paused me, what is running now, what
waits for HIS answer, what is scheduled (in time order), and what finished in the last 24
hours with how it ended. All times are local time.
"""
from __future__ import annotations
from backend import localtime as _localtime

import logging
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_TZ = _localtime.ZONE
_DAY_S = 24 * 3600.0
_MAX_RUNNING = 6
_MAX_WAITING = 6
_MAX_SCHEDULED = 14
_MAX_DONE = 12
_UNCERTAIN_WINDOW_S = 3 * _DAY_S     # an uncertain approval stays visible this long

_CONTEXT_WORDS = {
    "live": "from our conversation", "background": "from a background job",
    "scheduled": "from a scheduled task", "night": "from a night task",
    "unattended": "from unattended work",
}
_WATCH_EVERY = {3600.0: "hour", 6 * 3600.0: "6 hours", 24 * 3600.0: "day"}


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=_TZ)


def _when(ts: float, now: float) -> str:
    """'today 15:30', 'tomorrow 08:00', 'yesterday 22:00', or 'Fri 02 Oct 15:30'."""
    at, base = _dt(ts), _dt(now)
    gap = (at.date() - base.date()).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(gap) or at.strftime("%a %d %b")
    return f"{day} {at.strftime('%H:%M')}"


def _span(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    minutes = s // 60
    if minutes < 90:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _cap(lines: list[str], n: int) -> list[str]:
    return lines if len(lines) <= n else lines[:n] + [f"(+{len(lines) - n} more)"]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------------------
# Paused
# ---------------------------------------------------------------------------

def _paused(now: float) -> list[str]:
    from backend.brain import pause
    from backend.storage import pauses
    row = pauses.active(now=now)
    if row is None:
        return []
    line = pause.describe(row, now=now)
    skipped = len(pauses.chain_skips(row["id"]))
    if skipped:
        line += f" {_plural(skipped, 'thing')} skipped or stopped so far; /resume lists them."
    return [line]


# ---------------------------------------------------------------------------
# Running now
# ---------------------------------------------------------------------------

_JOB_WORDS = {"agent": "job", "scheduled": "scheduled run", "web_research": "web research"}


def _job_label(t: dict) -> str:
    word = _JOB_WORDS.get(t["kind"], "job")
    depth = f", {t['depth']}" if t["kind"] == "web_research" and t.get("depth") else ""
    return f"{word} #{t['id']}{depth}"


def _running(now: float) -> list[str]:
    from backend.brain import background
    from backend.storage import approvals, runs
    out: list[str] = []
    for t in background.snapshot():
        if t["status"] == "running":
            out.append(f"{_job_label(t)} ({_span(now - t['started'])}): {_short(t['description'], 110)}")
        elif t["status"] == "queued":
            out.append(f"{_job_label(t)} queued: {_short(t['description'], 110)}")
    try:
        from backend.brain import people_research
        for key in people_research.running():
            out.append(f"profile research on {key}")
    except Exception as exc:
        log.debug("activity: profile research unreadable (%s)", exc)
    for r in runs.open_runs(["night_task"]):
        out.append(f"night task ({_span(now - float(r.get('started_at') or now))}): "
                   f"{_short(r.get('user_preview') or 'an overnight task', 110)}")
    for a in approvals.recent(30):
        if a["state"] == approvals.EXECUTING:
            out.append(f"request #{a['id']} is being carried out: {_short(a['preview'].splitlines()[0], 100)}")
    return _cap(out, _MAX_RUNNING)


# ---------------------------------------------------------------------------
# Waiting for him
# ---------------------------------------------------------------------------

def _waiting(now: float) -> list[str]:
    from backend.storage import approvals, forecasts
    out: list[str] = []
    for a in approvals.pending(now=now):
        who = _CONTEXT_WORDS.get(a["context"], a["context"])
        out.append(f"request #{a['id']}: {_short(a['preview'].splitlines()[0], 110)} "
                   f"({who}, expires in {_span(a['expires_at'] - now)}). "
                   f"Reply \"yes {a['id']}\" or \"no {a['id']}\", or tap the buttons.")
    for a in approvals.recent(50):
        when = a.get("executed_at") or a.get("decided_at") or a["created_at"]
        if a["state"] == approvals.UNCERTAIN and now - when < _UNCERTAIN_WINDOW_S:
            out.append(f"request #{a['id']} was cut off mid-run ({_when(when, now)}): "
                       f"{_short(a['preview'].splitlines()[0], 90)}. It may or may not have "
                       "happened; check before asking again.")
    for f in forecasts.query(state=forecasts.UNRESOLVABLE):
        out.append(f"forecast #{f['forecast_id']} could not be settled on its own: "
                   f"{_short(f['claim'], 90)}. Tell me how it turned out.")
    parked = _parked_notices()
    if parked:
        out.append(parked)
    return _cap(out, _MAX_WAITING)


def _parked_notices() -> str:
    """Things I could not message him and will tell him in our next conversation."""
    from backend.brain import chronos, research
    from backend.storage import forecasts, watches
    parts = []
    reminders = chronos.agenda()["parked"]
    if reminders:
        parts.append(_plural(reminders, "reminder"))
    findings = research.snapshot()["deferred"]
    if findings:
        parts.append(_plural(findings, "research finding"))
    alerts = sum(1 for w in watches.list_active() if w["alert_state"].startswith("parked"))
    alerts += sum(1 for f in forecasts.query() if f["notice_state"].startswith("parked"))
    if alerts:
        parts.append(_plural(alerts, "alert"))
    return ("to tell you in our next conversation: " + ", ".join(parts)) if parts else ""


# ---------------------------------------------------------------------------
# Scheduled
# ---------------------------------------------------------------------------

def _repeat_words(t: dict) -> str:
    rule = {"once": "once", "daily": "every day", "weekdays": "on weekdays",
            "weekly": f"every {t['due'].strftime('%A')}"}.get(t["repeat"], t["repeat"])
    if t.get("until") and t["repeat"] != "once":
        rule += f" until {t['until']}"
    return rule


def _next_brief(brief: dict, now: float) -> tuple[float, bool]:
    """(when the next morning brief goes out, whether it is overdue). One goes out per local time
    day, at or after its time, so a brief not yet sent whose time has passed is due now."""
    hh, mm = (int(x) for x in brief["time"].split(":"))
    day = _dt(now).date()
    if brief["last_date"] == day.strftime("%Y-%m-%d"):
        day += timedelta(days=1)                 # today's is out: tomorrow's is next
    target = datetime.combine(day, dtime(hh, mm), tzinfo=_TZ).timestamp()
    return (now, True) if target < now else (target, False)


def _scheduled(now: float) -> list[str]:
    from backend.brain import chronos, research
    from backend.storage import forecasts, watches
    ag = chronos.agenda()
    timed: list[tuple[float, str]] = []
    for r in ag["reminders"]:
        every = " (every day)" if r["repeat"] == "daily" else ""
        timed.append((r["due"].timestamp(), f"reminder: {_short(r['message'], 90)}{every}"))
    for t in ag["tasks"]:
        tz = "" if t["tz"] == _localtime.TIMEZONE else f", {t['due'].strftime('%H:%M')} {t['tz']}"
        follow = f", following skill {t['skill']}" if t.get("skill") else ""
        timed.append((t["due"].timestamp(),
                      f"timed check: {_short(t['task'], 80)} ({_repeat_words(t)}{tz}{follow})"))
    brief = ag["brief"]
    if brief["enabled"]:
        ts, overdue = _next_brief(brief, now)
        timed.append((ts, "morning brief" + (" (overdue: goes out as soon as I am free)"
                                             if overdue else "")))
    try:
        from backend.brain import planday
        timed.extend(planday.scheduled_items(now))
    except Exception:
        pass                                     # the plan loop's lines are a bonus; the rest still shows
    timed.sort(key=lambda item: item[0])
    try:
        from backend.brain import portfolio
        timed.extend(portfolio.scheduled_items(now))
        timed.sort(key=lambda item: item[0])
    except Exception:
        timed.append((now, "portfolio schedule could not be read just now"))
    out = [f"{_when(ts, now)}: {text}" for ts, text in timed]

    night = ag["night"]
    if night:
        held = [n for n in night if n["after"] and n["after"] > _dt(now).strftime("%Y-%m-%d")]
        line = (f"{_plural(len(night), 'night task')} queued for the quiet hours "
                f"(02:00 to 07:00): " + "; ".join(_short(n["task"], 60) for n in night[:3]))
        if held:
            line += f" ({len(held)} not before its date)"
        out.append(line)

    for w in watches.list_active():
        every = _WATCH_EVERY.get(float(w["every_s"]), f"{int(w['every_s'] // 60)} min")
        out.append(f"watch: {_short(w['subject'], 60)} -- {_short(w['condition'], 70)} "
                   f"(every {every}, next check {_when(w['next_check_at'], now)}, "
                   f"until {_when(w['expires_at'], now)})")

    open_f = forecasts.query(state=forecasts.OPEN)
    if open_f:
        nxt_f = min(open_f, key=lambda f: f["resolve_by"])
        out.append(f"{_plural(len(open_f), 'open forecast')}; next one falls due "
                   f"{_when(nxt_f['resolve_by'], now)}: {_short(nxt_f['claim'], 70)}")

    snap = research.snapshot()
    if snap["pace"] == "off":
        out.append("my own research is off (/pace to change)")
    else:
        nxt_r = f", next pass {_when(snap['next_at'], now)}" if snap["next_at"] else ""
        queued = (f"; {_plural(len(snap['queued']), 'subject')} you queued: "
                  + ", ".join(_short(q['subject'], 30) for q in snap['queued'][:3])) if snap["queued"] else ""
        out.append(f"my own research: pace {snap['pace']}{nxt_r}{queued}")
    return _cap(out, _MAX_SCHEDULED)


# ---------------------------------------------------------------------------
# Done in the last 24 hours
# ---------------------------------------------------------------------------

def _done(now: float) -> list[str]:
    from backend.brain import chronos, research
    from backend.brain import background
    from backend.storage import approvals, forecasts, operations, pauses, runs, watches
    since = now - _DAY_S
    items: list[tuple[float, str]] = []

    for t in background.snapshot():
        fin = t["finished"]
        if not fin or fin < since or t["status"] in ("running", "queued"):
            continue
        took = _span(fin - t["started"])
        head = f"{_job_label(t)} {t['status']} ({took}): {_short(t['description'], 70)}"
        if t["status"] == "done" and t["summary"]:
            head += f" -- {_short(t['summary'], 110)}"
        elif t["status"] in ("failed", "cancelled") and t["summary"]:
            head += f" -- {_short(t['summary'], 90)}"
        if t["note_file"]:
            head += f" [{t['note_file']}]"
        items.append((fin, head))

    for a in approvals.recent(60):
        first = _short(a["preview"].splitlines()[0], 90)
        state = a["state"]
        if state == approvals.EXECUTED:
            ts, text = a.get("executed_at"), f"request #{a['id']} approved and carried out: {first}"
        elif state == approvals.FAILED:
            ts, text = (a.get("executed_at"),
                        f"request #{a['id']} approved but failed: {first} -- {_short(a['outcome'], 70)}")
        elif state == approvals.UNCERTAIN:
            ts, text = (a.get("executed_at") or a.get("decided_at"),
                        f"request #{a['id']} approved, result uncertain: {first}")
        elif state == approvals.DENIED:
            ts, text = a.get("decided_at"), f"request #{a['id']} denied, so it never ran: {first}"
        elif state == approvals.EXPIRED:
            ts, text = a["expires_at"], f"request #{a['id']} expired unanswered and never ran: {first}"
        else:
            continue
        if ts and ts >= since:
            items.append((ts, text))

    for d in chronos.agenda()["done"]:
        ts = d["at"].timestamp()
        if ts >= since:
            items.append((ts, _short(d["text"], 120)))

    for p in research.snapshot()["passes"]:
        if p["at"] >= since:
            items.append((p["at"], f"research: {_short(p['subject'], 60)} -- {_short(p['outcome'], 90)}"))

    for f in forecasts.query(state=forecasts.RESOLVED):
        if (f["resolved_at"] or 0) >= since:
            verdict = "yes" if f["outcome"] else "no"
            items.append((f["resolved_at"], f"forecast #{f['forecast_id']} settled {verdict}: {_short(f['claim'], 80)}"))

    for w in watches.fired_since(since):
        items.append((w["last_checked_at"], f"watch fired: {_short(w['subject'], 60)}"))

    try:
        from backend.brain import planday
        items.extend(planday.done_items(since))
    except Exception:
        pass
    for p in pauses.recent(10):                  # one still in force is shown at the top
        end = p.get("ended_at")
        if end and end >= since:
            items.append((end, f"pause ended after {_span(end - p['started_at'])} "
                               f"({p['scope']}, {p['ended_how']})"
                          + (f": {_short(p['reason'], 60)}" if p.get("reason") else "")))

    items.sort(key=lambda item: item[0], reverse=True)
    out = [f"{_when(ts, now)}: {text}" for ts, text in items]
    out = _cap(out, _MAX_DONE)

    turns = runs.counts_since(since, ["discord", "local"])
    if turns:
        total = sum(turns.values())
        odd = [f"{turns[s]} {s}" for s in ("interrupted", "failed") if turns.get(s)]
        out.append(f"conversation turns: {total}" + (f" ({', '.join(odd)})" if odd else ""))
    ops = operations.outcome_counts(since)
    if ops:
        label = {"ok": "ok", "failed": "failed", "unknown": "uncertain"}
        order = [k for k in label if k in ops] + sorted(set(ops) - set(label))
        out.append("tool calls: " + ", ".join(f"{ops[k]} {label.get(k, k)}" for k in order))
    return out


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

def _goals(now: float) -> list[str]:
    """G1a: goals that are running, paused or waiting for his yes, and those ended in the last
    day. Empty while goals are off."""
    from backend.brain import goal_runner, goals
    from backend.storage import goals as store
    if not goal_runner.configured():
        return []
    lines = []
    for g in store.list_goals(limit=20):
        if g["state"] in store.ENDED and (g.get("ended_at") or 0) < now - 86400:
            continue
        lines.append(goals._summary(g).lstrip("- "))
    return lines


def _safe(name: str, fn, now: float) -> list[str]:
    try:
        return fn(now)
    except Exception as exc:
        log.warning("activity: %s section unavailable (%s)", name, exc)
        return [f"({name} could not be read just now)"]


_SECTIONS = (
    ("Running now", "running work", _running, "nothing is running."),
    ("Waiting for you", "waiting items", _waiting, "nothing is waiting on you."),
    ("Scheduled", "schedule", _scheduled, "nothing is scheduled."),
    ("Done in the last 24 hours", "recent work", _done, "nothing finished."),
    ("Goals", "goals", _goals, None),
)


def activity_view(now: float | None = None) -> dict:
    """The same view as data, for the phone: the pause line, then each section's lines. A
    section with no empty wording (goals) is left out when it has nothing. Never raises."""
    ts = time.time() if now is None else now
    paused = _safe("pause state", _paused, ts)
    sections = []
    for title, name, fn, empty in _SECTIONS:
        lines = _safe(name, fn, ts)
        if empty is None and not lines:
            continue                         # a section shown only when it has something
        sections.append({"title": title, "items": lines, "empty": empty or ""})
    return {"as_of": ts, "paused": paused[0] if paused else None, "sections": sections}


def activity_text(now: float | None = None) -> str:
    """The whole view as text. Never raises."""
    view = activity_view(now)
    parts = [f"Activity as of {_dt(view['as_of']).strftime('%a %d %b %H:%M')} (local time)"]
    if view["paused"]:
        parts += ["", view["paused"]]
    for s in view["sections"]:
        parts.append("")
        if s["items"]:
            parts += [s["title"], *[f"- {line}" for line in s["items"]]]
        else:
            parts.append(f"{s['title']}: {s['empty']}")
    return "\n".join(parts)

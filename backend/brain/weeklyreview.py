"""P4: the Sunday-evening look back, and next week drawn from it.

What was planned against what happened comes straight from SQLite (`plans.adherence`, the events
behind it) -- counts, not a model's recollection -- and the message is composed by code, so it
cannot invent a number. Next week is proposed from this week's stored rules (`week_plan propose`,
a draft: nothing reaches his calendar until he says yes). A pattern in what slipped is only ever a
*suggestion* in the message; a rule changes only if he agrees, in the conversation.

Nothing here sends anything; `planloop.py` decides when and delivers.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from backend.brain import planday
from backend.brain import planner as P
from backend.brain import weekplan as W
from backend.storage import plans

log = logging.getLogger(__name__)

_LOOKBACK_WEEKS = 4
_PATTERN_MIN = 2                # slipped in this many different weeks
_MAX_PATTERNS = 2


def _iso(d: date) -> str:
    return d.isoformat()


def this_week_text(monday: date) -> str:
    rows = plans.adherence(since_week=_iso(monday), until_week=_iso(monday))
    if not rows:
        return "There were no blocks to look back on."
    return "Planned against what happened: " + "; ".join(planday._kind_stats(r) for r in rows) + "."


def patterns(today: date) -> list[str]:
    """Kinds of block that slipped on the same weekday in several of the last weeks."""
    since = today - timedelta(weeks=_LOOKBACK_WEEKS) + timedelta(days=1)
    weeks: dict[tuple, set] = {}
    for s in plans.slips(since_date=_iso(since)):
        if not s["kind"] or s["kind"] == "work":
            continue
        d = date.fromisoformat(s["date"])
        weeks.setdefault((s["kind"], d.weekday()), set()).add(_iso(P.monday_of(d)))
    found = sorted(((len(w), k) for k, w in weeks.items() if len(w) >= _PATTERN_MIN), reverse=True)
    lines = []
    for n, (kind, weekday) in found[:_MAX_PATTERNS]:
        day = P._DAY_LONG[weekday]
        lines.append(f"{day} {kind} slipped in {n} of the last {_LOOKBACK_WEEKS} weeks. Want me to stop planning "
                     f"{kind} on {day}s? I only change a rule if you say so.")
    return lines


def _compact(plan: dict) -> str:
    by_day = planday._rows_by_day(plan)
    lines = []
    for d in sorted(by_day):
        parts = [f"{kind} {planday._times(rows)}" for kind, rows in planday._grouped(by_day[d])]
        lines.append(f"{P.DAYS[d.weekday()].capitalize()} {d.day}: " + ", ".join(parts))
    return "\n".join(lines) if lines else "(no blocks)"


def next_week_text(now: datetime, plan: dict, monday: date, calendar_ready: bool) -> str:
    next_monday = monday + timedelta(days=7)
    existing = plans.latest_live(_iso(next_monday))
    if existing is not None:
        what = "a plan in your calendar" if existing["state"] == plans.APPLIED else f"a proposal (#{existing['id']})"
        return f"Next week (from {next_monday.day} {next_monday:%b}) already has {what}; I left it alone."
    if not calendar_ready:
        return "I could not reach your calendar, so next week is not drawn yet. Ask me and I will plan it."
    out = W.run(W.Ask(action="propose", week=_iso(next_monday), rules=plan["rules"]), now)
    draft = plans.draft(_iso(next_monday))
    if draft is None or out.startswith("Error"):
        why = out[len("Error:"):].strip() if out.startswith("Error") else "it could not be stored"
        return f"I could not draw next week from these rules ({why})."
    return (f"Next week drawn from the same rules (proposal #{draft['id']}, nothing in your calendar yet):\n"
            f"{_compact(draft)}\nSay yes and I'll put it in your calendar, or tell me what to change.")


def compose(now: datetime, calendar_ready: bool = True) -> str | None:
    """The review message, or None when nothing was planned this week. Draws next week as a
    side effect (a draft)."""
    today = P.local(now).date()
    monday = P.monday_of(today)
    plan = plans.applied(_iso(monday))
    if plan is None:
        return None
    parts = [f"Weekly review, week of {monday.day} {monday:%b}.", this_week_text(monday)]
    parts.extend(patterns(today))
    try:
        from backend.brain import skill_nightly
        skills_week = skill_nightly.week_line(monday)
    except Exception:
        skills_week = ""
    if skills_week:
        parts.append(skills_week)
    parts.append(next_week_text(now, plan, monday, calendar_ready))
    return "\n".join(parts)

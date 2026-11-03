"""P4: the day in the plan -- what is planned today, the evening check-in, and what became of each block.

`weekplan.py` builds the week and keeps the calendar equal to it. This module is what happens to
the plan as the days go by:

  * **The evening check-in.** One short message lists the day's planned blocks (`checkin_text`).
    The owner answers in his own words -- typed or a voice note, it reaches JARVIS the same way -- and
    JARVIS turns the answer into one outcome per block with `week_plan(action="report")`: done,
    partly, skipped, moved, unknown, each with his reason if he gave one (`report`). **Code matches
    every outcome to a real block** (by kind and, when there are two, the time); one it cannot match
    is refused with the day's blocks listed. A block he left unclear gets ONE follow-up question a
    day; what is still unanswered when the day closes is recorded as **unknown, never guessed**
    (`close_day`). Outcomes are appended as facts (`plans.record_outcome`) and the check-in itself is
    a row per day (`storage/checkins.py`).
  * **Telling JARVIS.** He cannot see his own earlier tool calls, so a waiting check-in is put in
    front of him on every turn (`prompt_line`), the same way a waiting proposal and a pause are.
  * **Reading the plan back.** `/plan` (`handle_plan_command`), the morning brief's list of today's
    blocks (`todays_blocks_text`), and the two life-log views (`stats_text`).

Settings live in `plan_settings` (check-in on/off and time, review on/off and time, pre-approved
moves), so they are backed up with the rest of the record. Nothing here talks to the calendar.
"""
from __future__ import annotations
from backend import owner as _owner

import logging
import re
from datetime import date, datetime, timedelta, timezone

from backend.brain import planner as P
from backend.brain import weekplan as W
from backend.storage import checkins, lifelog, plans

log = logging.getLogger(__name__)

DEFAULT_CHECKIN_TIME = "21:30"
DEFAULT_REVIEW_TIME = "19:00"
WAIT_S = 20 * 3600                   # a check-in older than this is no longer waiting for an answer
CLOSE_HOUR = 4                       # a day closes at 04:00 the next morning: a reply after midnight still counts
_NEAR_MAX_MIN = 45                   # "the gym near 18:00" must be within this of a block's start
_MAX_ITEMS = 12
_TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def clean_time(text) -> str | None:
    """'9:5' is not a time; '9:05' and '21:30' are. Returns 'HH:MM' or None."""
    m = _TIME_RE.match(str(text or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def checkin_config() -> tuple[bool, str]:
    return (bool(plans.get_setting("checkin_enabled", True)),
            clean_time(plans.get_setting("checkin_time")) or DEFAULT_CHECKIN_TIME)


def review_config() -> tuple[bool, str]:
    return (bool(plans.get_setting("review_enabled", True)),
            clean_time(plans.get_setting("review_time")) or DEFAULT_REVIEW_TIME)


def auto_move_kinds() -> list[str]:
    """The kinds of block he has said JARVIS may move himself when one is skipped or a meeting
    lands on it (a standing pre-approval; everything else is put to him first)."""
    raw = plans.get_setting("auto_move", [])
    return [k for k in raw if isinstance(k, str) and k] if isinstance(raw, list) else []


def set_auto_move(kinds) -> list[str]:
    """Replace the pre-approved kinds. Returns the cleaned list. Raises ValueError for anything
    that is not a short lowercase kind name, or for 'work'."""
    if isinstance(kinds, str):
        kinds = [k for k in re.split(r"[,\s]+", kinds) if k]
    if not isinstance(kinds, (list, tuple)):
        raise ValueError("auto_move must be a list of block kinds such as [\"gym\"]")
    clean: list[str] = []
    for k in kinds:
        kind = str(k).strip().lower()
        if kind == "work":
            raise ValueError("work blocks are not moved on their own; auto_move is for blocks such as gym")
        if not P._KIND_RE.match(kind):
            raise ValueError(f"'{k}' is not a block kind (a short lowercase name such as gym)")
        if kind not in clean:
            clean.append(kind)
    plans.set_setting("auto_move", clean)
    return clean


# ---------------------------------------------------------------------------
# The day's blocks
# ---------------------------------------------------------------------------

def _iso(d: date) -> str:
    return d.isoformat()


def _day_of(now: datetime) -> date:
    return P.local(now).date()


def _hm(ts: float) -> str:
    return f"{P.local(W._dt(ts)):%H:%M}"


def _span(row: dict) -> str:
    s, e = W._eff(row)
    return f"{_hm(s)}-{_hm(e)}"


def _rows_by_day(plan: dict | None) -> dict:
    """The live blocks of a plan by the local day they really fall on (where the owner moved one,
    there), in time order. Blocks he deleted are not in the calendar and not in the day."""
    out: dict[date, list] = {}
    for r in (plan or {}).get("blocks", []):
        if r["status"] == plans.DELETED:
            continue
        s, _ = W._eff(r)
        out.setdefault(P.local(W._dt(s)).date(), []).append(r)
    for rows in out.values():
        rows.sort(key=lambda r: (W._eff(r)[0], r["kind"]))
    return out


def live_blocks_on(day: date) -> list[dict]:
    """The blocks of the applied plan that fall on local time `day`."""
    plan = plans.applied(_iso(P.monday_of(day)))
    return _rows_by_day(plan).get(day, [])


def _grouped(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    order: list[str] = []
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        if r["kind"] not in by_kind:
            order.append(r["kind"])
        by_kind.setdefault(r["kind"], []).append(r)
    return [(k, by_kind[k]) for k in order]


def _times(rows: list[dict]) -> str:
    return ", ".join(_span(r) for r in rows)


def _long_day(day: date) -> str:
    return f"{P.day_name(day)} {day.day} {day:%b}"


# ---------------------------------------------------------------------------
# The evening check-in
# ---------------------------------------------------------------------------

def checkin_text(day: date, blocks: list[dict]) -> str:
    lines = [f"Evening check-in, {_long_day(day)}. Today's plan:"]
    for kind, rows in _grouped(blocks):
        lines.append(f"• {W._title(kind)} {_times(rows)}")
    lines.append('How did it go? Tell me in your own words, for example "gym done, stopped work at 4, '
                 'skipped the reading".')
    return "\n".join(lines)


def checkin_payload(day: date) -> tuple[str, list[str]] | None:
    """(the message, the uids it covers) for `day`, or None when nothing is planned that day or
    every block already has an outcome (he told me before I asked)."""
    blocks = live_blocks_on(day)
    if not blocks:
        return None
    done = plans.outcomes([r["uid"] for r in blocks])
    if all(r["uid"] in done for r in blocks):
        return None
    return checkin_text(day, blocks), [r["uid"] for r in blocks]


def prompt_line(now: datetime | None = None) -> str:
    """One line for the top of every turn while a check-in waits for his answer, else "".

    JARVIS cannot see his own earlier turns' tool calls, and a reply to the check-in arrives as an
    ordinary message ("gym done, skipped the reading"): without this he has no way to know what it
    answers. Never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        ci = checkins.waiting(since_ts=now.timestamp() - WAIT_S)
        if ci is None:
            return ""
        day = date.fromisoformat(ci["local_date"])
        blocks = live_blocks_on(day)
        if not blocks:
            return ""
        told = plans.outcomes([r["uid"] for r in blocks])
        still = [r for r in blocks if r["uid"] not in told]
        if not still:
            return ""
        open_text = "; ".join(f"{r['kind']} {_span(r)}" for r in still)
        part = (_owner.say(" «He» has already told you about the others.") if told else "")
        asked = (" You have already asked your one question today: do not ask again."
                 if ci.get("followup_at") else _owner.say(" Ask at most one short question, only about blocks «he» left unclear."))
        return (_owner.say(f"Evening check-in: I asked «him» at {_hm(ci['sent_at'])} how {_long_day(day)} went and «he» has not "
                f"covered {open_text} yet.{part} When «he» tells you how it went, even in passing, call week_plan "
                "report with one outcome per block «he» covered (done, partly, skipped or moved, plus «his» reason) and "
                f"«his» words in said; leave out any block «he» did not mention, do not mark it unknown. A remark about the "
                f"whole day (\"it was ok\") covers no block: record nothing and ask which; one that names a block "
                f"(\"work was fine\") is done for that block.{asked} "
                "Never guess an outcome."))
    except Exception:
        return ""


def _minutes(hhmm: str) -> int | None:
    t = clean_time(hhmm)
    return None if t is None else int(t[:2]) * 60 + int(t[3:])


def _start_minutes(row: dict) -> int:
    d = P.local(W._dt(W._eff(row)[0]))
    return d.hour * 60 + d.minute


def _match(blocks: list[dict], kind: str, near: str) -> tuple[list[dict], str]:
    """The block(s) an outcome is about, or why none. Code decides this, never the model."""
    kind = (kind or "").strip().lower()
    listing = "; ".join(f"{r['kind']} {_span(r)}" for r in blocks)
    cands = [r for r in blocks if r["kind"] == kind]
    if not cands:
        return [], f"no {kind or 'block'} is planned that day (the day has: {listing})"
    if near:
        want = _minutes(near)
        if want is None:
            return [], f"near must be a time such as 17:30, not {near!r}"
        best = min(cands, key=lambda r: abs(_start_minutes(r) - want))
        if abs(_start_minutes(best) - want) > _NEAR_MAX_MIN:
            return [], f"no {kind} starts near {near} (the {kind} blocks: {_times(cands)})"
        return [best], ""
    if kind == "work" or len(cands) == 1:
        return cands, ""
    return [], f"there are {len(cands)} {kind} blocks that day ({_times(cands)}); say which with near"


def _items(raw) -> list[dict]:
    import json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise W._Fail("outcomes must be a list of objects such as "
                          '[{"kind": "gym", "outcome": "done"}]') from None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise W._Fail("say what happened: outcomes is a list of {kind, outcome, reason, near}")
    if len(raw) > _MAX_ITEMS:
        raise W._Fail(f"at most {_MAX_ITEMS} outcomes in one report")
    if not all(isinstance(x, dict) for x in raw):
        raise W._Fail("each outcome must be an object such as {\"kind\": \"gym\", \"outcome\": \"done\"}")
    return raw


def _report_day(a: "W.Ask", now: datetime) -> date:
    today = _day_of(now)
    spec = (a.day or "").strip().lower()
    if spec in ("", "today"):
        ci = checkins.waiting(since_ts=now.timestamp() - WAIT_S)
        return date.fromisoformat(ci["local_date"]) if ci and not spec else today
    if spec == "yesterday":
        return today - timedelta(days=1)
    day = P.parse_day(spec, P.monday_of(today))
    if day is None:
        raise W._Fail(f"I can't read the day {a.day!r}; use today, yesterday, a weekday name or a date (YYYY-MM-DD)")
    if day > today:
        raise W._Fail("that day has not happened yet; an outcome is what became of a block")
    return day


_pending_proposals: list[dict] = []          # reschedule proposals to put to him, drained by the async wrapper


def drain_proposals() -> list[dict]:
    out = list(_pending_proposals)
    _pending_proposals.clear()
    return out


def report(a: "W.Ask", now: datetime) -> str:
    """`week_plan report`: what became of the day's blocks, in his words, matched by code."""
    items = _items(a.outcomes)
    day = _report_day(a, now)
    plan = plans.applied(_iso(P.monday_of(day)))
    blocks = _rows_by_day(plan).get(day, [])
    if plan is None or not blocks:
        raise W._Fail(f"there are no planned blocks on {P.day_label(day)}, so there is nothing to record")
    ci = checkins.get(_iso(day))
    asked = bool(ci and ci["sent_at"] and not ci["closed_at"])
    actor = "checkin" if asked else "owner"
    before = plans.outcomes([r["uid"] for r in blocks])
    recorded, problems, parsed, skipped = [], [], [], []
    for it in items:
        outcome = str(it.get("outcome") or "").strip().lower()
        if outcome not in plans.OUTCOMES:
            problems.append(f"{it.get('kind') or 'a block'}: outcome must be one of {', '.join(plans.OUTCOMES)}")
            continue
        rows, why = _match(blocks, str(it.get("kind") or ""), str(it.get("near") or ""))
        if not rows:
            problems.append(why)
            continue
        reason = lifelog.short(it.get("reason") or "", 200)
        for r in rows:
            prev = before.get(r["uid"])
            if prev and prev["outcome"] == outcome and prev["reason"] == reason:
                recorded.append(f"{r['kind']} {_span(r)}: {outcome} (already recorded)")
                continue
            plans.record_outcome(r["uid"], plan["id"], outcome, actor, reason=reason, now=now.timestamp())
            before[r["uid"]] = {"outcome": outcome, "reason": reason}
            parsed.append({"uid": r["uid"], "kind": r["kind"], "outcome": outcome, "reason": reason})
            recorded.append(f"{r['kind']} {_span(r)}: {outcome}" + (f" ({reason})" if reason else ""))
            if outcome == "skipped":
                skipped.append(r)
    if ci and parsed:
        checkins.record_reply(_iso(day), a.said, parsed, now=now.timestamp())
    out = []
    if recorded:
        out.append("Recorded for " + _long_day(day) + ":\n" + "\n".join(f"- {x}" for x in recorded))
    if problems:
        out.append("Not recorded:\n" + "\n".join(f"- {x}" for x in problems))
    still = [r for r in blocks if r["uid"] not in before]
    if not still:
        out.append("Every block that day now has an outcome.")
    else:
        open_text = "; ".join(f"{r['kind']} {_span(r)}" for r in still)
        if asked:
            if checkins.note_followup(_iso(day), f"still open: {open_text}", now=now.timestamp()):
                out.append(_owner.say(f"Still open: {open_text}. Ask «him» ONCE, in one short line, about these. If «he» does not "
                           "know or does not answer they are recorded as unknown at the end of the day; never "
                           "guess an outcome."))
            else:
                out.append(f"Still open: {open_text}. You already asked once today; leave them. They are recorded "
                           "as unknown at the end of the day.")
        else:
            out.append(f"Not reported yet: {open_text}.")
    if skipped:
        try:
            from backend.brain import reshuffle
            out.extend(reshuffle.after_skip(skipped, plan, day, now))
        except Exception as exc:
            log.warning("planday: reschedule proposal failed (%s)", exc)
    return "\n".join(out)


TAP_OUTCOMES = ("done", "partly", "skipped", "moved")


def _tappable(uid: str, now: datetime) -> tuple[date, dict, dict]:
    """(day, plan, block) for a block of today's plan, or of yesterday's while that day is open."""
    today = _day_of(now)
    for day in (today, today - timedelta(days=1)):
        if day < today and closable({"local_date": _iso(day)}, now):
            break                                  # yesterday has closed (04:00): its outcomes are final
        plan = plans.applied(_iso(P.monday_of(day)))
        for row in _rows_by_day(plan).get(day, []):
            if row["uid"] == uid:
                return day, plan, row
    raise W._Fail("that block is not in today's plan (yesterday's closes at 04:00)")


def record_tap(uid: str, outcome: str, now: datetime, proposals: list | None = None) -> str:
    """The phone's Today tab: what became of one block, tapped (slice 2). The block is named by
    its uid, so nothing is matched or guessed. The same record a reported outcome makes: actor
    "checkin" while the evening check-in waits for that day, "owner" otherwise, so the check-in
    and JARVIS's per-turn line no longer ask about it. A block can be skipped before it starts
    (and is then offered a new slot); done, partly and another time only once it has started.
    A skip's new slot, when it needs his approval, goes into `proposals` (the caller puts it to him;
    planday's shared list belongs to a chat turn's report). Raises W._Fail with the reason."""
    if outcome not in TAP_OUTCOMES:
        raise W._Fail(f"outcome must be one of {', '.join(TAP_OUTCOMES)}")
    day, plan, row = _tappable(uid, now)
    start, _ = W._eff(row)
    if outcome != "skipped" and start > now.timestamp():
        raise W._Fail(f"{W._title(row['kind'])} {_span(row)} has not started yet")
    ci = checkins.get(_iso(day))
    actor = "checkin" if ci and ci["sent_at"] and not ci["closed_at"] else "owner"
    prev = plans.outcomes([uid]).get(uid)
    label = f"{W._title(row['kind'])} {_span(row)}"
    if prev and prev["outcome"] == outcome:
        return f"{label}: {outcome} (already recorded)."
    plans.record_outcome(uid, plan["id"], outcome, actor, now=now.timestamp())
    if ci:
        checkins.record_reply(_iso(day), "", [{"uid": uid, "kind": row["kind"], "outcome": outcome,
                                               "reason": "", "via": "app"}], now=now.timestamp())
    lines = [f"{label}: {outcome}."]
    if outcome == "skipped":
        try:
            from backend.brain import reshuffle
            lines.extend(reshuffle.after_skip([row], plan, day, now,
                                              queue=proposals if proposals is not None else []))
        except Exception as exc:
            log.warning("planday: reschedule proposal failed (%s)", exc)
    return "\n".join(lines)


def close_day(row: dict, now: datetime) -> int:
    """The day of check-in `row` is over. If he was asked, every block nobody answered for is
    recorded as unknown (never guessed); then the check-in is closed. Returns how many."""
    day_iso = row["local_date"]
    n = 0
    if row.get("sent_at"):
        day = date.fromisoformat(day_iso)
        plan = plans.applied(_iso(P.monday_of(day)))
        blocks = _rows_by_day(plan).get(day, [])
        told = plans.outcomes([r["uid"] for r in blocks])
        for r in blocks:
            if r["uid"] not in told:
                plans.record_outcome(r["uid"], plan["id"], "unknown", "system",
                                     reason="no answer at the evening check-in", now=now.timestamp())
                n += 1
    checkins.close(day_iso, now=now.timestamp())
    return n


def closable(row: dict, now: datetime) -> bool:
    """A day closes at 04:00 the next morning (a reply after midnight is still about that day)."""
    local = P.local(now)
    day = date.fromisoformat(row["local_date"])
    today = local.date()
    return day < today - timedelta(days=1) or (day < today and local.hour >= CLOSE_HOUR)


# ---------------------------------------------------------------------------
# Reading the plan back
# ---------------------------------------------------------------------------

def _line(row: dict, outcome: dict | None) -> str:
    bits = []
    if outcome:
        bits.append(outcome["outcome"] + (f", {outcome['reason']}" if outcome.get("reason") else ""))
    if row["status"] == plans.MOVED:
        bits.append("you moved it")
    elif row["status"] == plans.FAILED:
        bits.append("not in the calendar")
    return f"  {_span(row)} {row['kind']}" + (f"  ({'; '.join(bits)})" if bits else "")


def day_text(day: date, heading: str = "") -> str:
    blocks = live_blocks_on(day)
    head = heading or _long_day(day)
    if not blocks:
        return f"{head}: nothing planned."
    told = plans.outcomes([r["uid"] for r in blocks])
    return "\n".join([head] + [_line(r, told.get(r["uid"])) for r in blocks])


def todays_blocks_text(now: datetime | None = None) -> str:
    """Today's planned blocks in one line each, for the morning brief; "" when none."""
    try:
        now = now or datetime.now(timezone.utc)
        blocks = live_blocks_on(_day_of(now))
        return "\n".join(f"- {_span(r)} {r['kind']}" for r in blocks)
    except Exception:
        return ""


def week_text(monday: date, today: date) -> str:
    plan = plans.applied(_iso(monday))
    if plan is None:
        return f"No plan in the calendar for the week of {P.day_label(monday)}."
    by_day = _rows_by_day(plan)
    told = plans.outcomes([r["uid"] for rows in by_day.values() for r in rows])
    lines = [f"Week of {P.day_label(monday)} (plan version {plan['version']})"]
    for offset in range(7):
        d = monday + timedelta(days=offset)
        rows = by_day.get(d, [])
        if not rows:
            continue
        marker = "  <- today" if d == today else ""
        lines.append(f"{P.day_label(d)}{marker}")
        lines.extend(_line(r, told.get(r["uid"])) for r in rows)
    if len(lines) == 1:
        lines.append("  (no blocks)")
    return "\n".join(lines)


def _kind_stats(r: dict) -> str:
    bits = [f"{r['done']} of {r['planned']} done"]
    for key, word in (("partly", "partly"), ("skipped", "skipped"), ("moved", "moved"),
                      ("unknown", "unknown"), ("unreported", "not reported")):
        if r[key]:
            bits.append(f"{r[key]} {word}")
    return f"{r['kind']} " + ", ".join(bits)


def stats_text(today: date | None = None, weeks: int = 4) -> str:
    """The adherence view, newest week first: planned against what happened, per kind."""
    today = today or _day_of(datetime.now(timezone.utc))
    since = _iso(P.monday_of(today) - timedelta(days=7 * (weeks - 1)))
    rows = plans.adherence(since_week=since)
    if not rows:
        return "No plan history in the last few weeks yet."
    by_week: dict[str, list] = {}
    for r in rows:
        by_week.setdefault(r["week_start"], []).append(r)
    lines = []
    for wk in sorted(by_week, reverse=True):
        monday = date.fromisoformat(wk)
        lines.append(f"Week of {monday.day} {monday:%b}: " + "; ".join(_kind_stats(r) for r in by_week[wk]))
    return "\n".join(lines)


def _settings_text() -> str:
    on, at = checkin_config()
    r_on, r_at = review_config()
    moves = auto_move_kinds()
    return (f"Evening check-in: {'every day at ' + at if on else 'off'}. "
            f"Weekly review: {'Sundays at ' + r_at if r_on else 'off'}. "
            f"Pre-approved moves: {', '.join(moves) if moves else 'none (I ask first)'}.")


_HELP = ("Commands: /plan (today and the rest of the week), /plan week, /plan stats, "
         "/plan checkin HH:MM|on|off, /plan review HH:MM|on|off.")


def _switch(word: str, setting: str, time_key: str, what: str) -> str:
    on_key = f"{setting}_enabled"
    if word == "off":
        plans.set_setting(on_key, False)
        return f"{what} off."
    if word == "on":
        plans.set_setting(on_key, True)
        return f"{what} on."
    t = clean_time(word)
    if t is None:
        return f"'{word}' isn't a time or on/off. {_HELP}"
    plans.set_setting(time_key, t)
    plans.set_setting(on_key, True)
    return f"{what} set to {t} local time."


def handle_plan_command(arg: str, now: datetime | None = None) -> str:
    """Discord '/plan ...'. Read-only apart from the two switches; never touches the calendar."""
    text = (arg or "").strip().lower()
    now = now or datetime.now(timezone.utc)
    today = _day_of(now)
    if text in ("", "today"):
        week = plans.newest_week(_iso(P.monday_of(today)))
        if week is None or plans.applied(week) is None:
            return "There is no plan in your calendar yet. Ask me to organise your week.\n" + _settings_text()
        out = [day_text(today, f"Today, {_long_day(today)}")]
        rest = week_text(P.monday_of(today), today) if week == _iso(P.monday_of(today)) else \
            week_text(date.fromisoformat(week), today)
        out.append(rest)
        out.append(_settings_text())
        return "\n\n".join(out)
    if text == "week":
        week = plans.newest_week(_iso(P.monday_of(today))) or _iso(P.monday_of(today))
        return week_text(date.fromisoformat(week), today)
    if text in ("stats", "outcomes", "log"):
        return stats_text(today)
    if text.startswith("checkin"):
        word = text[len("checkin"):].strip()
        if not word:
            on, at = checkin_config()
            return f"Evening check-in: {'every day at ' + at if on else 'off'}. Change it: /plan checkin HH:MM|on|off."
        return _switch(word, "checkin", "checkin_time", "Evening check-in")
    if text.startswith("review"):
        word = text[len("review"):].strip()
        if not word:
            on, at = review_config()
            return f"Weekly review: {'Sundays at ' + at if on else 'off'}. Change it: /plan review HH:MM|on|off."
        return _switch(word, "review", "review_time", "Weekly review")
    return f"'{arg.strip()}' isn't something I understand. {_HELP}"


# ---------------------------------------------------------------------------
# What /activity shows
# ---------------------------------------------------------------------------

def scheduled_items(now: float) -> list[tuple[float, str]]:
    """The plan loop's own upcoming work as (time, words), for the /activity view: the next evening
    check-in, the weekly review, and the 30-minute look at his calendar. Nothing when there is no
    plan in his calendar (the loop has nothing to keep true)."""
    when_dt = datetime.fromtimestamp(now, timezone.utc)
    today = _day_of(when_dt)
    if not any(plans.applied(_iso(P.monday_of(today) + timedelta(days=7 * k))) for k in (0, 1)):
        return []
    out: list[tuple[float, str]] = []
    enabled, at_time = checkin_config()
    if enabled:
        for k in range(8):
            d = today + timedelta(days=k)
            blocks = live_blocks_on(d)
            row = checkins.get(_iso(d))
            if not blocks or (row and row["sent_at"]):
                continue
            out.append((P.at(d, at_time).timestamp(),
                        f"evening check-in for {_long_day(d)} ({len(blocks)} planned block(s))"))
            break
    r_on, r_at = review_config()
    if r_on:
        sunday = today + timedelta(days=(6 - today.weekday()) % 7)
        done_week = plans.get_setting("last_review_week") == _iso(P.monday_of(sunday))
        if done_week or P.at(sunday, r_at).timestamp() < now - 3600:
            sunday += timedelta(days=7)
        out.append((P.at(sunday, r_at).timestamp(), "weekly review, and next week drawn as a proposal"))
    last = float(plans.get_setting("last_scan_at", 0.0) or 0.0)
    nxt = max(now, last + 30 * 60)
    local = P.local(datetime.fromtimestamp(nxt, timezone.utc))
    if not (7 <= local.hour < 23):
        day = local.date() + timedelta(days=1 if local.hour >= 23 else 0)
        nxt = P.at(day, "07:00").timestamp()
    out.append((nxt, "look at your calendar for meetings landing on plan blocks (every 30 minutes, 07:00-23:00)"))
    return out


def done_items(since: float) -> list[tuple[float, str]]:
    """Check-ins sent since `since`, with how much of the day he answered: (time, words)."""
    out: list[tuple[float, str]] = []
    for row in checkins.recent(5):
        if not row.get("sent_at") or row["sent_at"] < since:
            continue
        day = date.fromisoformat(row["local_date"])
        blocks = live_blocks_on(day)
        told = plans.outcomes([r["uid"] for r in blocks])
        out.append((row["sent_at"], f"evening check-in for {_long_day(day)}: "
                                    f"{len(told)} of {len(blocks)} block(s) answered"
                                    + (" (closed)" if row.get("closed_at") else "")))
    return out

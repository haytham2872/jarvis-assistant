"""backend/brain/chronos.py — JARVIS's relationship with the future.

Everything else in the brain is reactive (the owner speaks) or interval-paced
(every N minutes, whatever the clock says). This module is the temporal layer:
acting AT a time, in the owner's timezone. Three capabilities on one loop:

  Reminders     — "remind me to call the bank tomorrow at 10" (usually from
                  the phone). Stored in jarvis-vault/schedule.md (pipe format,
                  Obsidian-visible, same family as research-queue.md), fired
                  as a Discord DM at the due minute. once | daily.
  Scheduled tasks — "at 15:30 every weekday, check NVDA's price and tell me." A
                  lookup that runs AT a time: the background worker does it then and
                  its answer is DMed. once | daily | weekdays | weekly, an optional
                  end date, and a timezone per task (see "Scheduled tasks" below).
  Night tasks   — "research X tonight and tell me in the morning." Queued in
                  the same file, executed during a quiet night window
                  (_NIGHT_START..-END, skipped while the pipeline is busy) via
                  background.execute_task() — the same bounded autonomous tool
                  loop background tasks use — results written to drafts/ and
                  saved for the morning brief. Optionally carries an `after`
                  YYYY-MM-DD gate date (e.g. a task about a future event) so
                  it doesn't fire — and report on something that hasn't
                  happened yet — on the very next quiet window regardless.
  Morning brief — one DM at a configurable time (default 08:00): night task
                  findings, research passes since yesterday, open intents,
                  today's reminders, today's calendar. Composed by one cheap
                  LLM call (research._llm, the standard tier,
                  usage-recorded); degrades to a plain formatted digest if the
                  model call fails. "/brief" Discord command configures it
                  ("/brief 07:30", "/brief off|on", "/brief now").

ALL times are in the owner's zone (JARVIS_TIMEZONE, backend/localtime.py; ZoneInfo, so
clock changes are handled), because that's where the owner is. The same _TZ powers the [Now] line personality.py injects into
every turn's system prompt, so the model computing "tomorrow at 10" from
conversation and this module firing the result agree on what time it is.

Same DI pattern as research.py/backup_loop.py (never imports backend.main;
notifier + busy check injected at startup), same silent-degradation contract
(any failure logs and waits for the next cycle), same state-file family
(.jarvis_chronos.json for operational state, the vault file for content).
"""
from backend import localtime as _localtime
from backend import owner as _owner

import re
import json
import asyncio
import logging
import datetime
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable, Optional

from backend.brain import pacing
from backend.brain import notices as _notices
from backend.brain import pause

log = logging.getLogger(__name__)

_PROJECT_ROOT   = Path(__file__).parent.parent.parent
_STATE_FILE     = _PROJECT_ROOT / ".jarvis_chronos.json"
_SCHEDULE_FILE  = _PROJECT_ROOT / "jarvis-vault" / "schedule.md"

_TZ = _localtime.ZONE   # The owner's timezone — every time in this module is local

_CHECK_INTERVAL_S = 30.0         # loop cadence — reminders should fire within ~30s of due

_MAX_PENDING_REMINDERS = 25
_MAX_PENDING_TASKS     = 20
_MAX_PENDING_NIGHT     = 3       # night tasks are LLM tool loops — keep the queue an intention list
_MAX_DONE_LINES        = 40      # fired/completed trail cap in schedule.md (oldest trimmed)

# Night-task execution window (local hours). Chosen to sit after the
# evening's conversations and before the morning brief, when the machine is
# idle and API traffic is cheapest to spare.
_NIGHT_START_HOUR = 2
_NIGHT_END_HOUR   = 7    # exclusive — tasks run while 2:00 <= now.hour < 7:00

_DEFAULT_BRIEF_TIME = "08:00"
_BRIEF_MAX_TOKENS   = 2500       # k2.5 reasoning shares this budget (see research.py's picker lesson)

_SCHEDULE_HEADER = [
    "# Schedule",
    "",
    _owner.say("Reminders, scheduled tasks and night tasks JARVIS is holding. Managed by «him» via the"),
    "schedule_reminder / schedule_task / schedule_night_task tools; fired by",
    "backend/brain/chronos.py.",
    _owner.say("All times «tz»."),
    "",
]

_notifier:   Optional[Callable[[str], Awaitable[bool]]] = None
_busy_check: Optional[Callable[[], bool]] = None


def set_notifier(fn: Callable[[str], Awaitable[bool]]) -> None:
    """Injected from main.py: async fn(text) -> bool that DMs the owner."""
    global _notifier
    _notifier = fn


def set_busy_check(fn: Callable[[], bool]) -> None:
    """Injected: returns True while a conversation turn holds the pipeline lock."""
    global _busy_check
    _busy_check = fn


# ---------------------------------------------------------------------------
# Time — one source of truth for "now" in the owner's world
# ---------------------------------------------------------------------------

def now_local() -> datetime.datetime:
    return datetime.datetime.now(_TZ)


def now_line() -> str:
    """The [Now] line personality.py injects into every system prompt, so the
    model always knows the owner's exact current local time (and can turn
    'tomorrow at 10' into an absolute datetime for schedule_reminder)."""
    now = now_local()
    return _owner.say(f"[Now] {now.strftime('%A %Y-%m-%d %H:%M')} («tz» — «owner's» local time)")


# ---------------------------------------------------------------------------
# State (.jarvis_chronos.json) — brief config, night results, parked mentions
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    return pacing.load_state(_STATE_FILE)


def _save_state(state: dict) -> None:
    pacing.save_state(_STATE_FILE, state, log_prefix="chronos")


def _update_state(mutate) -> dict:
    """Atomic read-modify-write. Use this, not _load_state/_save_state around an await —
    see pacing.update_state for the lost-update this prevents."""
    return pacing.update_state(_STATE_FILE, mutate, log_prefix="chronos")


def get_brief_config() -> tuple[bool, str]:
    """(enabled, 'HH:MM'). Defaults: enabled at 08:00."""
    state = _load_state()
    time_str = str(state.get("brief_time", _DEFAULT_BRIEF_TIME))
    if not re.fullmatch(r"\d{2}:\d{2}", time_str):
        time_str = _DEFAULT_BRIEF_TIME
    return bool(state.get("brief_enabled", True)), time_str


# ---------------------------------------------------------------------------
# schedule.md — reminders + night tasks, pipe format
#   [reminder] 2026-07-08 10:00 | once | call the bank
#   [reminder] 2026-07-08 08:30 | daily | morning stretch   (date = next fire)
#   [night] 2026-07-07 | after= | research X and summarize findings
#   [night] 2026-09-08 | after=2026-09-16 | report the FOMC decision results
#   [done] 2026-07-08 10:00 | reminder fired: call the bank
#
# [night] lines: field 1 is the date the task was QUEUED (not a fire gate);
# `after=` is the optional real gate (see night_task_due()). A bare
# "[night] date | task" (no after= field) is the pre-gate legacy shape and
# still parses -- treated as after="" (no gating, fires next window), same
# as an explicit `after=`.
# ---------------------------------------------------------------------------

def _read_schedule(*, strict: bool = False) -> list[str]:
    try:
        return _SCHEDULE_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception as exc:
        if strict:
            log.warning("chronos: schedule could not be read (%s)", exc)
            raise
        return []


def _write_schedule(lines: list[str]) -> None:
    """Raises on failure -- callers that report a confirmation to the model
    (schedule_reminder/schedule_night_task) must see real write failures, not
    a silently-swallowed one that lets a false "done" through (2026-09-08
    incident: a swallowed exception here meant the tool could still return
    its success string even though nothing was persisted). Loop-internal
    callers (_fire_due_reminders/_run_night_task, invoked from _cycle() via
    pacing.run_periodic) are already protected by run_periodic's own
    catch-and-log-and-continue wrapper, so raising here doesn't risk the loop."""
    _SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SCHEDULE_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_schedule_verified(lines: list[str], target_line: str) -> bool:
    """Write schedule.md, then read it back and confirm `target_line` is
    actually on disk. Returns False (never raises) on any failure -- callers
    use this to decide whether a "done" claim is safe to make."""
    try:
        _write_schedule(lines)
        return target_line in _read_schedule()
    except Exception as e:
        log.warning("chronos: schedule write+verify failed: %s", e)
        return False


def parse_when(when: str) -> Optional[datetime.datetime]:
    """'YYYY-MM-DD HH:MM' (or with 'T') -> aware local time datetime, None if unparseable."""
    when = (when or "").strip().replace("T", " ")
    try:
        dt = datetime.datetime.strptime(when, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=_TZ)
    except Exception:
        return None


def _format_reminder_line(due: datetime.datetime, repeat: str, message: str) -> str:
    return f"[reminder] {due.strftime('%Y-%m-%d %H:%M')} | {repeat} | {message}"


def _parse_reminder_line(line: str) -> Optional[dict]:
    stripped = line.strip()
    if not stripped.startswith("[reminder]"):
        return None
    parts = [p.strip() for p in stripped[len("[reminder]"):].split("|")]
    if len(parts) < 3 or not parts[2]:
        return None
    due = parse_when(parts[0])
    if due is None:
        return None
    repeat = parts[1].lower() if parts[1].lower() in ("once", "daily") else "once"
    return {"due": due, "repeat": repeat, "message": parts[2]}


def _format_night_line(queued_date: str, after: str, task: str) -> str:
    return f"[night] {queued_date} | after={after} | {task}"


def _parse_night_line(line: str) -> Optional[dict]:
    """Parses both the current 3-field format ('date | after=YYYY-MM-DD | task',
    `after` may be empty) and the legacy 2-field format ('date | task') written
    before night tasks could carry a gating date -- a line already sitting in
    schedule.md when this ships must keep parsing with after=""."""
    stripped = line.strip()
    if not stripped.startswith("[night]"):
        return None
    parts = [p.strip() for p in stripped[len("[night]"):].split("|")]
    if len(parts) < 2 or not parts[-1]:
        return None
    date = parts[0]
    after = ""
    if len(parts) >= 3 and parts[1].startswith("after="):
        after = parts[1][len("after="):].strip()
    return {"date": date, "after": after, "task": parts[-1]}


def night_task_due(after: str, now: datetime.datetime) -> bool:
    """Pure gate: has an optional 'after' YYYY-MM-DD date arrived? Empty
    `after` means no gating date was set -- due on the very next quiet
    window, the only behavior night tasks had before this field existed.
    A malformed date fails open (due now) rather than stranding the task
    in the queue forever with no way to fire."""
    after = (after or "").strip()
    if not after:
        return True
    try:
        gate_date = datetime.datetime.strptime(after, "%Y-%m-%d").date()
    except ValueError:
        return True
    return now.date() >= gate_date


def _append_done(lines: list[str], text: str, now: datetime.datetime) -> list[str]:
    """Append a [done] trail line and trim the oldest past the cap."""
    lines.append(f"[done] {now.strftime('%Y-%m-%d %H:%M')} | {text}")
    done_idx = [i for i, l in enumerate(lines) if l.strip().startswith("[done]")]
    excess = len(done_idx) - _MAX_DONE_LINES
    if excess > 0:
        for i in reversed(done_idx[:excess]):
            del lines[i]
    return lines


def next_daily_occurrence(due: datetime.datetime, now: datetime.datetime) -> datetime.datetime:
    """First future occurrence of a daily reminder after `now`, preserving the
    time of day. Loops (rather than adding exactly one day) so a reminder that
    went unfired for several days while the backend was off doesn't refire
    once per loop cycle catching up."""
    while due <= now:
        due = due + datetime.timedelta(days=1)
    return due


def in_night_window(now: datetime.datetime) -> bool:
    """Pure gate: is `now` inside the night-task execution window?"""
    return _NIGHT_START_HOUR <= now.hour < _NIGHT_END_HOUR


def brief_due(enabled: bool, brief_time: str, last_brief_date: str, now: datetime.datetime) -> bool:
    """Pure gate: should the morning brief fire now?

    Fires once per calendar day, any time at-or-after the configured time --
    so a backend that was off at 08:00 still sends the (late) brief when it
    comes up at 11:00, rather than silently skipping the day.
    """
    if not enabled:
        return False
    if last_brief_date == now.strftime("%Y-%m-%d"):
        return False
    try:
        hh, mm = brief_time.split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        return False
    return now >= target


def _parse_done_line(line: str) -> Optional[dict]:
    stripped = line.strip()
    if not stripped.startswith("[done]"):
        return None
    head, _, text = stripped[len("[done]"):].partition("|")
    at = parse_when(head.strip())
    if at is None or not text.strip():
        return None
    return {"at": at, "text": text.strip()}


def agenda(*, strict: bool = False) -> dict:
    """P2: what chronos is holding, parsed and in time order -- for /activity. Read-only.

    `reminders`, `tasks` (timed lookups), `night` (queued overnight work), `done` (the trail
    of what fired, newest last), the `brief` setting, and how many reminders wait to be told
    in the next conversation. Parsing stays here, where the formats are owned.
    """
    lines = _read_schedule(strict=True) if strict else _read_schedule()
    if strict and any(line.strip().startswith("[done]") and not _parse_done_line(line) for line in lines):
        log.warning("chronos: invalid schedule trail line")
        raise ValueError("Invalid schedule trail")
    reminders = sorted((r for r in map(_parse_reminder_line, lines) if r), key=lambda r: r["due"])
    tasks = sorted((t for t in map(_parse_task_line, lines) if t), key=lambda t: t["due"])
    night = [n for n in map(_parse_night_line, lines) if n]
    done = sorted((d for d in map(_parse_done_line, lines) if d), key=lambda d: d["at"])
    state = pacing.load_state(_STATE_FILE, strict=True) if strict else _load_state()
    enabled, brief_time = get_brief_config()
    return {
        "reminders": reminders, "tasks": tasks, "night": night, "done": done,
        "brief": {"enabled": enabled, "time": brief_time,
                  "last_date": str(state.get("last_brief_date", ""))},
        "parked": len(state.get("pending_mentions", []) or []),
        "night_results": len(state.get("night_results", []) or []),
        "night_details": list(state.get("night_results", []) or []),
    }


# ---------------------------------------------------------------------------
# Registered tools
# ---------------------------------------------------------------------------

async def schedule_reminder(action: str, when: str = "", message: str = "", repeat: str = "once") -> str:
    """Registered tool: add / list / cancel reminders. Times are local
    'YYYY-MM-DD HH:MM' — the [Now] line in the system prompt gives the model
    the current time to compute absolutes from. Never raises."""
    try:
        action = (action or "").strip().lower()
        lines = _read_schedule() or list(_SCHEDULE_HEADER)

        if action == "list":
            reminders = [r for r in (_parse_reminder_line(l) for l in lines) if r]
            nights = [n for n in (_parse_night_line(l) for l in lines) if n]
            tasks = [t for t in (_parse_task_line(l) for l in lines) if t]
            if not reminders and not nights and not tasks:
                return "Nothing scheduled."
            out = []
            for r in sorted(reminders, key=lambda r: r["due"]):
                out.append(f"- {r['due'].strftime('%Y-%m-%d %H:%M')} ({r['repeat']}): {r['message']}")
            for t in sorted(tasks, key=lambda t: t["due"]):
                out.append(f"- scheduled task, {_describe_task(t)}")
            for n in nights:
                when_txt = f"next quiet window on/after {n['after']}" if n["after"] else "next quiet window"
                out.append(f"- {when_txt}: {n['task']}")
            return "Scheduled (jarvis-vault/schedule.md):\n" + "\n".join(out)

        if action == "cancel":
            needle = (message or "").strip().lower()
            if len(needle) < 3:
                return "Error: give me part of the reminder text to cancel."
            for i, l in enumerate(lines):
                r = _parse_reminder_line(l)
                if r and needle in r["message"].lower():
                    del lines[i]
                    _write_schedule(lines)
                    return f"Cancelled: {r['message']}"
            return f"No reminder matching '{message}'."

        if action != "add":
            return f"Error: unknown action '{action}'. Use 'add', 'list', or 'cancel'."

        message = (message or "").strip()
        if len(message) < 2:
            return "Error: a reminder needs a message."
        due = parse_when(when)
        if due is None:
            return ("Error: 'when' must be local time formatted YYYY-MM-DD HH:MM "
                    "(compute it from the current time in your context).")
        now = now_local()
        if due <= now:
            return (_owner.say(f"Error: {due.strftime('%Y-%m-%d %H:%M')} is in the past — it is "
                    f"{now.strftime('%Y-%m-%d %H:%M')} right now («tz»)."))
        repeat = (repeat or "once").strip().lower()
        if repeat not in ("once", "daily"):
            return "Error: repeat must be 'once' or 'daily'."
        pending = sum(1 for l in lines if l.strip().startswith("[reminder]"))
        if pending >= _MAX_PENDING_REMINDERS:
            return f"Error: {_MAX_PENDING_REMINDERS} reminders already pending — cancel some first."
        new_line = _format_reminder_line(due, repeat, message)
        lines.append(new_line)
        if not _write_schedule_verified(lines, new_line):
            return (_owner.say("Error: the reminder did not persist — a read-back of jarvis-vault/schedule.md "
                     "right after writing did not find it. Do not tell «owner» this is set; retry or "
                     "report the failure."))
        return (f"Confirmed written to jarvis-vault/schedule.md: \"{new_line}\". "
                f"Reminder set for {due.strftime('%A %Y-%m-%d %H:%M')} local time"
                + (" (repeats daily)" if repeat == "daily" else "")
                + f": {message}" + pause.tool_note(pause.ALL))
    except Exception as e:
        log.warning("chronos: schedule_reminder failed: %s", e)
        return f"Error: scheduling unavailable ({e})"


async def schedule_night_task(task: str, after: str = "") -> str:
    """Registered tool: queue a task to run autonomously during a future
    2-7am local time quiet window; findings are delivered in the morning brief.

    `after` (optional) is a local 'YYYY-MM-DD' date gate: the task will
    NOT fire on any quiet window before that date, e.g. an event that hasn't
    happened yet (an earnings call, a meeting decision, an announcement).
    Leave it empty for "run on the very next available quiet window" -- the
    only behavior this tool had before `after` existed. Without a gate, a
    task about a future event can fire and report on it before it happened."""
    try:
        task = (task or "").strip()
        if len(task) < 10:
            return "Error: describe the task in a full sentence — it runs with no conversation context."
        after = (after or "").strip()
        if after:
            try:
                datetime.datetime.strptime(after, "%Y-%m-%d")
            except ValueError:
                return "Error: 'after' must be a local date formatted YYYY-MM-DD."
        lines = _read_schedule() or list(_SCHEDULE_HEADER)
        nights = [n for n in (_parse_night_line(l) for l in lines) if n]
        if any(task.lower() == n["task"].lower() for n in nights):
            return "That task is already queued for tonight."
        if len(nights) >= _MAX_PENDING_NIGHT:
            return f"Error: {_MAX_PENDING_NIGHT} night tasks already queued — that's the cap per night."
        new_line = _format_night_line(now_local().strftime("%Y-%m-%d"), after, task)
        lines.append(new_line)
        if not _write_schedule_verified(lines, new_line):
            return (_owner.say("Error: the night task did not persist — a read-back of jarvis-vault/schedule.md "
                     "right after writing did not find it. Do not tell «owner» this is queued; retry "
                     "or report the failure."))
        enabled, brief_time = get_brief_config()
        delivery = f"the {brief_time} morning brief" if enabled else "the morning brief (currently OFF — '/brief on' to enable)"
        gate_note = f" It will not fire before {after}." if after else ""
        return (f"Confirmed written to jarvis-vault/schedule.md (night_tasks): \"{new_line}\"."
                f"{gate_note} Runs in a future {_NIGHT_START_HOUR}:00-{_NIGHT_END_HOUR}:00 local quiet "
                f"window; findings will be in {delivery}." + pause.tool_note())
    except Exception as e:
        log.warning("chronos: schedule_night_task failed: %s", e)
        return f"Error: scheduling unavailable ({e})"


# ---------------------------------------------------------------------------
# Scheduled tasks — a lookup that runs AT a time and messages its answer
#   [task] 2026-09-30 09:30 | weekdays | until=2026-10-02 | tz=America/New_York | Check ...
#   [task] 2026-10-05 09:00 | weekly | until= | tz=the owner's zone | skill=stock-research | ...
#
# A routine is a scheduled task tied to a skill (backend/brain/skills.py): each run loads
# the skill as it is THEN, so improving the skill improves every later run. The `skill=`
# field is optional and sits before the task text; lines without it read as before.
#
# Why not a reminder: a reminder sends its own fixed text. Why not a night task: those
# run only 2-7am. Why not a watch: a watch fires when a condition turns true, checked
# at best hourly. "Tell me NVDA's price at the open" needs the lookup done AT 15:30.
#
# Field 1 is always the NEXT occurrence, as wall-clock time in the task's own timezone
# (default local time). A timezone per task is what keeps "the 09:30 New York open" right
# for the week each autumn and spring when the US and Europe change clocks on
# different dates (in 2026: local time on 25 October, New York on 1 November, so the open
# is 14:30 local time that week, not 15:30). The task text is the last field and may
# contain "|".
#
# An occurrence found more than _TASK_GRACE_S late (the backend was down) is skipped
# and reported as missed rather than run: a price "at the open" delivered five hours
# late would be wrong, not late. Each run goes through the background worker (its
# tools, its receipts, its restart handling); its answer is DMed to the owner as it is
# and reaches the next turn as an already-sent notice, so he can ask a follow-up.
# No result note is written: a stored "NVDA is $227" would later resurface in recall
# as if it were current.
# ---------------------------------------------------------------------------

_TASK_REPEATS = ("once", "daily", "weekdays", "weekly")
_TASK_GRACE_S = 20 * 60
_TASK_MAX_DAYS = 366
_TASK_MIN_CHARS = 10

# G0 / N1: an occurrence that has come due is recorded in the state file
# (`due_task_runs`) BEFORE schedule.md moves on to the next one, and forgotten only once
# its job is admitted, reported missed, or skipped by a pause. Until 5 October 2026 a run
# waiting for a free slot lived only in a module list, so a restart inside the 20-minute
# grace lost it without a word. Each record's key is also stored on the queued job
# (background payload "occurrence"), so a crash between admission and forgetting cannot
# start the same run twice.


def _zone(name: str) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo((name or "").strip() or _localtime.TIMEZONE)
    except Exception:
        return None


def _format_task_line(due: datetime.datetime, repeat: str, until: str, tz_name: str,
                      task: str, skill: str = "") -> str:
    follow = f"skill={skill} | " if skill else ""
    return (f"[task] {due.strftime('%Y-%m-%d %H:%M')} | {repeat} | until={until} | "
            f"tz={tz_name} | {follow}{task}")


def _parse_task_line(line: str) -> Optional[dict]:
    stripped = line.strip()
    if not stripped.startswith("[task]"):
        return None
    parts = [p.strip() for p in stripped[len("[task]"):].split("|", 4)]
    if len(parts) < 5 or not parts[4]:
        return None
    tz_name = parts[3][len("tz="):].strip() if parts[3].startswith("tz=") else _localtime.TIMEZONE
    zone = _zone(tz_name)
    if zone is None:
        return None
    try:
        naive = datetime.datetime.strptime(parts[0].replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    repeat = parts[1].lower() if parts[1].lower() in _TASK_REPEATS else "once"
    until = parts[2][len("until="):].strip() if parts[2].startswith("until=") else ""
    task, skill = parts[4], ""
    if task.startswith("skill="):
        head, _, rest = task.partition("|")
        skill, task = head[len("skill="):].strip(), rest.strip()
        if not task:
            return None
    return {"due": naive.replace(tzinfo=zone), "repeat": repeat, "until": until,
            "tz": tz_name, "task": task, "skill": skill}


def _until_date(until: str) -> Optional[datetime.date]:
    try:
        return datetime.datetime.strptime(until, "%Y-%m-%d").date() if until else None
    except ValueError:
        return None


def _roll_to_weekday(due: datetime.datetime) -> datetime.datetime:
    while due.weekday() >= 5:
        due = datetime.datetime.combine(due.date() + datetime.timedelta(days=1),
                                        due.time(), tzinfo=due.tzinfo)
    return due


def next_task_occurrence(due: datetime.datetime, repeat: str,
                         now: datetime.datetime) -> Optional[datetime.datetime]:
    """The first occurrence after both `due` and `now`, at the same wall-clock time in
    `due`'s own timezone; None for a one-off. Steps by calendar date and rebuilds the
    time, so a daily 09:30 New York task stays at 09:30 there across a clock change.
    Skips every missed occurrence in one call (a backend off for a week fires once)."""
    if repeat not in ("daily", "weekdays", "weekly"):
        return None
    step = datetime.timedelta(days=7 if repeat == "weekly" else 1)
    day = due.date()
    while True:
        day += step
        candidate = datetime.datetime.combine(day, due.time(), tzinfo=due.tzinfo)
        if repeat == "weekdays" and candidate.weekday() >= 5:
            continue
        if candidate > now:
            return candidate


def _occurrences(due: datetime.datetime, repeat: str, until: str, limit: int = 8) -> list:
    """The upcoming run times, for the confirmation. Pure."""
    end = _until_date(until)
    out = [due]
    while len(out) < limit and repeat != "once":
        nxt = next_task_occurrence(out[-1], repeat, out[-1])
        if nxt is None or (end and nxt.date() > end):
            break
        out.append(nxt)
    return out


def local_shifts(due: datetime.datetime, repeat: str, until: str, horizon_days: int = 183) -> str:
    """When a task on another clock lands at different local time times because the two places change
    clocks on different dates: "15:30 until Fri 23 Oct, 14:30 from Mon 26 Oct to Fri 30 Oct,
    15:30 from Mon 02 Nov". "" when the local time never changes. Pure.

    Code says it so the model does not work it out. Live (5 October), confirming the NVDA runs
    on the New York clock, JARVIS said they "shift with it on 1 November, landing an hour
    earlier in your evening": local time changes on 25 October, so they land an hour earlier from
    26 to 30 October and are back at 15:30 after New York changes on 1 November."""
    if repeat == "once":
        return ""
    end = _until_date(until) or (due + datetime.timedelta(days=horizon_days)).date()
    segments: list[list] = []
    for run in _occurrences(due, repeat, end.isoformat(), limit=400):
        paris = run.astimezone(_TZ)
        hm = paris.strftime("%H:%M")
        if segments and segments[-1][0] == hm:
            segments[-1][2] = paris
        else:
            segments.append([hm, paris, paris])
    if len(segments) < 2:
        return ""
    parts = [f"{segments[0][0]} until {segments[0][2]:%a %d %b}"]
    for hm, first, last in segments[1:-1]:
        parts.append(f"{hm} from {first:%a %d %b} to {last:%a %d %b}")
    parts.append(f"{segments[-1][0]} from {segments[-1][1]:%a %d %b}")
    return ", ".join(parts)


def _shift_note(t: dict) -> str:
    shifts = local_shifts(t["due"], t["repeat"], t["until"]) if t["tz"] != _localtime.TIMEZONE else ""
    return f" In local time it runs at {shifts}." if shifts else ""


def _describe_task(t: dict) -> str:
    due = t["due"]
    local = due.astimezone(_TZ)
    when = f"next {local.strftime('%a %Y-%m-%d %H:%M')} local time"
    if t["tz"] != _localtime.TIMEZONE:
        when += f" ({due.strftime('%H:%M')} {t['tz']})"
    rule = {"once": "once", "daily": "every day", "weekdays": "on weekdays",
            "weekly": f"every {due.strftime('%A')}"}[t["repeat"]]
    if t["until"] and t["repeat"] != "once":
        rule += f" until {t['until']}"
    follow = f", following skill {t['skill']}" if t.get("skill") else ""
    return f"{when}, {rule}{follow}: {t['task']}"


def _worked_on_skills(names) -> None:
    """Scheduling or cancelling a routine works on its skill, it does not follow it: no
    "followed" line and no Checks on the reply (skills.worked_on)."""
    names = [n for n in names if n]
    if names:
        from backend.brain import skills as _skills
        for n in names:
            _skills.worked_on(n)


async def schedule_task(action: str, when: str = "", task: str = "", repeat: str = "once",
                        until: str = "", timezone: str = "", skill: str = "") -> str:
    """Registered tool: add / list / update / cancel scheduled tasks. Never raises."""
    try:
        action = (action or "").strip().lower()
        lines = _read_schedule() or list(_SCHEDULE_HEADER)
        tasks = [t for t in (_parse_task_line(l) for l in lines) if t]

        if action == "list":
            if not tasks:
                return "No scheduled tasks."
            return ("Scheduled tasks (jarvis-vault/schedule.md):\n"
                    + "\n".join(f"- {_describe_task(t)}{_shift_note(t)}"
                                for t in sorted(tasks, key=lambda t: t["due"])))

        if action == "cancel":
            needle = (task or "").strip().lower()
            if len(needle) < 3:
                return "Error: give part of the task's text to cancel it."
            keep, gone = [], []
            for line in lines:
                t = _parse_task_line(line)
                if t and (needle in t["task"].lower() or needle == t["skill"]):
                    gone.append(t)
                else:
                    keep.append(line)
            if not gone:
                return f"No scheduled task matching '{task}'."
            _write_schedule(keep)
            _worked_on_skills(t["skill"] for t in gone)
            return (f"Cancelled {len(gone)} scheduled task(s):\n"
                    + "\n".join(f"- {_describe_task(t)}" for t in gone))

        if action == "update":
            # Ties existing tasks to a skill (or takes it off), keeping their times. Replayed
            # live (5 October), "cancel and re-add each one against the skill" went wrong 6
            # times in 18: three times a cancel by "NVDA" removed the open AND the close and only
            # the open came back; once all four came back without the skill, said to be bound to it.
            needle = (task or "").strip().lower()
            if len(needle) < 3:
                return "Error: give part of the task's text (or the skill it follows) to update it."
            new_skill = (skill or "").strip().lower()
            if new_skill:
                from backend.brain import skills as _skills
                found = _skills.load(new_skill)
                if found is None or not found.active:
                    return (f"Error: you have no skill called '{new_skill}' in use, so nothing was "
                            "changed." + _skills._closest(new_skill, _skills.active_skills()))
            out, changed, before = [], [], []
            for line in lines:
                t = _parse_task_line(line)
                if t and (needle in t["task"].lower() or needle == t["skill"]):
                    before.append(t["skill"])
                    t = {**t, "skill": new_skill}
                    line = _format_task_line(t["due"], t["repeat"], t["until"], t["tz"], t["task"], new_skill)
                    changed.append((t, line))
                out.append(line)
            if not changed:
                return f"No scheduled task matching '{task}', so nothing was changed."
            _write_schedule(out)
            on_disk = _read_schedule()
            if not all(line in on_disk for _, line in changed):
                return (_owner.say("Error: the change did not persist -- a read-back of jarvis-vault/schedule.md "
                        "did not find it. Do not tell «owner» it changed."))
            _worked_on_skills([new_skill] + before)
            what = (f"each now follows the skill {new_skill} as it is at each run" if new_skill
                    else "none of them follows a skill now")
            return (f"Confirmed: {len(changed)} scheduled task(s) changed in jarvis-vault/schedule.md, "
                    f"same times; {what}:\n"
                    + "\n".join(f"- {_describe_task(t)}{_shift_note(t)}" for t, _ in changed)
                    + pause.tool_note(pause.ALL))

        if action != "add":
            return f"Error: unknown action '{action}'. Use 'add', 'list', 'update' or 'cancel'."

        task = " ".join((task or "").split())
        if len(task) < _TASK_MIN_CHARS:
            return ("Error: describe the task in a full sentence -- it runs later with no "
                    "conversation context.")
        from backend.brain import background
        if background._NEEDS_SHELL.search(task):
            return "Error: this task asks for a shell, terminal or git, which no worker can run."
        skill = (skill or "").strip().lower()
        if skill:
            from backend.brain import skills as _skills
            found = _skills.load(skill)
            if found is None or not found.active:
                return (f"Error: you have no skill called '{skill}' in use, so nothing was scheduled."
                        + _skills._closest(skill, _skills.active_skills()))
        tz_name = (timezone or "").strip() or _localtime.TIMEZONE
        zone = _zone(tz_name)
        if zone is None:
            return (f"Error: unknown timezone '{timezone}'. Use an IANA name such as "
                    "Europe/Paris or America/New_York.")
        try:
            naive = datetime.datetime.strptime((when or "").strip().replace("T", " "),
                                               "%Y-%m-%d %H:%M")
        except ValueError:
            return ("Error: 'when' must be YYYY-MM-DD HH:MM in local time (compute it from the "
                    "[Now] line in your context).")
        repeat = (repeat or "once").strip().lower()
        if repeat not in _TASK_REPEATS:
            return "Error: repeat must be once, daily, weekdays or weekly."
        # `when` is ALWAYS local time; `timezone` only picks the clock later runs stay on.
        # Live, with `when` read on the task's own clock, the model put the local time open
        # (15:30) on the New York clock 2 times in 12 -- a 21:30 "open" report. It always
        # knew the open is 15:30 local time; the conversion is now done here.
        due = naive.replace(tzinfo=_TZ).astimezone(zone)
        if repeat == "weekdays":
            due = _roll_to_weekday(due)
        now = now_local()
        if due <= now:
            return (_owner.say(f"Error: {naive.strftime('%Y-%m-%d %H:%M')} local time is in the past -- it "
                    f"is {now.strftime('%Y-%m-%d %H:%M')} right now («tz»)."))
        if (due - now).days > _TASK_MAX_DAYS:
            return "Error: the first run must be within a year."
        until = (until or "").strip()
        if until:
            end = _until_date(until)
            if end is None:
                return "Error: 'until' must be a date formatted YYYY-MM-DD (the last day to run)."
            if end < due.date():
                return f"Error: 'until' ({until}) is before the first run ({due.strftime('%Y-%m-%d')})."
            if repeat == "once":
                until = ""
        if any(t["task"].lower() == task.lower() and t["due"] == due for t in tasks):
            return "That task is already scheduled at that time."
        if len(tasks) >= _MAX_PENDING_TASKS:
            return f"Error: {_MAX_PENDING_TASKS} scheduled tasks already pending -- cancel some first."

        new_line = _format_task_line(due, repeat, until, tz_name, task, skill)
        lines.append(new_line)
        if not _write_schedule_verified(lines, new_line):
            return (_owner.say("Error: the task did not persist -- a read-back of jarvis-vault/schedule.md "
                    "right after writing did not find it. Do not tell «owner» it is scheduled."))
        _worked_on_skills([skill])
        runs = _occurrences(due, repeat, until)
        shown = ", ".join(r.astimezone(_TZ).strftime("%a %d %b %H:%M") for r in runs)
        count = (f"{len(runs)} run(s) in all" if repeat == "once" or until and len(runs) < 8
                 else "first runs")
        check = ""
        if tz_name != _localtime.TIMEZONE:
            check = (f" It stays at {due.strftime('%H:%M')} on the {tz_name} clock when clocks "
                     "change; if that is not the local time you meant, cancel it and add it "
                     "again." + _shift_note({"due": due, "repeat": repeat, "until": until, "tz": tz_name})
                     + (" Say the local times from this, don't work them out." if
                        local_shifts(due, repeat, until) else ""))
        return (_owner.say(f"Confirmed written to jarvis-vault/schedule.md: \"{new_line}\". "
                f"{_describe_task(_parse_task_line(new_line))}. {count}, local time, when "
                f"«Owner» gets them: {shown}.{check} At each run a background worker does the "
                "task with live data and «owner» gets its answer on Discord.")
                + (f" Each run follows the skill {skill} as it is at that moment, so improving "
                   "the skill improves every later run." if skill else "")
                + pause.tool_note(pause.ALL))
    except Exception as e:
        log.warning("chronos: schedule_task failed: %s", e)
        return f"Error: scheduling unavailable ({e})"


def _task_brief(t: dict, now: datetime.datetime) -> str:
    local = now.astimezone(t["due"].tzinfo)
    when = now.strftime("%A %Y-%m-%d %H:%M") + " local time"
    if t["tz"] != _localtime.TIMEZONE:
        when += f" ({local.strftime('%H:%M')} {t['tz']})"
    return (
        _owner.say(f"Scheduled task «owner» set up, running now, {when}.\n\n"
        f"Task: {t['task']}\n\n"
        "Do it now with your tools and live data. Your reply is sent to «owner» on Discord "
        "exactly as you write it: give each figure with its source and the time it is from. "
        "If it does not apply right now (a market closed for a holiday, nothing has changed), "
        "say so in one line.")
        + _routine_skill(t)
    )


def _routine_skill(t: dict) -> str:
    """The skill a routine follows, loaded now rather than when it was scheduled."""
    name = t.get("skill") or ""
    if not name:
        return ""
    from backend.brain import skills as _skills
    found = _skills.load(name)
    if found is None or not found.active:
        return (f"\n\nThis routine was set up to follow your skill '{name}', which is no longer "
                "in use: do the task as written, and say so in one line at the end.")
    from backend.brain import background
    missing = [t for t in found.tools if t not in background._ALLOWED_TOOLS]
    gap = (f" It names tools a scheduled run does not have ({', '.join(missing)}): skip those "
           "steps and say which in one line." if missing else "")
    return (f"\n\nFollow your skill '{found.name}' for this; it is below, so there is no need "
            f"to open it.{gap}\n\n{found.body}")


async def _tell(text: str) -> None:
    """DM the owner, or park the text for his next conversation turn."""
    sent = False
    if _notifier is not None:
        try:
            sent = await _notifier(text)
        except Exception as e:
            log.debug("chronos: DM failed: %s", e)
    if not sent:
        def _park(state, _text=text):
            state.setdefault("pending_mentions", []).append(_text)
            state["pending_mentions"] = state["pending_mentions"][-10:]
        _update_state(_park)


async def _start_task_run(t: dict, now: datetime.datetime, occurrence: str = "") -> bool:
    from backend.brain import background
    label = f"scheduled {t['due'].astimezone(_TZ).strftime('%a %H:%M')}: {t['task']}"
    from backend.brain import skills as _skills
    follows = t.get("skill") if t.get("skill") and (s := _skills.load(t["skill"])) and s.active else ""
    error = await background.start_scheduled(label[:300], _task_brief(t, now), skill=follows,
                                             occurrence=occurrence)
    if error:
        log.warning("chronos: scheduled task not started (%s): %s", error[:80], t["task"][:80])
    elif t.get("skill"):
        from backend.brain import skills as _skills
        from backend.storage import skills as _skill_uses
        if (found := _skills.load(t["skill"])) is not None and found.active:
            _skill_uses.record_use(found.name, "routine", channel="discord")
    return not error


def _skip_for_pause(t: dict) -> None:
    """A scheduled run whose moment passed during a pause. It is not delayed: a price "at the
    open" delivered an hour late is wrong. It is listed in the message on /resume."""
    pause.record_skip("scheduled_task",
                      f"{t['task'][:80]} ({t['due'].astimezone(_TZ).strftime('%a %H:%M')})")


def _occurrence_key(t: dict) -> str:
    return f"{t['due'].isoformat()}|{t['tz']}|{t['task']}"


def _due_record(t: dict, last: bool) -> dict:
    return {"key": _occurrence_key(t), "due": t["due"].isoformat(), "tz": t["tz"],
            "task": t["task"], "skill": t.get("skill", ""), "last": last, "attempts": 0}


def _task_from_record(r: dict) -> Optional[dict]:
    zone = _zone(r.get("tz", ""))
    try:
        due = datetime.datetime.fromisoformat(r["due"])
    except (KeyError, TypeError, ValueError):
        return None
    if zone is None or due.tzinfo is None or not r.get("task"):
        return None
    return {"due": due.astimezone(zone), "tz": r["tz"], "task": r["task"],
            "skill": r.get("skill", "")}


def _record_due_runs(records: list[dict]) -> None:
    def mutate(state):
        pending = state.setdefault("due_task_runs", [])
        have = {r.get("key") for r in pending}
        pending.extend(r for r in records if r["key"] not in have)
    _update_state(mutate)


def _forget_due_run(key: str) -> None:
    def mutate(state):
        state["due_task_runs"] = [r for r in state.get("due_task_runs", []) if r.get("key") != key]
    _update_state(mutate)


def _note_attempt(key: str) -> None:
    def mutate(state):
        for r in state.get("due_task_runs", []):
            if r.get("key") == key:
                r["attempts"] = int(r.get("attempts", 0)) + 1
    _update_state(mutate)


def _trail(text: str, now: datetime.datetime) -> None:
    """One [done] line, written when its outcome is known. No await in between."""
    _write_schedule(_append_done(_read_schedule(), text, now))


async def _fire_due_tasks(now: datetime.datetime) -> None:
    lines = _read_schedule()
    due = [t for t in (_parse_task_line(l) for l in lines) if t and t["due"] <= now]
    paused = pause.is_paused("all")   # /pause holds my own work, not what the owner scheduled

    if due:
        # A04 slice 4: advance the schedule BEFORE anything awaits. Nothing between the
        # read above and this write awaits, so no concurrent schedule_task call can be lost.
        out, trail, records, skipped, missed = [], [], [], [], []
        for line in lines:
            t = _parse_task_line(line)
            if t is None or t["due"] > now:
                out.append(line)
                continue
            nxt = next_task_occurrence(t["due"], t["repeat"], now)
            end = _until_date(t["until"])
            again = nxt is not None and not (end and nxt.date() > end)
            if again:
                out.append(_format_task_line(nxt, t["repeat"], t["until"], t["tz"], t["task"],
                                             t["skill"]))
            suffix = "" if again else " (last run)"
            if paused:
                trail.append("task skipped (paused): " + t["task"][:80] + suffix)
                skipped.append(t)
            elif (now - t["due"]).total_seconds() > _TASK_GRACE_S:
                trail.append("task missed (backend was down): " + t["task"][:80] + suffix)
                missed.append(t)
            else:
                records.append(_due_record(t, last=not again))
        # N1: the runs that came due are on record before the schedule moves past them.
        if records:
            _record_due_runs(records)
        for text in trail:
            out = _append_done(out, text, now)
        _write_schedule(out)
        for t in skipped:
            _skip_for_pause(t)
        for t in missed:
            log.warning("chronos: scheduled task missed by %.0f min: %s",
                        (now - t["due"]).total_seconds() / 60, t["task"][:80])
            await _tell(f"I missed a scheduled check at {t['due'].astimezone(_TZ).strftime('%a %H:%M')} "
                        f"local time (the backend wasn't running then): {t['task']}")

    await _run_due_records(now, paused)


async def _run_due_records(now: datetime.datetime, paused: bool) -> None:
    """Start each recorded due run, or settle it: skipped by a pause, already admitted
    before a crash, or out of its grace. Also what the first cycle after a restart does."""
    from backend.storage import jobs as _jobs
    for r in list(_load_state().get("due_task_runs", []) or []):
        key = r.get("key", "")
        t = _task_from_record(r)
        if t is None:
            _forget_due_run(key)
            continue
        suffix = " (last run)" if r.get("last") else ""
        when = t["due"].astimezone(_TZ).strftime("%a %H:%M")
        if paused:
            _forget_due_run(key)
            _skip_for_pause(t)
            _trail("task skipped (paused): " + t["task"][:80] + suffix, now)
        elif _jobs.has_occurrence(key):
            # Queued before a crash that came before it was forgotten: it is running or ran.
            _forget_due_run(key)
            _trail("task run: " + t["task"][:80] + suffix, now)
        elif (now - t["due"]).total_seconds() > _TASK_GRACE_S:
            _forget_due_run(key)
            why = ("every background slot stayed busy" if int(r.get("attempts", 0)) > 0
                   else "the backend restarted before it could start")
            _trail("task not run (" + why + "): " + t["task"][:80] + suffix, now)
            await _tell(f"I couldn't run the scheduled check at {when} local time: {why}. "
                        f"The task: {t['task']}")
        elif await _start_task_run(t, now, occurrence=key):
            _forget_due_run(key)
            _trail("task run: " + t["task"][:80] + suffix, now)
        else:
            _note_attempt(key)


# ---------------------------------------------------------------------------
# Firing — reminders, night tasks, the brief
# ---------------------------------------------------------------------------

def pop_pending_mentions() -> str:
    """Reminders that came due but couldn't be DMed (Discord down) — injected
    into the next conversation turn instead, exactly once. Same pattern as
    background.pop_completed_notices / research.pop_deferred_mentions."""
    state = _load_state()
    pending = state.get("pending_mentions", [])
    if not pending:
        return ""
    state["pending_mentions"] = []
    _save_state(state)
    lines = "\n".join(f"- {m}" for m in pending)
    return (_owner.say("[Reminders that came due but couldn't be delivered by Discord — "
            "tell «owner» about them now]\n") + lines)


async def _fire_due_reminders(now: datetime.datetime) -> None:
    lines = _read_schedule()
    if not lines:
        return
    changed = False
    out: list[str] = []
    fired: list[tuple[str, str]] = []   # (message, repeat)

    for line in lines:
        r = _parse_reminder_line(line)
        if r is None or r["due"] > now:
            out.append(line)
            continue
        changed = True
        fired.append((r["message"], r["repeat"]))
        if r["repeat"] == "daily":
            nxt = next_daily_occurrence(r["due"], now)
            out.append(_format_reminder_line(nxt, "daily", r["message"]))

    if not changed:
        return

    # A04 slice 4: the schedule mutation must complete BEFORE the DMs, not after.
    # This used to write `out` after awaiting each notifier -- a read taken at the top
    # of the function, written back minutes later, silently destroying any reminder
    # scheduled in between (reproduced live: schedule_reminder had already reported it
    # saved and verified). Nothing between the read above and this write awaits, so the
    # event loop cannot interleave another writer into the window.
    for message, repeat in fired:
        out = _append_done(
            out, f"reminder fired: {message}" + (" (daily)" if repeat == "daily" else ""), now)
    _write_schedule(out)

    # Delivery happens after the file is consistent. A failed DM parks the reminder for
    # the next conversation turn rather than losing it -- and parking now goes through
    # update_state, since pop_pending_mentions() writes the same file from every turn.
    for message, repeat in fired:
        text = f"Reminder: {message}"
        sent = False
        if _notifier is not None:
            try:
                with _notices.labelled("reminder"):
                    sent = await _notifier(text)
            except Exception as e:
                log.debug("chronos: reminder DM failed: %s", e)
        if not sent:
            def _park(state, _text=text):
                state.setdefault("pending_mentions", []).append(_text)
                state["pending_mentions"] = state["pending_mentions"][-10:]
            _update_state(_park)
        log.info("chronos: reminder fired (%s): %s", "DM" if sent else "parked for conversation", message)


async def _run_night_task(now: datetime.datetime) -> None:
    """Pop and execute ONE queued night task that is actually due (one per
    cycle keeps each loop iteration bounded; the window is hours long).

    Scans in file order but skips any task whose `after` gate date hasn't
    arrived yet, so a task queued for a future event (e.g. "after=2026-09-16")
    stays queued through earlier quiet windows instead of firing early and
    reporting on something that hasn't happened."""
    lines = _read_schedule()
    task = None
    idx = None
    for i, line in enumerate(lines):
        n = _parse_night_line(line)
        if n and night_task_due(n["after"], now):
            task = n["task"]
            idx = i
            break
    if task is None:
        return
    from backend.brain import background as _bg
    if not _bg.slots_free():
        # G1a / D7: a night task counts against the three background slots. The quiet window
        # is hours long, so it simply runs on a later cycle.
        log.info("chronos: night task waiting for a free background slot")
        return
    original = lines[idx]
    # G0 / D7: the task's full text is kept in state while it runs, so a crash mid-run is
    # reported with it at the next startup (recover_night_task) instead of vanishing.
    _update_state(lambda state: state.__setitem__(
        "night_running", {"task": task, "line": original, "started": now.isoformat()}))
    del lines[idx]
    _write_schedule(lines)

    log.info("chronos: night task starting — %s", task[:100])
    try:
        from backend.brain import background
        with background.occupy("night"):
            summary = await background.execute_task(
                _owner.say(f"{task}\n\n(This is an overnight task «owner» queued earlier; «he» will read "
                f"your findings in «his» morning brief. Be thorough — you have time.)")
            )
    except pause.Paused:
        # /pause stopped it between steps. It was popped before it ran, so put it back: it
        # runs in the next quiet window after /resume, and the resume message lists it.
        _write_schedule(_read_schedule() + [original])
        _update_state(lambda state: state.pop("night_running", None))
        pause.record_skip("night_task_stopped", task[:80])
        log.info("chronos: night task stopped by /pause and kept — %s", task[:80])
        return
    except Exception as e:
        # G0 / D7: a failure is recorded as one, for the brief -- no drafts/ note (it used to
        # be written up there and trailed "done"), and not retried.
        log.warning("chronos: night task failed: %s", e)
        failed = f"Failed: {e}"
        partial = getattr(getattr(e, "result", None), "text", "") or ""
        if partial.strip():
            failed += "\nWhat it had written: " + partial.strip()

        def _record_failure(state):
            state.pop("night_running", None)
            state.setdefault("night_results", []).append({
                "date": now.strftime("%Y-%m-%d"), "task": task, "status": "failed",
                "summary": failed[:1500], "file": "",
            })
            state["night_results"] = state["night_results"][-6:]
        _update_state(_record_failure)
        _trail(f"night task failed: {task[:80]}", now)
        return

    summary = (summary or "").strip() or "Finished, but produced no summary."
    filename = ""
    try:
        from backend.tools.vault import vault_write
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:50] or "task"
        filename = f"night-{now.strftime('%Y-%m-%d')}-{slug}.md"
        await vault_write(filename, f"# Night task — {now.strftime('%Y-%m-%d')}\n\nTask: {task}\n\n{summary}\n", "drafts")
    except Exception as e:
        log.debug("chronos: night result note failed: %s", e)

    def _record_result(state):
        state.pop("night_running", None)
        state.setdefault("night_results", []).append({
            "date": now.strftime("%Y-%m-%d"), "task": task, "status": "done",
            "summary": summary[:1500], "file": filename,
        })
        state["night_results"] = state["night_results"][-6:]

    _update_state(_record_result)

    _trail(f"night task done: {task[:80]}", now)
    log.info("chronos: night task done — %s", task[:80])


def recover_night_task(now: Optional[datetime.datetime] = None) -> bool:
    """G0 / D7, called once at startup before the loops start: a night task the previous
    process was running when it died. Reported in the next brief with its full text and
    not run again -- what it did before the crash may already have landed. Returns True
    when one was reported. A task whose line is still in schedule.md (the crash came
    before it was popped) was never started: nothing to report, it runs as queued."""
    running = _load_state().get("night_running")
    if not running:
        return False
    now = now or now_local()
    still_queued = running.get("line") in _read_schedule()
    task = running.get("task") or "an unnamed night task"

    def mutate(state):
        state.pop("night_running", None)
        if not still_queued:
            state.setdefault("night_results", []).append({
                "date": now.strftime("%Y-%m-%d"), "task": task, "status": "interrupted",
                "summary": ("Interrupted: the backend restarted while this was running. It "
                            "was not run again; what it did before then may have landed."),
                "file": "",
            })
            state["night_results"] = state["night_results"][-6:]
    _update_state(mutate)
    if still_queued:
        return False
    _trail(f"night task interrupted (restart): {task[:80]}", now)
    log.warning("chronos: a night task was cut off by a restart -- %s", task[:80])
    return True


# ---------------------------------------------------------------------------
# Morning brief
# ---------------------------------------------------------------------------

_BRIEF_PROMPT = _owner.say("""\
You are JARVIS. It is {now} («tz»). Compose your MORNING BRIEF for «owner» — a single
Discord message under 1500 characters, plain prose in your own voice: direct, warm, dry
wit allowed, no markdown headers, line breaks between topics are fine.

INPUTS (skip any empty section silently — never say "no calendar events", just omit it):

Overnight task findings («owner» explicitly asked for these — lead with them):
{night_results}

Autonomous research passes since yesterday:
{research_trail}

Open intents from the queue:
{intents}

Today's reminders:
{reminders}

Today's calendar:
{calendar}

Today's planned blocks («his» week plan; they are in «his» calendar already, mention them as the shape of the day):
{plan}

Turns yesterday that cost more than usual (mention briefly, one line, only if present):
{expensive}

Rules: lead with what matters most. Summarize, don't enumerate — this is a briefing from
someone who was up all night, not a log dump. If genuinely nothing happened, say so in
one honest line. Output ONLY the message text, nothing else.
""")


def _collect_research_trail(now: datetime.datetime) -> str:
    """[done] lines from research-queue.md dated today or yesterday."""
    try:
        content = (_PROJECT_ROOT / "jarvis-vault" / "research-queue.md").read_text(encoding="utf-8")
    except Exception:
        return ""
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    keep = [l.strip() for l in content.splitlines()
            if l.strip().startswith("[done]") and (today in l or yesterday in l)]
    return "\n".join(keep[-10:])


def _collect_open_intents() -> str:
    try:
        content = (_PROJECT_ROOT / "jarvis-vault" / "queue.md").read_text(encoding="utf-8")
    except Exception:
        return ""
    keep = [l.strip() for l in content.splitlines()
            if l.strip() and not l.strip().startswith("#") and "resolved" not in l.lower()]
    return "\n".join(keep[:8])


def _collect_todays_reminders(now: datetime.datetime) -> str:
    today = now.strftime("%Y-%m-%d")
    out = []
    for line in _read_schedule():
        r = _parse_reminder_line(line)
        if r and r["due"].strftime("%Y-%m-%d") == today:
            out.append(f"- {r['due'].strftime('%H:%M')}: {r['message']}")
        t = _parse_task_line(line)
        if t and t["due"].astimezone(_TZ).strftime("%Y-%m-%d") == today:
            out.append(f"- {t['due'].astimezone(_TZ).strftime('%H:%M')} (scheduled check): {t['task']}")
    return "\n".join(out)


_CALENDAR_BRIEF_CHARS = 3000
_EVENT_ID_RE = re.compile(r"[ \t]*\[id [^\]]*\]")


def _tidy_calendar(text: str) -> str:
    """The listing as the brief should read it: event ids are for the tools, not for a
    morning message, and a long day is cut between events with a count, never mid-line.
    (It used to slice at 800 characters, which lost the evening of a busy day.)"""
    text = _EVENT_ID_RE.sub("", text)
    if len(text) <= _CALENDAR_BRIEF_CHARS:
        return text
    kept = text[:_CALENDAR_BRIEF_CHARS].rsplit("\n", 1)[0]
    left = text[len(kept):].count("\n")
    return f"{kept}\n(+{left} more lines not shown)"


async def _collect_calendar() -> str:
    try:
        # Never trigger the interactive OAuth flow from an autonomous loop —
        # calendar_action with no valid token opens a browser and blocks its
        # executor thread waiting for consent. Only touch the calendar when a
        # token already exists (from a past conversational use).
        from backend.tools.calendar import TOKEN_PATH, calendar_action
        from backend.tools import outcomes
        if not TOKEN_PATH.exists():
            return ""
        result = await asyncio.wait_for(calendar_action(action="list_events", date="today"), timeout=15.0)
        text = str(result).strip()
        # The tool's own failures ("Calendar error: ...", "Calendar auth failed: ...") do
        # not start with "error"; they used to be composed into the brief as the agenda.
        if outcomes.is_failure("calendar_action", text) or "not configured" in text.lower():
            return ""
        return _tidy_calendar(text)
    except Exception as e:
        log.debug("chronos: calendar fetch skipped: %s", e)
        return ""


async def compose_brief(now: Optional[datetime.datetime] = None,
                        night_results: Optional[list[dict]] = None) -> str:
    """Build the morning brief text. Consumes stored night results. Falls back
    to a plain formatted digest when the LLM call fails — a degraded brief
    still beats a silent morning."""
    now = now or now_local()
    # A06: READ the night results, do not consume them here. This used to clear them
    # before composing -- the "pop-before-work queue" the roadmap names as a root
    # cause. `last_brief_date` is only set after a SUCCESSFUL delivery, so a failed DM
    # retried later, by which point the findings were already gone: the retry composed
    # a brief with nothing from the night's work. Reproduced before this change.
    # Consumption now happens in `_mark_briefed`, at the same moment the brief is
    # recorded as delivered.
    if night_results is None:
        night_results = _load_state().get("night_results", [])

    nights_text = "\n\n".join(
        f"Task: {r['task']}\nFindings: {r['summary']}" for r in night_results
    )
    research_trail = _collect_research_trail(now)
    intents = _collect_open_intents()
    reminders = _collect_todays_reminders(now)
    calendar = await _collect_calendar()
    plan_today = _collect_plan_today(now)
    expensive = _collect_expensive_turns()
    from backend.brain import portfolio
    portfolio_line = portfolio.brief_line()
    skills_line = _collect_skills_overnight(now)
    goals_line = _collect_goals_line()
    reading_line = _collect_reading_line(now)
    try:
        from backend.brain import research
        digest_line = research.digest_line(now.timestamp())
    except Exception:
        digest_line = ""
    tail = "\n".join(l for l in (portfolio_line, skills_line, goals_line, reading_line, digest_line) if l)

    if not any([nights_text, research_trail, intents, reminders, calendar, plan_today, expensive, tail]):
        return f"Morning. Quiet night — nothing new overnight, nothing on the schedule for today. ({now.strftime('%A %H:%M')})"

    prompt = _BRIEF_PROMPT.format(
        now=now.strftime("%A %Y-%m-%d %H:%M"),
        night_results=nights_text or "(none)",
        research_trail=research_trail or "(none)",
        intents=intents or "(none)",
        reminders=reminders or "(none)",
        calendar=calendar or "(none)",
        plan=plan_today or "(none)",
        expensive=expensive or "(none)",
    )
    # The number stays code-owned even when the brief's prose is generated by a model.
    try:
        from backend.brain.research import _llm
        from backend.storage.llm_calls import labelled
        with labelled("brief"):
            text = (await _llm(prompt, max_tokens=_BRIEF_MAX_TOKENS)).strip()
        if text:
            return text + ("\n" + tail if tail else "")
    except Exception as e:
        log.warning("chronos: brief LLM call failed: %s", e)

    # Fallback: plain digest, no LLM
    parts = [f"Morning brief — {now.strftime('%A %Y-%m-%d')}"]
    if nights_text:     parts.append("Overnight findings:\n" + nights_text)
    if research_trail:  parts.append("Research passes:\n" + research_trail)
    if reminders:       parts.append("Today's reminders:\n" + reminders)
    if intents:         parts.append("Open intents:\n" + intents)
    if calendar:        parts.append("Calendar:\n" + calendar)
    if plan_today:      parts.append("Planned blocks today:\n" + plan_today)
    if expensive:       parts.append("Expensive turns:\n" + expensive)
    if tail:            parts.append(tail)
    return "\n\n".join(parts)


def _collect_reading_line(now: datetime.datetime) -> str:
    """Once a month: one code-written line naming the research topics that stopped
    because the owner has not opened them. "" when none, already told this month, or the
    research state can't be read (the brief never fails because of this)."""
    try:
        from backend.brain import research
        topics = research._load_state().get("stopped_topics") or []
        if not topics:
            return ""
        stop_days = getattr(research, "_UNREAD_STOP_DAYS", 30)
        told = _load_state().get("reading_report_date", "")
        if told[:7] == now.strftime("%Y-%m"):
            return ""

        def day(epoch):
            d = datetime.datetime.fromtimestamp(epoch, _TZ)
            return f"{d.day} {d.strftime('%b')}"

        new = [t for t in topics if not told or
               (datetime.datetime.fromtimestamp(t["since"], _TZ)
                + datetime.timedelta(days=stop_days)).strftime("%Y-%m-%d") > told]
        if not new:
            return ""
        names = [f"{t['title']} (you haven't opened it since {day(t['last_opened'])})"
                 if t.get("last_opened") else f"{t['title']} (not opened since {day(t['since'])})"
                 for t in new[:5]]
        more = f"; and {len(new) - 5} more" if len(new) > 5 else ""
        return ("Research I stopped: " + "; ".join(names) + more
                + ". Open the note or ask me and it comes back.")
    except Exception:
        return ""


def _collect_goals_line() -> str:
    """G1a: one code-written line naming the goals at work (titles and states only).
    Empty while goals are off or there are none."""
    try:
        from backend.brain import goal_runner
        from backend.storage import goals as store
        if not goal_runner.configured():
            return ""
        live = store.list_goals((store.ACTIVE, store.WAITING, store.PAUSED), limit=10)
        # G3: questions a goal is waiting on him to answer.
        asked = [w for w in store.waits() if w["kind"] == "question"][:3]
        parts = []
        if live:
            parts.append("Goals at work: " + "; ".join(f"{g['title'][:60]} ({g['state']})" for g in live))
        if asked:
            parts.append("Waiting on your answer: " + "; ".join(
                f"{w['payload'].get('question', '')[:120]} (goal {w['goal_id']})" for w in asked))
        return ". ".join(parts)
    except Exception:
        return ""


def _collect_skills_overnight(now: datetime.datetime) -> str:
    """What the skills' overnight pass did (skill_nightly), code-written, or ""."""
    try:
        from backend.brain import skill_nightly
        return skill_nightly.brief_line(now)
    except Exception as e:
        log.debug("chronos: skills line skipped: %s", e)
        return ""


async def _offer_skill_routines() -> None:
    """After the brief: a routine the overnight pass proposes is put to him then, not at 3am."""
    try:
        from backend.brain import skill_nightly
        await skill_nightly.offer_pending()
    except Exception as e:
        log.debug("chronos: routine offers skipped: %s", e)


def _collect_plan_today(now: datetime.datetime) -> str:
    """P4: today's blocks from the week plan, one per line ("" when there is no plan today)."""
    try:
        from backend.brain import planday
        return planday.todays_blocks_text(now)
    except Exception as e:
        log.debug("chronos: plan blocks skipped: %s", e)
        return ""


def _collect_expensive_turns() -> str:
    """H1: turns over llm_calls.EXPENSIVE_TURN_USD in the last day."""
    try:
        from backend.storage import llm_calls
        return llm_calls.expensive_turns_text()
    except Exception as e:
        log.debug("chronos: expensive turns skipped: %s", e)
        return ""


def _mark_briefed(now: datetime.datetime,
                  reported_results: Optional[list[dict]] = None,
                  reading_reported: bool = False) -> None:
    """Record delivery and consume only the night results included in that brief.

    A06: these two facts belong together. Marking without consuming would repeat the
    same findings tomorrow; consuming without marking (the old behaviour, which cleared
    them up front) loses them whenever delivery fails.

    Applied as an atomic delta. A new night result can arrive during composition or
    delivery; it must remain for the next brief. `None` retains the old direct-call
    behavior for callers that deliberately mark the full current queue as covered.
    """
    covered = None if reported_results is None else Counter(
        json.dumps(item, sort_keys=True, ensure_ascii=False) for item in reported_results)

    def _apply(state):
        state["last_brief_date"] = now.strftime("%Y-%m-%d")
        if reading_reported:
            state["reading_report_date"] = now.strftime("%Y-%m-%d")
        if covered is None:
            state["night_results"] = []
            return
        remaining = []
        for item in state.get("night_results", []):
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if covered[key]:
                covered[key] -= 1
            else:
                remaining.append(item)
        state["night_results"] = remaining
    _update_state(_apply)


async def _send_brief(now: datetime.datetime) -> bool:
    covered = [dict(item) for item in _load_state().get("night_results", [])]
    text = await compose_brief(now, night_results=covered)
    sent = False
    if _notifier is not None:
        try:
            with _notices.labelled("brief"):
                sent = await _notifier(text)
        except Exception as e:
            log.debug("chronos: brief DM failed: %s", e)
    if sent:
        reading = _collect_reading_line(now)
        _mark_briefed(now, covered, reading_reported=bool(reading) and reading in text)
        log.info("chronos: morning brief sent")
        await _offer_skill_routines()
    else:
        log.warning("chronos: morning brief not delivered — night results kept for "
                    "the next attempt")
    return sent


async def brief_now(deliver: Optional[Callable[[str], Awaitable[bool]]] = None) -> str:
    """Compose on demand; acknowledge only after Discord confirms every chunk."""
    now = now_local()
    covered = [dict(item) for item in _load_state().get("night_results", [])]
    text = await compose_brief(now, night_results=covered)
    if deliver is not None and await deliver(text):
        _mark_briefed(now, covered)
    return text


def handle_brief_command(arg: str) -> str:
    """Discord '/brief [HH:MM|on|off]' — sync config part. '/brief now' is
    handled by the async path in discord_bridge (it composes + sends)."""
    arg = (arg or "").strip().lower()
    state = _load_state()
    enabled, brief_time = get_brief_config()
    if not arg:
        status = f"every day at {brief_time}" if enabled else "OFF"
        nights = sum(1 for l in _read_schedule() if l.strip().startswith("[night]"))
        extra = f" {nights} night task(s) queued for tonight." if nights else ""
        return f"Morning brief: {status}.{extra} Commands: /brief HH:MM, /brief on|off, /brief now."
    if arg == "off":
        state["brief_enabled"] = False
        _save_state(state)
        return "Morning brief off."
    if arg == "on":
        state["brief_enabled"] = True
        _save_state(state)
        return f"Morning brief on — every day at {brief_time}."
    if re.fullmatch(r"\d{1,2}:\d{2}", arg):
        hh, mm = arg.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            return f"'{arg}' isn't a valid time."
        state["brief_time"] = f"{int(hh):02d}:{mm}"
        state["brief_enabled"] = True
        _save_state(state)
        return f"Morning brief set to {int(hh):02d}:{mm} local time, daily."
    return f"'{arg}' isn't something I understand. Use /brief HH:MM, /brief on|off, or /brief now."


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

async def _cycle() -> None:
    now = now_local()
    # P2: a plain /pause holds my own work (night tasks) but reminders, the brief and the tasks
    # the owner scheduled still run -- they asked for those at a time. /pause all holds them too; they then
    # arrive late on resume (an overdue reminder fires late rather than never, and the brief
    # goes out at-or-after its time), which is why nothing is dropped here.
    hold_all = pause.is_paused("all")
    hold_work = pause.is_paused("work")
    if not hold_all:
        await _fire_due_reminders(now)
    await _fire_due_tasks(now)               # only /pause all skips a run (recorded, not delayed)

    busy = _busy_check() if _busy_check is not None else False
    if not busy and not hold_work and in_night_window(now):
        await _run_night_task(now)

    enabled, brief_time = get_brief_config()
    if (not busy and not hold_all
            and brief_due(enabled, brief_time, _load_state().get("last_brief_date", ""), now)):
        await _send_brief(now)


async def run_loop() -> None:
    """Started as a background task from main.py's lifespan. Every cycle:
    fire due reminders, run one night task if in the window, send the brief
    if due. Reminders fire even while the pipeline is busy (a DM doesn't
    contend with conversation); night tasks and the brief LLM call wait for
    an idle pipeline."""
    log.info("chronos: brief %s, tz=%s",
             "at " + get_brief_config()[1] if get_brief_config()[0] else "off", _TZ)
    await pacing.run_periodic(
        name="chronos", check_interval_s=_CHECK_INTERVAL_S, on_cycle=_cycle,
    )

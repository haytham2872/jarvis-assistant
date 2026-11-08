"""P2: /pause and /resume -- the off switch for everything JARVIS does on his own.

Dots (OpenAI, 29 September 2026) let you pause a dot when you do not want it to continue.
JARVIS works on his own in many places -- research, rumination, watches, forecast checks,
scheduled and night tasks, background jobs -- and until now the only way to stop any of it was
to stop the server. This is the switch.

**What a pause holds** (`work`, the default):
  * the paced loops: research, rumination, watches, forecast checks;
  * night tasks (kept, and run in the next quiet window);
  * new background jobs (`run_in_background`, `web_research`, `continue_in_background`,
    profile research) are refused; running ones stop after their current step.

**What keeps going**: the tasks the owner scheduled (asked for at a time) --
only `/pause all` skips a run whose moment passes, and lists it on resume: a price "at the
open" delivered an hour late is wrong, so it is not delayed; backups, the memory canary, consolidation, indexing, the self-model, the
evolution log, approval expiry -- housekeeping that protects data and never reaches anyone --
and **reminders and the morning brief**, for the same reason. `/pause all`
holds them too: a reminder then arrives late on resume rather than never.

A pause is one row (`storage/pauses.py`); what it cost is listed on `/resume`. `is_paused()` is
what every gate calls: cheap, synchronous, and false when storage is unavailable, so a broken
database can never leave JARVIS silently switched off.
"""
from __future__ import annotations
from backend import localtime as _localtime
from backend import owner as _owner

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from backend.storage import pauses

log = logging.getLogger(__name__)

WORK = pauses.WORK
ALL = pauses.ALL
_TZ = _localtime.ZONE
_MAX_HOURS = 24 * 14

_notifier: Callable[[str], Awaitable[bool]] | None = None


def set_notifier(fn) -> None:
    """async fn(text) -> bool: how the end of a timed pause is announced."""
    global _notifier
    _notifier = fn


class Paused(Exception):
    """A job noticed /pause between steps and stopped (see background._stop_if_paused)."""


def is_paused(kind: str = WORK, *, now: float | None = None) -> bool:
    """Is `kind` of work on hold? `work`: any pause. `all`: only a pause that also holds
    reminders and the brief. Never raises."""
    try:
        row = pauses.active(now=now)
    except Exception:
        return False
    if row is None:
        return False
    return True if kind == WORK else row["scope"] == ALL


def tool_note(kind: str = WORK) -> str:
    """A sentence for the end of a scheduling tool's result when the thing just set up will
    not run because the owner has paused me (empty otherwise), so JARVIS never confirms "I'll
    check hourly" while the check is on hold. `kind="all"`: for reminders and the
    scheduled tasks the owner set, which only a `/pause all` holds."""
    if not is_paused(kind):
        return ""
    what = "everything I do on my own" if kind == ALL else "my own work"
    return (_owner.say(f" Note: «Owner» has paused {what} (/pause), so this waits until «he» sends "
            "/resume or the pause ends. Tell «him» that."))


def record_skip(kind: str, label: str) -> None:
    """Note something that did not happen because of the pause, for the resume message."""
    try:
        row = pauses.active()
        if row:
            pauses.add_skip(row["id"], kind, label)
    except Exception as exc:
        log.debug("pause: skip not recorded (%s)", exc)


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%a %H:%M")


def _duration(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


_ON_HOLD = {
    WORK: ("research, rumination, watches and forecast checks, night tasks and new "
           "background jobs are on hold, and anything running stops after its current step. "
           "Reminders, the morning brief and scheduled tasks keep running; "
           "only /pause all holds those."),
    ALL: ("everything I do on my own is on hold, including reminders and the morning brief. "
          "Reminders and the brief arrive late when you resume; scheduled tasks whose time "
          "passes are skipped."),
}


def describe(row: dict, *, now: float | None = None) -> str:
    """One line for /status and /activity."""
    ts = time.time() if now is None else now
    until = f", until {_clock(row['until_at'])}" if row.get("until_at") else ", until you /resume"
    scope = "everything" if row["scope"] == ALL else "work only (reminders, the brief and your scheduled tasks still run)"
    why = f" -- {row['reason']}" if row.get("reason") else ""
    return f"PAUSED since {_clock(row['started_at'])}{until}; {scope}{why}."


def status_line(*, now: float | None = None) -> str:
    """"" when not paused."""
    try:
        row = pauses.active(now=now)
    except Exception:
        return ""
    return describe(row, now=now) if row else ""


def prompt_line(*, now: float | None = None) -> str:
    """What JARVIS is told on EVERY turn while he is paused ("" otherwise).

    Live (2026-09-30, P2): during a pause, "are you still doing your own research and
    checks?" was answered "same as always" in 2 of 5 trials, because the pause only reached him
    through tools he had not called. A state that changes what he can honestly say about
    himself has to be in front of him, not behind a lookup."""
    try:
        row = pauses.active(now=now)
    except Exception:
        return ""
    if not row:
        return ""
    return (f"{describe(row, now=now)} {_ON_HOLD[row['scope']]} Never say or imply that I am "
            "researching, watching or checking on my own while this stands, and when I set "
            "up my own work, say that it waits. Scheduled tasks keep running "
            "unless this is /pause all.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_HOURS_RE = re.compile(r"^(-?\d+(?:[.,]\d+)?)h?$", re.IGNORECASE)   # "-2" is refused, not a reason


def handle_pause_command(arg: str) -> str:
    """`/pause`, `/pause 3`, `/pause all`, `/pause 3 all travelling`. Sync, like /pace."""
    scope, hours, reason = WORK, None, []
    for tok in (arg or "").split():
        low = tok.lower()
        m = _HOURS_RE.match(low)
        if low == ALL:
            scope = ALL
        elif low == WORK:
            scope = WORK
        elif m:
            hours = float(m.group(1).replace(",", "."))
        else:
            reason.append(tok)
    if hours is not None and not (0 < hours <= _MAX_HOURS):
        return f"Use /pause with a number of hours between 0 and {_MAX_HOURS}, or no number to pause until /resume."

    already = pauses.active()
    until = time.time() + hours * 3600 if hours is not None else None
    pause_id = pauses.start(scope, until, " ".join(reason))
    if pause_id is None:
        return "I couldn't record the pause (storage unavailable), so nothing is paused."
    try:
        from backend.brain import background
        stopped = background.pause_running()
    except Exception as exc:
        log.debug("pause: could not stop running jobs (%s)", exc)
        stopped = 0
    log.info("pause: %s pause started%s", scope, f" for {hours}h" if hours else "")
    verb = "Changed the pause:" if already else "Paused"
    when = f"until {_clock(until)} local time" if until else "until you /resume"
    body = _ON_HOLD[scope]
    tail = f" {stopped} running research job(s) were cancelled." if stopped else ""
    return f"{verb} {when}. {body[0].upper() + body[1:]}{tail} Send /resume to undo."


def resume_text(row: dict, skipped: list[dict], *, expired: bool = False) -> str:
    """What the end of a pause says: how long it lasted and what it cost."""
    ended = row.get("ended_at") or time.time()
    lasted = _duration(ended - row["started_at"])
    scope = "everything" if row["scope"] == ALL else "work only"
    head = (f"The pause ended ({lasted}, {scope})." if expired
            else f"Resumed. I was paused for {lasted} ({scope}).")
    if not skipped:
        return head + " Nothing was missed; research, watches and forecast checks pick up again now."
    stopped = ("\nStopped jobs are not restarted on their own; ask me again if you still want them."
               if any(s["kind"] == "job_stopped" for s in skipped) else "")
    labels = {"scheduled_task": "skipped scheduled check", "job_stopped": "stopped job",
              "night_task_stopped": "night task stopped (kept for the next quiet window)"}
    lines = [f"- {labels.get(s['kind'], s['kind'])}: {s['label']}" for s in skipped]
    return (head + " While paused:\n" + "\n".join(lines[:15])
            + (f"\n(and {len(lines) - 15} more)" if len(lines) > 15 else "")
            + stopped + "\nEverything else picks up again now.")


def _stretch_text(row: dict, *, expired: bool = False) -> str:
    """The message for a pause that just ended, counting everything since he first switched me
    off: a `/pause` he then widened to `/pause all` is one stretch, not two."""
    stretch = pauses.chain(row["id"])
    started = stretch[0]["started_at"] if stretch else row["started_at"]
    return resume_text({**row, "started_at": started}, pauses.chain_skips(row["id"]),
                       expired=expired)


def handle_resume_command(arg: str = "") -> str:
    """`/resume`. Sync, like /pace."""
    row = pauses.end("resume")
    if row is None:
        return "I wasn't paused."
    text = _stretch_text(row)
    _after_resume()
    return text


def _after_resume() -> None:
    try:
        from backend.brain import background
        background.resume_queued()
    except Exception as exc:
        log.debug("pause: could not restart queued jobs (%s)", exc)


# ---------------------------------------------------------------------------
# A timed pause ending on its own
# ---------------------------------------------------------------------------

def nudge_text(row: dict, skipped: int) -> str:
    """The once-a-day reminder that he is still paused."""
    scope = "everything I do on my own" if row["scope"] == ALL else "my own work"
    until = f"until {_clock(row['until_at'])}" if row.get("until_at") else "until you /resume"
    missed = (f" {skipped} thing{'s' if skipped != 1 else ''} skipped or stopped so far."
              if skipped else "")
    return (f"You paused {scope} on {_clock(row['started_at'])} and it is still paused "
            f"({until}).{missed} Send /resume when you want me back.")


async def sweep() -> int:
    """End pauses whose time ran out and say so, and remind him of one that has gone a day
    without a word. The gate itself already opened on time (`pauses.active` ignores an expired
    pause); this marks it and tells him. Returns how many pauses it ended."""
    count = await _end_expired()
    await _nudge()
    return count


async def _nudge() -> None:
    row = pauses.nudge_due()
    if row is None or _notifier is None:
        return
    pauses.mark_nudged(row["id"])          # first: at most one a day, even if Discord is down
    try:
        await _notifier(nudge_text(row, len(pauses.chain_skips(row["id"]))))
    except Exception as exc:
        log.debug("pause: reminder failed (%s)", exc)


async def _end_expired() -> int:
    ended = pauses.expire_due()
    for row in ended:
        _after_resume()
        text = _stretch_text(row, expired=True)
        if _notifier is not None:
            try:
                await _notifier(text)
            except Exception as exc:
                log.debug("pause: expiry notice failed (%s)", exc)
    return len(ended)


async def run_loop() -> None:
    from backend.brain import pacing
    await pacing.run_periodic(name="pause expiry", check_interval_s=60.0, on_cycle=sweep)

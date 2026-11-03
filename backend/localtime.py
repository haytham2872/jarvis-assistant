"""The owner's time zone, and the process clock set to it.

`JARVIS_TIMEZONE` (an IANA name such as Europe/Paris or America/New_York; default UTC) is
the zone every time JARVIS reads, schedules or shows is in: reminders, scheduled tasks, the
morning brief, the week plan, the dates on notes. Modules use `ZONE`.

About ten places also read the *machine's* clock (`datetime.now()`, `date.today()`,
`time.localtime()`): mood's time of day, the dates on letters and research notes, the day a
turn counts toward, backup and log stamps. A server left on UTC would put "Gym at 18:00" at
the wrong hour and turn the day over at the wrong time, so `apply()` sets the process clock
to the owner's zone. It runs when `backend` is imported, which every entry point does.

Windows has no `time.tzset()` and does not understand IANA names in `TZ`; there the
machine's own zone setting is what counts, so it should match `JARVIS_TIMEZONE`.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def _setting() -> str:
    # The environment first, then the project's .env (only this key is taken from it here:
    # this module runs before anything loads .env).
    if os.environ.get("JARVIS_TIMEZONE", "").strip():
        return os.environ["JARVIS_TIMEZONE"].strip()
    try:
        from dotenv import dotenv_values
        return (dotenv_values(Path(__file__).resolve().parent.parent / ".env")
                .get("JARVIS_TIMEZONE") or "").strip()
    except Exception:
        return ""


def _zone(name: str) -> tuple[str, ZoneInfo]:
    try:
        return name, ZoneInfo(name)
    except Exception:
        if name:
            log.warning("localtime: unknown JARVIS_TIMEZONE %r, using UTC", name)
        return "UTC", ZoneInfo("UTC")


TIMEZONE, ZONE = _zone(_setting() or "UTC")


def apply() -> bool:
    """Make local time in this process the owner's time. True when it was set and checked.

    Never raises: a machine without zone data keeps its own clock and says so in the log,
    rather than failing to start.
    """
    if not hasattr(time, "tzset"):
        return False
    try:
        os.environ["TZ"] = TIMEZONE
        time.tzset()
        # tzset() does not fail on an unknown zone, it quietly falls back to UTC. Compare
        # against the zone database so a host without tzdata is reported, not trusted.
        now = datetime.now()
        want = now.astimezone(ZONE).utcoffset()
        have = now.astimezone().utcoffset()
        if want != have:
            log.warning("localtime: TZ=%s did not take effect (offset %s, wanted %s); "
                        "is the host's tzdata installed?", TIMEZONE, have, want)
            return False
        return True
    except Exception as exc:
        log.warning("localtime: could not set the process time zone: %s", exc)
        return False

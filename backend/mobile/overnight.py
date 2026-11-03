"""Slice 6: a read-only story assembled by each source's own reader."""
from __future__ import annotations
from backend import localtime as _localtime
from backend import owner as _owner

import logging
import math
import re
import time
from datetime import datetime, time as clock, timedelta
from zoneinfo import ZoneInfo

from backend.mobile import notes

log = logging.getLogger(__name__)
LOCAL_TZ = _localtime.ZONE


def default_since(now: float) -> float:
    # Deliberately yesterday even at 23:00: the switch says "last evening".
    yesterday = datetime.fromtimestamp(now, LOCAL_TZ).date() - timedelta(days=1)
    return datetime.combine(yesterday, clock(22), tzinfo=LOCAL_TZ).timestamp()


def window(since, now: float) -> float:
    try:
        value = float(since)
        if math.isfinite(value) and now - 7 * 86400 <= value <= now:
            return value
    except (ValueError, TypeError, OverflowError) as exc:
        if since is not None:
            log.warning("mobile overnight: invalid since; using last evening (%s)", exc)
    return default_since(now)


def _day_at(day: str, since: float, now: float) -> float | None:
    """Date-only entries overlap the window, with explicit uncertainty on the card."""
    start = datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(), clock(), tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    return max(since, start.timestamp()) if start.timestamp() <= now and end.timestamp() > since else None


def _item(id, at, kind, title, detail="", link=None, ok=None, **extra):
    return {"id": str(id), "at": at, "kind": kind, "title": title, "detail": detail,
            "link": link, "ok": ok, **extra}


def _research(since, now):
    from backend.brain import research
    out = []
    for i, p in enumerate(research.snapshot(strict=True)["passes"]):
        if not since <= p["at"] <= now:
            continue
        link = None
        match = re.search(r"\bwrote topics/([^\s]+\.md)", p["outcome"])
        if match:
            link = notes.link("topics", match[1])
        out.append(_item(f"research:{p['at']}:{i}", p["at"], "research", p["subject"], p["outcome"], link,
                         False if p["outcome"].startswith("failed") else None))
    return out


def _ruminations(since, now):
    from backend.brain import rumination
    out = []
    for i, r in enumerate(rumination.snapshot()):
        at = _day_at(r["date"], since, now)
        if at is not None:
            out.append(_item(f"rumination:{r['date']}:{r['session']}:{i}", at, "rumination",
                             "A second thought", r["text"], notes.link("self", "ruminations.md"), date_only=True))
    return out


def _watches(since, now):
    from backend.storage import watches
    return [_item(f"watch:{w['watch_id']}", w["last_checked_at"], "watch", w["subject"],
                  f"Condition: {w['condition']}\n{w['alert_text']}", ok=True)
            for w in watches.fired_since(since, strict=True)]


def _schedule(since, now):
    from backend.brain import chronos
    ag = chronos.agenda(strict=True)
    details = ag["night_details"]
    out = []
    matched = set()
    for i, d in enumerate(ag["done"]):
        text, at = d["text"], d["at"].timestamp()
        night = text.startswith("night task ")
        result = next((r for r in reversed(details) if night and r["date"] == d["at"].date().isoformat()
                       and text.endswith(r["task"][:80])), None)
        if result:
            matched.add(id(result))
        if not since <= at <= now:
            continue
        link = notes.link("drafts", result["file"]) if result and result.get("file") else None
        out.append(_item(f"schedule:{at}:{i}", at, "night_task" if night else "timed_check",
                         result["task"] if result else text,
                         result["summary"] if result else text, link,
                         False if any(w in text for w in ("failed:", "interrupted", "missed:", "skipped:")) else None))
    for i, result in enumerate(details):
        if id(result) in matched:
            continue
        at = _day_at(result["date"], since, now)
        if at is not None:
            link = notes.link("drafts", result["file"]) if result.get("file") else None
            out.append(_item(f"night-result:{result['date']}:{i}", at, "night_task", result["task"],
                             result["summary"], link, result.get("status", "done") == "done", date_only=True))
    return out


def _forecasts(since, now):
    from backend.storage import forecasts
    out = []
    for f in forecasts.query(state=forecasts.RESOLVED, strict=True):
        if not since <= f["resolved_at"] <= now:
            continue
        p, outcome = f["stated_p"], f["outcome"]
        if not math.isfinite(p) or not 0 <= p <= 1 or outcome not in (0, 1):
            raise ValueError("Invalid settled forecast")
        ok = None if p == .5 else (p > .5) == bool(outcome)
        out.append(_item(f"forecast:{f['forecast_id']}", f["resolved_at"], "forecast", f["claim"],
                         f"Settled {'yes' if outcome else 'no'} · corrected {p:.0%}\n{f['resolution_quote']}",
                         ok=ok, forecast_id=f["forecast_id"]))
    return out


def _goals(since, now):
    from backend.storage import goals
    out = []
    for e in goals.overnight_since(since):
        payload = e["payload"]
        detail = (f"Milestone {payload['milestone']} finished" if e["event"] == "milestone" else
                  payload.get("question", "") if e["event"] == "question" else payload.get("text", ""))
        out.append(_item(f"goal:{e['id']}", e["at"], "goal", e["title"], detail,
                         goal_id=e["goal_id"], event=e["event"]))
    return out


def _skills(since, now):
    from backend.brain import skill_nightly
    report = skill_nightly.report(strict=True)
    if not report:
        return []
    at = _day_at(report["date"], since, now)
    if at is None:
        return []
    return [_item(f"skills:{report['date']}:{i}", at, "skills", _owner.say("«His» procedures changed overnight"), line,
                  date_only=True) for i, line in enumerate(report["lines"])]


def _job(t, id):
    note = t.get("note_file") or ""
    link = notes.link(*note.split("/", 1)) if "/" in note else None
    kind = "timed_check" if t["kind"] == "scheduled" else "job"
    return _item(f"job:{id}", t["finished"], kind, t["description"], t.get("summary") or t["status"],
                 link, t["status"] == "done")


def _live_jobs(since, now):
    from backend.brain import background
    return [_job(t, t["id"]) for t in background.snapshot()
            if t["status"] not in ("running", "queued") and t.get("finished") and since <= t["finished"] <= now]


def _durable_jobs(since, now):
    from backend.storage import jobs
    out = []
    for row in jobs.finished_since(since):
        p = row["payload"]
        if p.get("goal_id"):
            continue
        t = {**p, "status": row["state"], "finished": p.get("finished") or row["updated_at"]}
        if not since <= t["finished"] <= now:
            continue
        out.append(_job(t, p.get("display_id") or row["job_id"]))
    return out


def _letters(since, now):
    from backend.tools import vault
    folder = vault._resolve("letters", "_mobile_anchor.md").parent
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.iterdir()):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_[A-Za-z0-9_-]{8}\.md", path.name):
            continue
        link = notes.link("letters", path.name)
        if not link:
            continue
        at = path.stat().st_mtime
        if since <= at <= now:
            note = notes.read(**link)
            opening = "\n".join(note["text"].strip().splitlines()[:5])[:700]
            out.append(_item(f"letter:{path.name}", at, "letter", _owner.say("A letter to «his» future self"), opening, link))
    return out


_SOURCES = (("Research passes", _research), ("Second thoughts", _ruminations), ("Alerts", _watches),
            ("Night tasks and timed checks", _schedule), ("Settled forecasts", _forecasts),
            ("Goal steps", _goals), ("Procedure changes", _skills), ("Background jobs", _live_jobs),
            ("Saved background jobs", _durable_jobs), ("Letters", _letters))


def view(since=None, now: float | None = None) -> dict:
    """Each source is atomic and independent. Quiet means every source was readable."""
    now = time.time() if now is None else now
    since = window(since, now)
    out = {"as_of": now, "since": since, "items": [], "notes": [], "complete": True}
    by_id = {}
    for label, reader in _SOURCES:
        try:
            part = [i for i in reader(since, now) if since <= i["at"] <= now]
            for item in part:
                by_id.setdefault(item["id"], item)  # live snapshot takes priority over durable copy
        except Exception as exc:
            log.warning("mobile overnight: %s could not be read (%s)", label, exc, exc_info=True)
            out["notes"].append(f"{label} could not be read just now.")
            out["complete"] = False
    out["items"] = sorted(by_id.values(), key=lambda i: (i["at"], i["id"]))
    return out


def hub(since=None) -> dict:
    v = view(since)
    return {"overnight": {"since": v["since"], "count": len(v["items"]), "complete": v["complete"]},
            "notes": v["notes"]}

"""The phone's goals. Canonical SQLite records only; no model calls or worker changes."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from backend.brain import goal_budget as money, goals
from backend.storage import goals as store, operations

log = logging.getLogger(__name__)
MAX_FILE_BYTES = 100_000
_RANK = {store.ACTIVE: 0, store.WAITING: 1, store.PAUSED: 2}


class NotFound(LookupError):
    pass


class Unavailable(RuntimeError):
    pass


def require_goal(goal_id: str) -> dict:
    try:
        row = store.get(goal_id)
    except Exception:
        raise Unavailable("Goals could not be read just now.") from None
    if row is None:
        raise NotFound("That goal could not be found.")
    return row


def _part(notes: list[str], label: str, read, default):
    try:
        return read()
    except Exception as exc:
        log.warning("mobile goals: %s unreadable (%s)", label, exc)
        notes.append(f"{label} could not be read just now.")
        return default


def _wait(row: dict) -> dict:
    payload = row.get("payload") or {}
    label = (payload.get("question") or payload.get("when") or payload.get("watch_id")
             or payload.get("job_id") or payload.get("approval_id") or row["kind"])
    return {"wait_id": row["wait_id"], "kind": row["kind"], "label": str(label),
            "milestone_id": row["milestone_id"]}


def _card(row: dict, notes: list[str]) -> dict:
    gid = row["goal_id"]
    plan = _part(notes, "Milestones", lambda: store.plan(gid), [])
    waits = _part(notes, "Waiting items", lambda: store.waits(gid), [])
    working = next((m for m in plan if m["state"] == store.WORKING), None)
    now = f"Working on {working['milestone_id']}: {working['title']}" if working and row["state"] == store.ACTIVE else None
    return {"goal_id": gid, "title": row["title"], "state": row["state"],
            "created_at": row["created_at"], "deadline_at": row["deadline_at"],
            "budget_usd": money.usd(row["budget_microusd"]),
            # Includes reservations, as the coordinator's cap does. Detail separates them.
            "spent_usd": _part(notes, "Budget", lambda: money.usd(money.goal_spent(gid)), None),
            "milestones": {"done": sum(m["state"] == store.MS_DONE for m in plan), "total": len(plan)},
            "now": now, "waiting": [_wait(w) for w in waits], "outcome": row.get("outcome") or None}


def listing() -> dict:
    notes: list[str] = []
    off = _part(notes, "Goal availability", goals._unavailable, "Goals are unavailable.")
    out = {"enabled": not bool(off), "reason": "Goals are off on this server." if off else "",
           "proposals": [], "goals": [], "notes": notes}
    if off:
        return out
    live = _part(notes, "Goals", lambda: store.list_goals(tuple(store.LIVE), limit=100), [])
    ended = _part(notes, "Ended goals", lambda: store.list_goals(tuple(store.ENDED), limit=20), [])
    rows = sorted(live + ended, key=lambda g: (_RANK.get(g["state"], 3), -g["created_at"]))
    out["goals"] = [_card(row, notes) for row in rows]
    proposals = _part(notes, "Goal offers", goals._waiting_proposals, [])
    out["proposals"] = [{"goal_id": p["goal_id"], "title": p["title"],
                         "criteria": [c["text"] for c in store.active_criteria(p)],
                         "budget_usd": money.usd(p["budget_microusd"]),
                         "deadline": datetime.fromtimestamp(p["deadline_at"], timezone.utc).isoformat()
                         if p["deadline_at"] else None, "created_at": p["created_at"]} for p in proposals]
    return out


def hub() -> dict:
    view = listing()
    return {"goals": {"active": sum(g["state"] in (store.ACTIVE, store.WAITING) for g in view["goals"]),
                      "questions": sum(w["kind"] == "question" for g in view["goals"] if g["state"] in store.LIVE for w in g["waiting"]),
                      "proposals": len(view["proposals"]), "enabled": view["enabled"]}, "notes": view["notes"]}


def _bounded(text: str) -> tuple[str, bool]:
    body = (text or "").encode("utf-8")
    return body[:MAX_FILE_BYTES].decode("utf-8", errors="ignore"), len(body) > MAX_FILE_BYTES


def file(goal_id: str, name: str) -> dict:
    require_goal(goal_id)
    if store.valid_name(name):
        raise NotFound("That goal file could not be found.")
    row = store.read_file(goal_id, name)
    if row is None:
        raise NotFound("That goal file could not be found.")
    text, truncated = _bounded(row["body"])
    return {"name": row["name"], "revision": row["revision"], "size": row["byte_length"],
            "text": text, "truncated": truncated}


def evidence(goal_id: str, evidence_id: str, *, preview: bool = False) -> dict:
    require_goal(goal_id)
    row = store.get_evidence(goal_id, evidence_id)
    if row is None:
        raise NotFound("That evidence could not be found in this goal.")
    meta = row.get("metadata") or {}
    text, truncated = _bounded(row["body"])
    url = meta.get("url") or meta.get("source_url") or ""
    # A tool return sometimes owns the source URL rather than its metadata.
    if not url:
        found = re.search(r"https?://[^\s<>\]\)\"']+", text)
        url = found.group(0) if found else ""
    return {"id": row["evidence_id"], "title": str(meta.get("title") or row["tool"]),
            "url": str(url), "tool": row["tool"], "excerpt": text[:500] if preview else text,
            "truncated": truncated or bool(meta.get("truncated")) or (preview and len(text) > 500)}


def _session(goal_id: str) -> dict | None:
    session = store.latest_session(goal_id)
    if not session:
        return None
    rows = operations.operations_for_run(session["run_id"])
    lines = [(r["started_at"], f"{r['tool']} · {r['outcome']}") for r in rows]
    lines += [(n["created_at"], f"{n['section']}: {n['body'][:240]}") for n in store.notes(goal_id)
              if n["created_at"] >= session["started_at"]]
    # Goal events are canonical too, but most tool/note progress lives in receipts/notes.
    lines += [(e["created_at"], f"Answered: {e['payload'].get('answer', '')[:240]}")
              for e in store.events(goal_id, "answer", limit=8) if e["created_at"] >= session["started_at"]]
    return {"state": session["state"], "started_at": session["started_at"],
            "steps": [text for _, text in sorted(lines, key=lambda pair: pair[0])[-8:]]}


def _charges(goal_id: str) -> dict:
    rows = store.charges(goal_id)
    return {"spent_usd": money.usd(sum((r["settled_microusd"] or 0) for r in rows if r["state"] != "reserved")),
            "reserved_usd": money.usd(sum(r["reserved_microusd"] for r in rows if r["state"] == "reserved"))}


def confirmation(action: str, text: str, goal: dict) -> str:
    """Tool prose addresses a model; the phone confirms the owner's action directly."""
    state = goal["state"]
    if action == "answer":
        return "That question is already settled." if "already settled" in text else "Answer saved for the next session."
    if action == "budget":
        return f"Total budget is now ${goal['budget_usd']:.2f}. The goal can carry on."
    if action == "pause" and state == store.PAUSED:
        return "Goal paused. No new sessions or spending." + (
            " Pending approvals will be offered again when you resume." if "withdrawn" in text else "")
    if action in ("start", "resume") and state == store.ACTIVE:
        return "Goal started. Follow its progress below." if action == "start" else "Goal resumed."
    if action == "cancel" and state == store.CANCELLED:
        return "Goal cancelled. Its notes and files are kept."
    return f"This goal is already {state}."


def detail(goal_id: str) -> dict:
    row = require_goal(goal_id)
    notes: list[str] = []
    out = _card(row, notes)
    off = _part(notes, "Goal availability", goals._unavailable, "unavailable")
    out.update(enabled=not bool(off), reason="Goals are off on this server." if off else "", notes=notes)
    plan = _part(notes, "Milestones", lambda: store.plan(goal_id), [])
    out["milestones"] = [{"id": m["milestone_id"], "title": m["title"], "state": m["state"],
                          "depends": m["dependencies"], "outputs": m["accepted"].get("artifact_refs", []),
                          "summary": m["accepted"].get("summary", "")} for m in plan]
    out["session"] = _part(notes, "Session", lambda: _session(goal_id), None)
    out["questions"] = [{"wait_id": w["wait_id"], "text": w["payload"].get("question") or "Your answer?"}
                        for w in _part(notes, "Questions", lambda: store.waits(goal_id), []) if w["kind"] == "question"]
    out["charges"] = _part(notes, "Charges", lambda: _charges(goal_id), {"spent_usd": None, "reserved_usd": None})
    out["files"] = [{"name": f["name"], "revision": f["revision"], "size": f["byte_length"]}
                    for f in _part(notes, "Files", lambda: store.files(goal_id), [])]
    result = _part(notes, "Result", lambda: store.read_file(goal_id, "result.md"), None)
    out["result"], out["result_truncated"] = _bounded(result["body"]) if result else (None, False)
    cited = list(dict.fromkeys(re.findall(r"\[(ev[A-Za-z0-9_-]+)\]", out["result"] or "")))
    out["evidence"] = []
    for ref in cited:
        item = _part(notes, f"Evidence {ref}", lambda: evidence(goal_id, ref, preview=True), None)
        if item:
            out["evidence"].append(item)
    return out

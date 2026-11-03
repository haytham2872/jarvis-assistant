"""What the tools did in the earlier turns of a conversation, for the next turn to read.

The live history keeps only the owner's words and the final reply of each turn. The tool
calls and what they returned are gone once a turn ends, so on a later turn JARVIS can see
that he SAID a tool refused something but not whether it did. Live (28 October 2026): a
sale could not be recorded (the figures were only in a screenshot, so the ledger refused
them), he told the owner so, and three messages later, with nothing but his own words to go
on, he told him the refusals "were my assertions, not the tool's return" -- a false
confession, written after a true report. The same gap lets him invent a cause for a refusal
he can no longer read. This block is written by code from the operation receipts (A02), so
it is the record, not his memory of it.
"""
from __future__ import annotations
from backend import owner as _owner

import re
import time

from backend.storage import db

_MAX_TURNS = 8           # the most recent turns that used tools
_MAX_CHARS = 2400        # the whole block
_ERROR_CHARS = 170       # one refusal's reason
_RESULT_CHARS = 90       # what a successful write said
_SAID_CHARS = 70         # his words, to say which turn it was
_SLACK_S = 5.0           # a refilled history is dated when the reply was stored, not begun

_OUTCOME_WORDS = {"failed": "refused", "unknown": "cut off mid-call, outcome unknown",
                  "pending": "became an approval request, not done"}


def _rows(channel: str, since: float | None, exclude_run: str | None,
          conversation_end: float | None = None) -> list[dict]:
    """Receipts of the runs on `channel` that ended at or after `since`, oldest first."""
    if since is None:
        return []
    conn = db.connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT r.run_id, r.started_at AS run_at, r.user_preview, o.tool, o.outcome, "
            "o.args_preview, o.result_preview, o.error FROM runs r "
            "JOIN operations o ON o.run_id = r.run_id "
            "WHERE r.channel = ? AND COALESCE(r.ended_at, r.started_at) >= ? "
            "AND COALESCE(r.ended_at, r.started_at) <= ? AND r.run_id != ? "
            "ORDER BY r.started_at, o.started_at",
            (channel, since - _SLACK_S, conversation_end if conversation_end is not None else time.time() + 1,
             exclude_run or "")).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []          # no runs/operations table yet, or storage unavailable


def _action(args_preview: str) -> str:
    m = re.search(r"""['"]action['"]\s*:\s*['"]([\w-]+)['"]""", args_preview or "")
    return m.group(1) if m else ""


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return (cut or text[:n]) + "…"


def _reason(row: dict) -> str:
    text = row.get("error") or row.get("result_preview") or ""
    return _clip(re.sub(r"^\s*Error:\s*", "", text), _ERROR_CHARS)


def _call(row: dict, reads: frozenset[str]) -> tuple[str, str]:
    """(the call, what came of it)."""
    action = _action(row.get("args_preview", ""))
    name = f"{row['tool']} {action}".strip()
    outcome = row.get("outcome") or ""
    if outcome == "ok":
        head = _clip(row.get("result_preview", ""), _RESULT_CHARS) if action and action not in reads else ""
        return name, "ok" + (f" ({head})" if head else "")
    what = _OUTCOME_WORDS.get(outcome, outcome)
    reason = _reason(row) if outcome == "failed" else ""
    return name, what + (f": {reason}" if reason else "")


def lines(rows: list[dict], reads: frozenset[str] = frozenset(), *, who: str = _owner.say("«he»"),
          max_turns: int = _MAX_TURNS, max_chars: int = _MAX_CHARS) -> tuple[list[str], bool]:
    """One line per turn that used a tool, newest last, and whether older ones were left out.
    `who` is how the line names the owner ("he" for JARVIS, "you" for the owner himself). Pure."""
    turns: list[tuple[dict, list[list]]] = []
    for row in rows:
        if not turns or turns[-1][0]["run_id"] != row["run_id"]:
            turns.append((row, []))
        name, result = _call(row, reads)
        calls = turns[-1][1]
        # The same call with the same result, in a row: once, with a count.
        if calls and calls[-1][0] == name and calls[-1][1] == result:
            calls[-1][2] += 1
        else:
            calls.append([name, result, 1])
    out = []
    for run, calls in turns[-max_turns:]:
        said = _clip(run.get("user_preview", ""), _SAID_CHARS)
        when = time.strftime("%H:%M", time.localtime(float(run.get("run_at") or 0)))
        out.append(f"{when}, after {who} said \"{said}\": " + "; ".join(
            f"{name}, {n} calls, each {result}" if n > 1 else f"{name}: {result}"
            for name, result, n in calls))
    while len("\n".join(out)) > max_chars and len(out) > 1:
        out.pop(0)
    return out, len(turns) > len(out)


def render(rows: list[dict], reads: frozenset[str] = frozenset()) -> str:
    """The block, or '' when no earlier turn used a tool. Pure."""
    turn_lines, cut = lines(rows, reads)
    if not turn_lines:
        return ""
    body = "\n".join("- " + line for line in turn_lines)
    older = " (older turns not shown)" if cut else ""
    return (
        "[Tool record of the earlier turns of this conversation" + older + _owner.say(" -- written by code "
        "from the tool receipts, not by you. Your history holds only what you said; this is what "
        "your tools actually did and returned. A turn not listed used no tool.\n"
        f"{body}\n"
        "What you told «him» a tool did or refused is true when it is here: do not take it back or "
        "call it invented. Something you said a tool did that is not here did not happen: say so "
        "plainly. When a tool refused, its reason is the one above; do not guess at a fault you "
        "cannot see. This record is for you; don't recite it to «him».]")
    )


def text(channel: str, since: float | None, exclude_run: str | None = None,
         reads: frozenset[str] = frozenset()) -> str:
    """The block for a live turn: receipts since the oldest turn in its history."""
    try:
        return render(_rows(channel, since, exclude_run), reads)
    except Exception:
        return ""


def _conversation_start(channel: str) -> float | None:
    """When the channel's latest conversation began: the same run of exchanges a restart puts
    back into the live history (history_refill), counted back from its newest exchange, so an
    hour-old conversation is still "the last one". None when the channel has none."""
    from backend.brain import history_refill, pacing
    from backend.brain.threads import SESSION_GAP_THRESHOLD
    from backend.storage import exchanges
    rows = exchanges.recent_exchanges(limit=40, channel=channel)
    if not rows:
        return None
    newest = max(float(r.get("timestamp") or 0.0) for r in rows)
    new_at = pacing.load_state(history_refill._STATE_FILE).get("new_at", {}).get(channel)
    kept = history_refill.conversation_to_restore(
        rows, newest + 1, SESSION_GAP_THRESHOLD, float(new_at) if new_at is not None else None, 40)
    return float(kept[0]["timestamp"]) if kept else None


def trace(channel: str) -> tuple[float | None, list[str], bool]:
    """For the owner himself (/trace on Discord, the phone's Activity tab): (when his latest
    conversation on `channel` began, one line per turn of it that used a tool, whether older
    turns were left out). Never raises."""
    try:
        from backend.brain.engine import _READ_ACTIONS
        since = _conversation_start(channel)
        if since is None:
            return None, [], False
        found, cut = lines(_rows(channel, since, None), _READ_ACTIONS, who="you",
                           max_turns=20, max_chars=3500)
        return since, found, cut
    except Exception:
        return None, [], False


def trace_text(channel: str) -> str:
    """/trace: what my tools did in our latest conversation, from the receipts."""
    since, found, cut = trace(channel)
    if since is None:
        return "There's no conversation of ours to trace yet."
    began = time.strftime("%H:%M on %d %b", time.localtime(since))
    if not found:
        return f"No tool calls in our conversation so far (it began at {began})."
    return ("What my tools did in our conversation since " + began + ", from the tool receipts"
            + (" (older turns left out)" if cut else "") + ":\n"
            + "\n".join("- " + line for line in found))


def for_window(channel: str, start: float, end: float,
               reads: frozenset[str] = frozenset()) -> str:
    """The block for a closed session (consolidation): receipts of the runs that ended in it."""
    try:
        return render(_rows(channel, start, None, conversation_end=end + _SLACK_S), reads)
    except Exception:
        return ""

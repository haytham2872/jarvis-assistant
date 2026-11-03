"""backend/brain/rumination.py — l'esprit de l'escalier.

Every other reflective pass in this codebase reflects on a TASK
(background.py's worker, research.py's synthesis). This one reflects on a
CONVERSATION, after the fact — the thought that catches up to you on the
stairs once the conversation has already ended.

~30 minutes after a channel goes quiet, one cheap LLM pass re-reads that
session's threads and asks: was there one thing I missed, answered too
quickly, or only connected just now? At most one such "staircase thought"
is produced, written to jarvis-vault/self/ruminations.md, and delivered
exactly once into the NEXT turn's volatile system block — on whatever turn
comes next, in whatever channel, regardless of whether that turn continues
the same session or starts a new one. Deliberately NOT coupled to
assign_session()/consolidation's 2h gap logic: eligibility here is purely
"channel idle >= _IDLE_MIN_S and this session hasn't been ruminated on yet".
A thought generated mid-session (the owner returns before the 2h gap) is still
delivered on the very next turn of that same session — that's the intended
behavior, not a bug.

Same DI pattern as chronos.py/research.py (never imports backend.main; busy
check injected at startup), same silent-degradation contract (any failure
logs and waits for the next cycle), same state-file family
(.jarvis_rumination.json for operational state, the vault file for content).
"""

import asyncio
import datetime
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from backend.brain.threads import recent_threads, threads_for_session
from backend.brain import pacing

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_VAULT = _PROJECT_ROOT / "jarvis-vault"
_STATE_FILE = _PROJECT_ROOT / ".jarvis_rumination.json"

_CHECK_INTERVAL_S = 60.0

_IDLE_MIN_S = 30 * 60      # channel must be quiet this long before ruminating
_MAX_AGE_S = 12 * 3600     # freshness window — don't ruminate on a stale session
_MIN_THREADS = 3           # skip trivial sessions (a couple of pings, a "/new")
_MIN_MAX_SIG = 0.5         # skip sessions where nothing was significant enough to chew on

_RUMINATION_MAX_TOKENS = 600
_THREAD_TEXT_CAP = 800     # per-side cap when quoting a high-sig thread's full text
_THOUGHT_MAX_CHARS = 700

_MAX_RUMINATED_IDS = 50    # cap on the remembered-sessions list, oldest trimmed first

_CHANNELS = ("local", "discord")

_busy_check: Optional[Callable[[], bool]] = None


def set_busy_check(fn: Callable[[], bool]) -> None:
    """Injected: returns True while a conversation turn holds the pipeline lock."""
    global _busy_check
    _busy_check = fn


# ---------------------------------------------------------------------------
# State (.jarvis_rumination.json)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    return pacing.load_state(_STATE_FILE)


def _save_state(state: dict) -> None:
    pacing.save_state(_STATE_FILE, state, log_prefix="rumination")


def _update_state(mutate) -> dict:
    """Atomic read-modify-write. A04 slice 4: this function awaits an LLM call and a
    vault write between reading state and writing it back, while pop_rumination_mention()
    writes the same file on EVERY turn -- so the stale write-back could resurrect an
    already-delivered thought or drop another channel's `ruminated` marker."""
    return pacing.update_state(_STATE_FILE, mutate, log_prefix="rumination")


def _mark_ruminated(state: dict, sid: str) -> None:
    ruminated = state.get("ruminated", [])
    if sid not in ruminated:
        ruminated.append(sid)
    state["ruminated"] = ruminated[-_MAX_RUMINATED_IDS:]


# ---------------------------------------------------------------------------
# Delivery — pop-once, mirrors background.pop_completed_notices /
# research.pop_deferred_mentions / chronos.pop_pending_mentions exactly.
# ---------------------------------------------------------------------------

_RUMINATION_FRAME = (
    "[A thought you had after the last conversation ended — something you "
    "missed, answered too quickly, or only connected later. Bring it up "
    "naturally if the moment fits; let it go if it doesn't. Never present "
    "it as a system event.]"
)


def pop_rumination_mention() -> str:
    """The pending staircase thought, framed and cleared — exactly once."""
    state = _load_state()
    pending = state.get("pending")
    if not pending:
        return ""
    state["pending"] = None
    _save_state(state)
    text = (pending or {}).get("text", "")
    if not text:
        return ""
    return f"{_RUMINATION_FRAME}\n\n{text}"


# ---------------------------------------------------------------------------
# Pure helpers — eligibility, prompt, cleanup
# ---------------------------------------------------------------------------

def snapshot() -> list[dict]:
    """Dated staircase thoughts. The writer records a day, never an exact clock time.

    Absent is empty; an unreadable note raises so read-only views can admit the gap.
    """
    from backend.tools import vault
    path = vault._resolve("self", "ruminations.md")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.warning("rumination: thoughts could not be read (%s)", exc)
        raise
    import re
    headings = list(re.finditer(r"^## (\d{4}-\d{2}-\d{2}) \(session ([^)]+)\)\s*$", text, re.M))
    return [{"date": h[1], "session": h[2],
             "text": text[h.end():headings[i + 1].start() if i + 1 < len(headings) else len(text)].strip()}
            for i, h in enumerate(headings)]


def _eligible(threads: list[dict], now: float) -> tuple[bool, str]:
    """Pure gate: may this session be ruminated on? (ok, reason-if-not)."""
    if not threads:
        return False, "no threads"
    if len(threads) < _MIN_THREADS:
        return False, f"too few threads ({len(threads)} < {_MIN_THREADS})"
    max_sig = max(float(t.get("significance", 0.0)) for t in threads)
    if max_sig < _MIN_MAX_SIG:
        return False, f"nothing significant (max sig {max_sig:.2f} < {_MIN_MAX_SIG})"
    last_ts = float(threads[-1].get("timestamp", now))
    age = now - last_ts
    if age > _MAX_AGE_S:
        return False, f"session too old ({age / 3600:.1f}h > {_MAX_AGE_S / 3600:.0f}h)"
    return True, ""


def _build_prompt(threads: list[dict]) -> str:
    lines: list[str] = []
    for t in threads:
        if t.get("high"):
            u = (t.get("user") or "").strip()[:_THREAD_TEXT_CAP]
            a = (t.get("assistant") or "").strip()[:_THREAD_TEXT_CAP]
            lines.append(f"U: {u}\nJ: {a}")
        else:
            lines.append(f"[{(t.get('summary') or '').strip()}]")
    transcript = "\n\n".join(lines)
    return (
        "You are JARVIS. The conversation below ended a while ago. Re-read it "
        "now, after the fact — the way a thought catches up to you on the "
        "stairs once a conversation is already over.\n\n"
        f"{transcript}\n\n"
        "Is there ONE thing you missed, answered too quickly, or only "
        "connected just now? If so, write it as a private afterthought — "
        "2-4 sentences, first person, a genuinely new thought, not a "
        "summary of what was said. If nothing like that occurs to you, "
        "respond with exactly the single word: NOTHING"
    )


def _clean_thought(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    text = text.strip('"').strip()
    if not text or text.strip().upper().rstrip(".") == "NOTHING":
        return ""
    if len(text) <= _THOUGHT_MAX_CHARS:
        return text
    truncated = text[:_THOUGHT_MAX_CHARS]
    boundary = max(truncated.rfind(". "), truncated.rfind(".\n"))
    if boundary > 100:
        return truncated[:boundary + 1]
    return truncated.rstrip() + "…"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

async def _try_ruminate_channel(channel: str, now: float) -> None:
    state = _load_state()
    if state.get("pending"):
        return  # one global pending slot at a time

    ruminated = set(state.get("ruminated", []))

    recent = await recent_threads(1, channel=channel)
    if not recent:
        return
    last = recent[0]
    last_ts = float(last.get("timestamp", now))
    if now - last_ts < _IDLE_MIN_S:
        return

    sid = last.get("session_id")
    if not sid or sid in ruminated:
        return

    threads = await threads_for_session(sid)
    ok, reason = _eligible(threads, now)
    if not ok:
        log.debug("rumination: session %s skipped (%s)", sid[:8], reason)
        _update_state(lambda st: _mark_ruminated(st, sid))
        return

    from backend.brain.research import _llm
    from backend.storage.llm_calls import labelled
    with labelled("rumination"):
        raw = await _llm(_build_prompt(threads), _RUMINATION_MAX_TOKENS)
    thought = _clean_thought(raw)

    if not thought:
        _update_state(lambda st: _mark_ruminated(st, sid))
        return

    # Persist to the vault — read-then-full-write via vault_write so this
    # note gets semindexed (a deliberate upgrade over mood-patterns.md's
    # direct pathlib append, which bypasses indexing entirely).
    try:
        from backend.tools.vault import vault_write
        path = _VAULT / "self" / "ruminations.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else "# Ruminations\n"
        date_str = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        new_content = existing + f"\n## {date_str} (session {sid[:8]})\n{thought}\n"
        await vault_write("ruminations.md", new_content, "self")
    except Exception as e:
        log.debug("rumination: vault write failed: %s", e)

    def _store(st):
        _mark_ruminated(st, sid)
        st["pending"] = {"sid": sid, "text": thought, "created": now}
    _update_state(_store)
    log.info("rumination: generated a staircase thought for session %s (channel=%s)",
              sid[:8], channel)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

async def _cycle() -> None:
    busy = _busy_check() if _busy_check is not None else False
    if busy:
        return
    now = time.time()
    for channel in _CHANNELS:
        await _try_ruminate_channel(channel, now)


async def run_loop() -> None:
    """Started as a background task from main.py's lifespan. Every cycle,
    tries each channel in turn; skips entirely while the pipeline is busy
    (threads_for_session + one LLM call shouldn't contend with a live turn)."""
    await pacing.run_periodic(
        name="rumination", check_interval_s=_CHECK_INTERVAL_S, on_cycle=_cycle, pausable="work",
    )

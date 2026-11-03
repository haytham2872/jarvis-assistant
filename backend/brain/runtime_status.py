"""backend/brain/runtime_status.py — "vital signs" for the running process.

The self-model gives JARVIS static anatomy (sliced from CLAUDE.md, describing
how the system is *built*); this gives him proprioception — how it's actually
*running* right now. Pulled together read-only from research.py (pace),
background.py (active tasks), canary.py (memory health), usage.py (session
cost), and this module's own turn/uptime counters.

Fully synchronous, no tool call, no network (local SQLite reads only) — callable directly from
personality.py the same way get_section() is, consistent with the rule that
JARVIS never uses a tool to look up his own internal state (it's already in
the system prompt). Same never-import-backend.main discipline as every other
brain module: _PROCESS_START is set at import time (this module loads early
in the startup chain via personality.py -> engine.py -> main.py, so it's
close enough to true process start for an uptime display).
"""

import time
import logging
import platform
import datetime

from backend.brain.router import OPENROUTER_STANDARD
from backend.brain.version import VERSION

log = logging.getLogger(__name__)

_PROCESS_START = time.time()


def _host_label() -> str:
    """Which machine this process is actually running on, right now.

    Added after a live incident (2026-09-08): CLAUDE.md describes the system
    as Windows-native throughout, but the always-on deployment is a Linux/GCP
    VM -- with nothing in the live system prompt saying so, JARVIS had no
    reliable way to tell which host he was on and had to guess mid-conversation
    ("this is a Linux box now..."). This is read-only proprioception, same as
    the rest of this module: no assumption baked into the prompt, just what
    platform.system() actually reports for the process running right now."""
    system = platform.system()
    if system == "Windows":
        return "Windows (desk — wake word, microphone, TTS, Electron UI; no screen or terminal tool)"
    if system == "Linux":
        return "Linux (always-on Discord deployment — no microphone, TTS, Electron UI, screen or terminal tool)"
    return system or "unknown"

_turn_counts: dict[str, int] = {}


def runtime_identity_text() -> str:
    """Small live identity block for every turn; full health stays on demand."""
    text = (
        "[Runtime identity]\n"
        f"Host: {_host_label()}\n"
        f"Version: {VERSION} | Model: {OPENROUTER_STANDARD} via OpenRouter"
    )
    # P2: while the owner has paused me, every turn says so (nothing is added otherwise).
    try:
        from backend.brain import pause as _pause
        held = _pause.prompt_line()
    except Exception:
        held = ""
    # P3: while a week plan he has been shown waits for his yes, every turn says so.
    try:
        from backend.brain import weekplan as _weekplan
        waiting = _weekplan.prompt_line()
    except Exception:
        waiting = ""
    # P4: ...and while an evening check-in waits for his answer.
    try:
        from backend.brain import planday as _planday
        checkin = _planday.prompt_line()
    except Exception:
        checkin = ""
    # G1a: his goals and a waiting goal proposal (no model call; empty while goals are off).
    try:
        from backend.brain import goals as _goals
        from backend.brain import goal_runner as _goal_runner
        goal_line = _goals.prompt_line() or _goal_runner.status_line()
    except Exception:
        goal_line = ""
    # Phone slice 4: where his own messages go, when not Discord only.
    try:
        from backend.brain import notices as _notices
        delivery = _notices.prompt_line()
    except Exception:
        delivery = ""
    return "\n".join(part for part in (text, held, waiting, checkin, goal_line, delivery) if part)


def record_turn() -> None:
    """Increment today's turn count. Called once per turn from engine.py's
    chat(), alongside the existing _reset_turn_stats()."""
    key = datetime.date.today().isoformat()
    _turn_counts[key] = _turn_counts.get(key, 0) + 1


def turns_today() -> int:
    return _turn_counts.get(datetime.date.today().isoformat(), 0)


def uptime_seconds() -> float:
    return max(0.0, time.time() - _PROCESS_START)


def _format_uptime(seconds: float) -> str:
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_memory_health(last_check, failed: list[str]) -> str:
    if last_check is None:
        return "not checked yet this session"
    age_min = max(0.0, (time.time() - last_check) / 60.0)
    age = f"{age_min:.0f}m ago" if age_min < 60 else f"{age_min / 60:.1f}h ago"
    if failed:
        return f"checked {age} — {', '.join(failed)} FAILED"
    return f"checked {age} — all layers healthy"


def runtime_status_text() -> str:
    """The full [Runtime status] block injected into the system prompt on
    introspective triggers, and returned directly by the Discord /status
    command. Never raises -- any single field that can't be gathered
    degrades to a placeholder rather than losing the whole block."""
    try:
        from backend.brain import research
        pace = research.get_pace()
    except Exception as e:
        log.debug("runtime_status: pace unavailable: %s", e)
        pace = "unknown"

    try:
        from backend.brain import background
        running = background.running_count()
        queued = background.queued_count()
        finished = background.finished_count()
    except Exception as e:
        log.debug("runtime_status: background task counts unavailable: %s", e)
        running = queued = finished = 0

    try:
        from backend.storage import delivery as _delivery
        _delivery_counts = _delivery.counts()
        delivery_pending = _delivery_counts.get("pending", 0) + _delivery_counts.get("partial", 0)
        delivery_uncertain = _delivery_counts.get("uncertain", 0)
    except Exception as e:
        log.debug("runtime_status: delivery counts unavailable: %s", e)
        delivery_pending = delivery_uncertain = 0

    try:
        from backend.brain import embeddings as _emb
        _embed_waiting, _embed_limit = _emb.background_pressure()
    except Exception as e:
        log.debug("runtime_status: embed pressure unavailable: %s", e)
        _embed_waiting, _embed_limit = 0, 0

    # A09: "a system can appear healthy while ... an integration is unavailable".
    # A00 records collections it deliberately refused to delete because they hold real
    # data the active embedder can no longer read -- and nothing consumed that list, so
    # /status reported "all layers healthy" with 1,128 facts sitting unusable. The
    # canary does not catch it either: an embedder swap at the same dimension still
    # returns hits, they are just meaningless. Plain list read, no I/O.
    try:
        from backend.brain import memory as _mem
        _incompatible = _mem.incompatible_collections()
    except Exception as e:
        log.debug("runtime_status: collection compatibility unavailable: %s", e)
        _incompatible = []

    try:
        from backend.brain import canary
        last_check, failed = canary.last_check_info()
    except Exception as e:
        log.debug("runtime_status: canary info unavailable: %s", e)
        last_check, failed = None, []

    try:
        from backend.brain import usage
        cost_summary = usage.session_summary()
    except Exception as e:
        log.debug("runtime_status: usage summary unavailable: %s", e)
        cost_summary = "unavailable"

    # H0: how much the model reasons per call, and how often a call hits its output
    # limit. Local SQLite read, like the delivery counts above.
    try:
        from backend.storage import llm_calls as _llm_calls
        _call_lines = _llm_calls.status_lines()
    except Exception as e:
        log.debug("runtime_status: llm call stats unavailable: %s", e)
        _call_lines = []

    # Which web layer answered (TinyFish, or a fallback), so a silent slide onto the
    # keyless scrapers shows up here rather than only in the log. In-memory counter.
    try:
        from backend.tools import browser
        _web_line = browser.served_line()
    except Exception as e:
        log.debug("runtime_status: web counts unavailable: %s", e)
        _web_line = ""

    try:
        from backend.brain import semindex
        point_count = semindex.cached_point_count()
        semantic_size = f"{point_count:,} points" if point_count is not None else "unknown"
    except Exception as e:
        log.debug("runtime_status: semantic point count unavailable: %s", e)
        semantic_size = "unknown"

    # P2: the owner can switch my own work off (/pause). Say so, so that a quiet loop is
    # never mistaken for a broken one. Empty when not paused.
    try:
        from backend.brain import pause as _pause
        _pause_line = _pause.status_line()
    except Exception as e:
        log.debug("runtime_status: pause state unavailable: %s", e)
        _pause_line = ""


    lines = [
        "[Runtime status]",
        f"Host: {_host_label()}",
        f"Version: {VERSION} | Model: {OPENROUTER_STANDARD} via OpenRouter",
        f"Uptime: {_format_uptime(uptime_seconds())} | Pace: {pace}",
        *( [_pause_line] if _pause_line else [] ),
        f"Turns today: {turns_today()}",
        f"Background tasks: {running} running, {finished} completed/recovered"
        f", {queued} queued",
        *( [f"Discord replies: {delivery_pending} pending, {delivery_uncertain} uncertain "
            "(check backend log before retrying uncertain parts)"]
           if delivery_pending or delivery_uncertain else [] ),
        # A08: overload must be bounded AND observable. Only shown when something is
        # actually queued -- a permanent "0 waiting" line is noise nobody reads.
        *( [f"Indexing backlog: {_embed_waiting} embed job(s) waiting "
            f"(limit {_embed_limit} at a time, so live turns stay responsive)"]
           if _embed_waiting else [] ),
        f"Memory health: {_format_memory_health(last_check, failed)}",
        *( [f"DEGRADED: {c} holds {p:,} points the active embedder cannot read "
            f"({r}). Fact recall from it is unreliable until it is migrated "
            f"deliberately — back up first, then set "
            f"JARVIS_ALLOW_COLLECTION_DELETION=1."
            for c, r, p in _incompatible] ),
        f"Semantic index size: {semantic_size} (jarvis_semantic — retention is unbounded by design)",
        *_call_lines,
        *( [_web_line] if _web_line else [] ),
        f"Session cost (estimated):\n{cost_summary}",
    ]
    return "\n".join(lines)

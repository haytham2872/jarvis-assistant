"""backend/brain/history_bridge.py — bridges the conversation-history sliding-window cliff.

main.py caps each channel's live history at _MAX_HISTORY_TURNS (40) — older turns are
dropped from the message list sent to the model with no summary, no pointer, nothing (see
CLAUDE.md's Hardening 2026-09-01 note). jarvis_threads/mem0 already capture significant
exchanges regardless of this cap, but that's a separate retrieval path the model has to go
looking in; within a single long-running session, turn 41 was simply absent from context.

This module holds the pure logic for a bounded, cheap (no LLM call) rolling FIFO of
one-line-per-side previews for exactly the turns a channel's live window just dropped.
main.py owns the per-channel state dict and calls these functions; personality.py injects
the formatted text into the volatile system prompt block via build_system_prompt's
history_bridge param — see _HISTORY_BRIDGE_FRAME there.

Kept separate from main.py (which instantiates real audio hardware at import time and so is
never imported directly by this test suite) so the actual logic here is unit-testable in
isolation, same reasoning as pacing.py/bootstrap.py's extraction.
"""

_MAX_LINES = 20       # bounded FIFO, oldest trimmed first
_PREVIEW_CHARS = 100   # per-side preview length


def record_dropped(bridge: list[str], dropped: list[dict],
                   times: list[float] | None = None) -> None:
    """Append one-line previews for `dropped` onto `bridge` in place, then
    trim to _MAX_LINES.

    `dropped` is [user, assistant, user, assistant, ...] in original order —
    history is always appended in user/assistant pairs, but a stray odd
    trailing entry is skipped rather than raising, same silent-degradation
    contract as the rest of the brain.

    A03: `times` (parallel to `dropped`, one per entry) stamps each line with when the
    exchange actually happened, making the bridge **source-backed** — the roadmap asks
    compaction to "cite canonical exchanges" so the model can recover the full wording.
    A date is the citation the model can actually act on: `memory_timeline` takes dates,
    and the full exchange is now durable in SQLite and `jarvis_semantic` regardless of
    significance (A02 slice 1). An opaque row id would be untypeable — no tool fetches
    an exchange by id.

    `times` is optional and the format is unchanged without it, so a caller that has no
    timestamps degrades to the previous behavior rather than to a wrong date.
    """
    for i in range(0, len(dropped) - 1, 2):
        u = str(dropped[i].get("content", ""))[:_PREVIEW_CHARS]
        a = str(dropped[i + 1].get("content", ""))[:_PREVIEW_CHARS]
        when = _stamp(times[i]) if times and i < len(times) else ""
        bridge.append(f"{when}User: {u} | JARVIS: {a}")
    if len(bridge) > _MAX_LINES:
        bridge[:] = bridge[-_MAX_LINES:]


def _stamp(ts) -> str:
    """'[2026-09-21 14:03] ' for a usable timestamp, '' for anything else."""
    try:
        from datetime import datetime
        return datetime.fromtimestamp(float(ts)).strftime("[%Y-%m-%d %H:%M] ")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def format_text(bridge: list[str]) -> str:
    """Formatted bridge block for injection into the volatile system prompt,
    '' if nothing has aged out yet."""
    return "\n".join(bridge) if bridge else ""

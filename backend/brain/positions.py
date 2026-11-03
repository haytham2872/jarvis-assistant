"""backend/brain/positions.py — JARVIS's standing intellectual stances, patched not rewritten.

jarvis-vault/self/positions.md holds what JARVIS currently thinks about a subject and why,
with sources and a revision history — as opposed to a topics/ note, which is descriptive
knowledge ABOUT a subject, not a stance he holds.

The file is machine-owned, same discipline as predictions.py: consolidation no longer
rewrites positions.md wholesale. It emits small typed patches (add a new stance, revise an
existing one) as JSON; every byte of file surgery happens here, deterministically. This
removes the full-rewrite truncation risk the old positions_update field carried (a shrink-
floor guard could only detect a truncated rewrite after the fact and reject it, discarding
that session's actual stance-forming content) — there's no full-file token budget for a
truncated LLM response to blow, so there's nothing left to truncate. See CLAUDE.md's
Hardening 2026-09-01 note.

Format:
    # Positions

    ## nietzsche
    **Stance:** 2-4 sentences on the current stance.
    **Sources:** [[topics/nietzsche]], [[letters/2026-07-01_abcd1234]]
    **Formed:** 2026-07-01
    **Revisions:**
    - 2026-07-20 — softened X because Z

Same silent-degradation contract as the rest of the brain: every function here returns a
harmless default on malformed input rather than raising.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

_HEADER_TEMPLATE = "# Positions"

_HEADING_RE          = re.compile(r"^##\s+(.+?)\s*$")
_STANCE_RE           = re.compile(r"^\*\*Stance:\*\*\s*(.*)$")
_SOURCES_RE          = re.compile(r"^\*\*Sources:\*\*\s*(.*)$")
_FORMED_RE           = re.compile(r"^\*\*Formed:\*\*\s*(.*)$")
_REVISIONS_HEADER_RE = re.compile(r"^\*\*Revisions:\*\*\s*$")
_REVISION_LINE_RE    = re.compile(r"^-\s+(.*)$")


@dataclass
class Position:
    topic: str
    stance: str
    sources: str
    formed: str
    revisions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure parsing / rendering
# ---------------------------------------------------------------------------

def parse_file(text: str) -> dict[str, Position]:
    """Parse positions.md into {topic.lower(): Position}, keyed case-insensitively
    for patch lookup but preserving each Position's original-case topic string.

    Degrades cleanly on malformed/missing content: a heading with no
    recognized fields yet still produces a Position with blank strings,
    never raises.
    """
    positions: dict[str, Position] = {}
    if not text:
        return positions

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    for block in blocks:
        hm = _HEADING_RE.match(block[0])
        if not hm:
            continue
        topic = hm.group(1).strip()
        if not topic:
            continue
        stance = sources = formed = ""
        revisions: list[str] = []
        in_revisions = False
        for line in block[1:]:
            sm = _STANCE_RE.match(line)
            if sm:
                stance, in_revisions = sm.group(1).strip(), False
                continue
            so = _SOURCES_RE.match(line)
            if so:
                sources, in_revisions = so.group(1).strip(), False
                continue
            fm = _FORMED_RE.match(line)
            if fm:
                formed, in_revisions = fm.group(1).strip(), False
                continue
            if _REVISIONS_HEADER_RE.match(line):
                in_revisions = True
                continue
            if in_revisions:
                rm = _REVISION_LINE_RE.match(line)
                if rm:
                    revisions.append(rm.group(1).strip())
        positions[topic.lower()] = Position(
            topic=topic, stance=stance, sources=sources, formed=formed, revisions=revisions,
        )
    return positions


def render_position(p: Position) -> str:
    lines = [f"## {p.topic}", f"**Stance:** {p.stance}"]
    if p.sources:
        lines.append(f"**Sources:** {p.sources}")
    lines.append(f"**Formed:** {p.formed}")
    if p.revisions:
        lines.append("**Revisions:**")
        for r in p.revisions:
            lines.append(f"- {r}")
    return "\n".join(lines)


def render_file(positions: dict[str, Position]) -> str:
    """Deterministic full-file render from the parsed positions dict.

    Order is the dict's insertion order — apply_patch preserves existing
    order and appends new topics at the end, so a diff between versions of
    the file stays small and readable instead of reshuffling on every edit.
    """
    if not positions:
        return f"{_HEADER_TEMPLATE}\n\n(none yet)\n"
    parts = [render_position(p) for p in positions.values()]
    return f"{_HEADER_TEMPLATE}\n\n" + "\n\n".join(parts) + "\n"


def apply_patch(text: str, patches: list[dict], today: str) -> tuple[str, int]:
    """Apply small typed patch ops to positions.md, deterministically.

    patches: [{"topic": "...", "action": "add"|"revise", "stance": "...",
               "sources": "...", "revision_note": "..."}]

    - "add": creates a new position dated `today`. Ignored (not an error) if
      a position with that topic (case-insensitive) already exists — a
      "revise" was meant instead; skipping rather than clobbering an
      established stance is the safe default for a malformed/duplicate patch.
    - "revise": updates an EXISTING position's stance in place and appends a
      dated `revision_note` line under Revisions (only if a note was given).
      Ignored if the topic doesn't exist yet — an "add" was meant instead.

    Malformed entries (missing topic/stance, unrecognized action) are skipped
    individually — never raises, never corrupts the file.

    Returns (new_text, applied_count) so the caller can skip the write
    entirely when nothing in `patches` was valid, rather than rewriting an
    unchanged file.
    """
    positions = parse_file(text)
    applied = 0
    for patch in (patches or []):
        if not isinstance(patch, dict):
            continue
        topic = str(patch.get("topic", "")).strip()
        action = str(patch.get("action", "")).strip().lower()
        stance = str(patch.get("stance", "")).strip()
        if not topic or not stance or action not in ("add", "revise"):
            continue
        key = topic.lower()

        if action == "add":
            if key in positions:
                continue
            sources = str(patch.get("sources", "")).strip()
            positions[key] = Position(
                topic=topic, stance=stance, sources=sources, formed=today, revisions=[],
            )
            applied += 1
        else:  # revise
            existing = positions.get(key)
            if existing is None:
                continue
            note = str(patch.get("revision_note", "")).strip()
            existing.stance = stance
            if note:
                existing.revisions.append(f"{today} — {note}")
            applied += 1

    return render_file(positions), applied


# ---------------------------------------------------------------------------
# Async I/O — thin wrappers around the vault
# ---------------------------------------------------------------------------

async def write_file(content: str) -> None:
    """Overwrite positions.md via vault_write — semindex indexing free."""
    try:
        from backend.tools.vault import vault_write
        await vault_write("positions.md", content, "self")
    except Exception as e:
        log.warning("positions: write_file failed: %s", e)

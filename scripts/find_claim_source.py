r"""Where does JARVIS get a claim from? Read-only search of every place he keeps memory.

    .venv/bin/python scripts/find_claim_source.py "ledger is empty" "five lines" "\bre-?send"

Built after 5 October 2026: asked "good night", JARVIS said "the ledger is empty ... say the
five lines again" while the ledger held five positions. Conversation history starts empty after
a restart, so the claim came from something stored. This prints each match with where it
lives, when, and a short snippet, so a stale note can be found and fixed:

  vault        the queue, his portrait, letters, ruminations, drafts and every other note
  rumination   the staircase thought waiting to be said (.jarvis_rumination.json)
  facts        mem0 facts in Qdrant, with who said them and whether consolidated
  exchanges    the stored conversation turns (SQLite), newest first

Phrases are case-insensitive regular expressions; put \b in front of a word that is also
the end of another ("\bre-?send", or "pre-send" matches). Nothing is changed.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_SNIPPET = 110
_MAX_PER_SOURCE = 25
_MAX_PER_FILE = 5


def _snippet(text: str, m: re.Match) -> str:
    start, end = max(0, m.start() - _SNIPPET), min(len(text), m.end() + _SNIPPET)
    return ("..." if start else "") + " ".join(text[start:end].split()) + ("..." if end < len(text) else "")


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(ts or "?")


def vault(pattern: re.Pattern) -> list[str]:
    base = ROOT / "jarvis-vault"
    out = []
    files = sorted((p for p in base.rglob("*.md") if ".obsidian" not in p.parts),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Every match, not only the first: on its first server run, a letter's first match
        # ("pre-send") hid whatever else the same paragraph said.
        last_end = -1
        shown = 0
        for m in pattern.finditer(text):
            if m.start() < last_end or shown >= _MAX_PER_FILE:
                continue
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"  {path.relative_to(base)}:{line}  (modified {_when(path.stat().st_mtime)})\n"
                       f"      {_snippet(text, m)}")
            last_end, shown = m.end() + _SNIPPET, shown + 1
    return out


def rumination(pattern: re.Pattern) -> list[str]:
    path = ROOT / ".jarvis_rumination.json"
    try:
        pending = (json.loads(path.read_text(encoding="utf-8")) or {}).get("pending") or {}
    except (OSError, ValueError):
        return []
    text = json.dumps(pending, ensure_ascii=False)
    m = pattern.search(text)
    return [f"  pending staircase thought\n      {_snippet(text, m)}"] if m else []


def facts(pattern: re.Pattern) -> list[str]:
    try:
        from backend.brain import memory
        from backend.storage import qdrant
        name = memory._find_mem0_collection()
        if not name:
            return ["  (no mem0 fact collection found)"]
        client = qdrant.get_client(timeout=10)
    except Exception as e:
        return [f"  (facts not searched: {e})"]
    out, offset = [], None
    while True:
        points, offset = client.scroll(collection_name=name, limit=256, offset=offset,
                                       with_payload=True, with_vectors=False)
        for p in points:
            payload = p.payload or {}
            text = str(payload.get("data") or payload.get("memory") or payload.get("text") or "")
            m = pattern.search(text)
            if m:
                who = payload.get("attributed_to") or "?"
                state = "consolidated" if payload.get("consolidated") else "recallable"
                out.append(f"  fact {p.id}  said by {who}, {state}, created {payload.get('created_at', '?')}\n"
                           f"      {_snippet(text, m)}")
        if offset is None:
            break
    return out


def exchanges(pattern: re.Pattern) -> list[str]:
    try:
        from backend.storage import db
        conn = db.connect()
        rows = conn.execute("SELECT session_id, channel, timestamp, user_text, assistant_text FROM exchanges "
                            "ORDER BY timestamp DESC LIMIT 5000").fetchall() if conn else []
    except Exception as e:
        return [f"  (exchanges not searched: {e})"]
    out = []
    for session, channel, ts, user, assistant in rows:
        for who, text in (("Sam", user or ""), ("JARVIS", assistant or "")):
            m = pattern.search(text)
            if m:
                out.append(f"  {_when(ts)}  {channel}  session {str(session)[:8]}  {who} said:\n"
                           f"      {_snippet(text, m)}")
    return out


def main(phrases: list[str]) -> int:
    if not phrases:
        print(__doc__)
        return 2
    pattern = re.compile("|".join(f"(?:{p})" for p in phrases), re.I)
    for title, fn in (("VAULT NOTES", vault), ("RUMINATION", rumination), ("MEMORY FACTS", facts),
                      ("STORED CONVERSATION", exchanges)):
        try:
            hits = fn(pattern)
        except Exception as e:
            hits = [f"  (search failed: {e})"]
        found = sum(1 for h in hits if not h.startswith("  ("))
        print(f"\n== {title}: {found} match(es)")
        for h in hits[:_MAX_PER_SOURCE]:
            print(h)
        if len(hits) > _MAX_PER_SOURCE:
            print(f"  ... and {len(hits) - _MAX_PER_SOURCE} more")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Which past research passes had a current reason? Read-only, for research v2's thresholds.

    .venv/bin/python scripts/measure_research_reasons.py [--passes 60] [--root /path/to/jarvis]

For each of the last N passes in jarvis-vault/research-queue.md's [done] trail, it reports
what a reason-gated picker would have seen at that moment:
  conv  best cosine between the subject and the owner's messages in the 14 days before the pass
  hold  the subject names a stock the owner holds now (ticker or company name)
  goal  the subject shares a word (5+ letters) with a goal's title
  open  the topic note was opened on the phone in the 14 days before (tracking is new)
and the pass's outcome class (content, nothing_new, skipped, failed). It prints the subject
(a topic name) but never message text. The database is opened with mode=ro.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import re
import sqlite3
import statistics
import sys
from pathlib import Path

_DONE = re.compile(r"^\[done\] (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| (.+?) \[(\w+)\] \((queued|auto)\) \| (.*)$")
_WINDOW_DAYS = 14
_WORD = re.compile(r"[a-z]{5,}")


def outcome_class(outcome: str) -> str:
    o = outcome.lower()
    if o.startswith("wrote") or o.startswith("updated") or o.startswith("created"):
        return "content"
    if "no new" in o:
        return "nothing_new"
    if o.startswith("skipped"):
        return "skipped"
    return "failed"


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=60)
    ap.add_argument("--root", default=None)
    args = ap.parse_args()
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))                                     # the code, when run from /tmp
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # ...or from the repo itself

    trail = root / "jarvis-vault" / "research-queue.md"
    passes = []
    for line in trail.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _DONE.match(line.strip())
        if m:
            when = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").timestamp()
            passes.append({"ts": when, "stamp": m.group(1), "subject": m.group(2),
                           "angle": m.group(3), "queued": m.group(4) == "queued",
                           "cls": outcome_class(m.group(5))})
    passes = passes[-args.passes:]
    if not passes:
        print("No [done] passes found.")
        return 0

    con = sqlite3.connect(f"file:{root / '.jarvis_runtime.db'}?mode=ro", uri=True)
    first = passes[0]["ts"] - _WINDOW_DAYS * 86400
    exchanges = con.execute(
        "SELECT timestamp, user_text FROM exchanges WHERE timestamp >= ? AND channel != 'canary' "
        "ORDER BY timestamp", (first,)).fetchall()

    def table(name):
        return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()

    holdings: set[str] = set()
    if table("live_trades") and table("instruments"):
        for sym, name in con.execute(
                "SELECT i.symbol, i.name FROM instruments i WHERE i.symbol IN "
                "(SELECT instrument FROM live_trades)"):
            holdings.add(sym.lower())
            holdings.update(w for w in _WORD.findall(name.lower()) if w not in {"class", "group", "holdings"})
    goal_words: set[str] = set()
    if table("goals"):
        for (title,) in con.execute("SELECT title FROM goals"):
            goal_words.update(_WORD.findall(title.lower()))
    opens: list[tuple[str, float]] = []
    if table("note_opens"):
        opens = con.execute("SELECT name, ts FROM note_opens WHERE folder='topics'").fetchall()

    from backend.brain import embeddings
    texts = [p["subject"] for p in passes] + [(t or "")[:500] for _, t in exchanges]
    vecs = embeddings._embed_many_sync(texts) if texts else []
    subj_vecs, ex_vecs = vecs[:len(passes)], vecs[len(passes):]

    print(f"{'when':16} {'q':1} {'conv':>5} hold goal open  {'outcome':11} subject")
    rows = []
    for p, sv in zip(passes, subj_vecs):
        lo = p["ts"] - _WINDOW_DAYS * 86400
        conv = max((cosine(sv, ev) for (ts, _), ev in zip(exchanges, ex_vecs) if lo <= ts < p["ts"]),
                   default=0.0)
        words = set(_WORD.findall(p["subject"].lower())) | {w.lower() for w in re.findall(r"\b[A-Z]{2,5}\b", p["subject"])}
        hold = bool(words & holdings)
        goal = bool(words & goal_words)
        slug = re.sub(r"[^a-z0-9]+", "-", p["subject"].lower()).strip("-")
        opened = any(lo <= ts < p["ts"] and (slug[:20] in n or any(w in n for w in words))
                     for n, ts in opens)
        rows.append({**p, "conv": conv, "hold": hold, "goal": goal, "open": opened})
        print(f"{p['stamp']:16} {'Q' if p['queued'] else '-'} {conv:5.3f} {'yes ' if hold else ' -  '} "
              f"{'yes ' if goal else ' -  '} {'yes ' if opened else ' -  '} {p['cls']:11} {p['subject'][:50]}")

    print("\nConversation similarity by outcome (auto passes only):")
    for cls in ("content", "nothing_new", "skipped", "failed"):
        vals = sorted(r["conv"] for r in rows if r["cls"] == cls and not r["queued"])
        if vals:
            q = statistics.quantiles(vals, n=4) if len(vals) >= 4 else vals
            print(f"  {cls:11} n={len(vals):3}  min {vals[0]:.3f}  quartiles {[round(x, 3) for x in q]}  max {vals[-1]:.3f}")
    auto = [r for r in rows if not r["queued"]]
    for floor in (0.80, 0.82, 0.84, 0.86, 0.88):
        kept = [r for r in auto if r["conv"] >= floor or r["hold"] or r["goal"] or r["open"]]
        useful = sum(r["cls"] == "content" for r in kept)
        print(f"  floor {floor:.2f}: {len(kept):3} of {len(auto)} auto passes would run "
              f"({useful} of them added content; {sum(r['cls'] == 'content' for r in auto)} did overall)")
    print(f"\nHoldings known: {len(holdings) > 0}, goals: {len(goal_words) > 0}, note opens: {len(opens)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

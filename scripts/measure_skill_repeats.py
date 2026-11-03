"""Read-only: what the skills' overnight pass would pick on this host's real history.

    .venv/bin/python scripts/measure_skill_repeats.py

Prints counts only, never what Sam said: how many finished turns with tools the last
30 days hold, each repeat cluster (how many requests, on how many days, the tools, how many
shared words, whether an existing skill already covers it), the skill pairs that would be
offered to the model as merge candidates, and how many missed corrections it would fold.
Makes no model call and writes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.brain import skill_nightly as N  # noqa: E402
from backend.brain import skills  # noqa: E402
from backend.storage import lifelog  # noqa: E402


def main() -> None:
    now = lifelog.stamp()[0]
    runs = N._runs(now - N._LOOKBACK_DAYS * 86_400, now)
    with_tools = [r for r in runs if r["n_ops"] >= 2 and r["tools"]]
    print(f"Finished turns in the last {N._LOOKBACK_DAYS} days: {len(runs)} "
          f"({len(with_tools)} with 2+ tool calls, {sum(1 for r in with_tools if r['opened'])} of them "
          "followed a skill)")
    clusters = N.find_repeats(now)
    print(f"\nRepeat clusters (>= {N._MIN_REPEATS} requests on >= {N._MIN_DAYS} days): {len(clusters)}")
    for i, c in enumerate(clusters, 1):
        print(f"  {i}. {len(c['runs'])} requests on {len(c['days'])} days, tools {', '.join(c['tools'])}, "
              f"{len(c['words'])} shared words"
              + (f", already covered by skill '{c['covered_by']}'" if c["covered_by"] else ", not covered: would be learned"))
    pairs = N.merge_candidates(skills.active_skills())
    print(f"\nMerge candidates put to the model: {len(pairs)}")
    for a, b in pairs:
        print(f"  {a.name} + {b.name}")
    print(f"\nMissed corrections in the last day it would fold: {len(N.fold_candidates(now))}")


if __name__ == "__main__":
    main()

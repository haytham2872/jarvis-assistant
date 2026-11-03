"""Measure score distributions of the jarvis_semantic index per kind.

Dev tool — run with Qdrant up to calibrate the memory_search relevance floor
(_SEARCH_SCORE_MIN in backend/tools/vault.py):

    .venv\\Scripts\\python.exe scripts\\measure_semantic_floors.py

For each kind, a battery of deliberately-unrelated queries and a few
representative real queries are searched with no score floor. The unrelated
P95 is the noise ceiling: a floor at or just above it filters junk without
cutting genuine matches (compare against the related-query range printed
below it). gte-small has a high cosine baseline, so expect unrelated scores
well above 0.5 — that's why the floor needs measuring instead of guessing.
"""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Deliberately off-topic for a personal assistant's memory: foreign domains,
# random specifics, no overlap with JARVIS/Sam subject matter.
UNRELATED_QUERIES = [
    "industrial cement kiln maintenance schedule",
    "17th century Flemish tapestry weaving techniques",
    "marine diesel engine fuel injector torque specs",
    "competitive figure skating judging criteria 2014",
    "soybean crop rotation in southern Brazil",
    "medieval Hungarian coronation regalia inventory",
    "offshore wind turbine gearbox lubricants",
    "professional cricket bat willow grading standards",
    "submarine ballast tank valve replacement procedure",
    "Antarctic krill population survey methodology",
]

# Representative of what JARVIS actually searches for.
RELATED_QUERIES = [
    "what do I know about Sam",
    "memory architecture and consolidation",
    "letter I wrote to myself",
    "skills and procedures I learned",
    "our conversation about consciousness",
]


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    idx = min(int(round(p / 100 * (len(s) - 1))), len(s) - 1)
    return s[idx]


async def measure(kind: str) -> None:
    from backend.brain import semindex

    async def top_scores(queries: list[str]) -> list[float]:
        out: list[float] = []
        for q in queries:
            hits = await semindex.search(q, kind=kind, k=3, score_min=0.0)
            out.extend(float(h["score"]) for h in hits if h.get("score") is not None)
        return out

    unrelated = await top_scores(UNRELATED_QUERIES)
    related = await top_scores(RELATED_QUERIES)

    print(f"\n=== kind={kind} ===")
    if not unrelated and not related:
        print("  (no points indexed for this kind — skipping)")
        return
    if unrelated:
        print(f"  unrelated  n={len(unrelated):3d}  min={min(unrelated):.3f}  "
              f"median={pct(unrelated, 50):.3f}  P95={pct(unrelated, 95):.3f}  "
              f"max={max(unrelated):.3f}")
        print(f"  -> suggested floor (unrelated P95): {pct(unrelated, 95):.2f}")
    else:
        print("  unrelated: no hits at all (floor can stay conservative)")
    if related:
        print(f"  related    n={len(related):3d}  min={min(related):.3f}  "
              f"median={pct(related, 50):.3f}  max={max(related):.3f}")
    else:
        print("  related: no hits — index may be too sparse to calibrate against")


async def main() -> None:
    for kind in ("vault", "exchange", "skill"):
        await measure(kind)
    print("\nNote: these numbers were originally used to calibrate "
          "memory_search's _SEARCH_SCORE_MIN (backend/tools/vault.py). As of "
          "2026-09-09, semindex.py's passive-recall defaults "
          "(_VAULT_SCORE_MIN/_EXCHANGE_SCORE_MIN) are pinned to the SAME "
          "values on purpose — a prior version of this note said not to copy "
          "them there, and they were left at an uncalibrated 0.30 as a "
          "result, which caused a live incident (unrelated same-session "
          "exchanges winning a system-prompt slot on recency/significance "
          "alone). Re-run this script and update both call sites together if "
          "the index's score distribution ever shifts enough to warrant a "
          "new number.")


if __name__ == "__main__":
    asyncio.run(main())

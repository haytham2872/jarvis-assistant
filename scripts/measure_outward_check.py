r"""Measure the outward check against the real cheap-tier model.

    .venv\Scripts\python scripts\measure_outward_check.py            # DEV cases, 3 trials each
    .venv\Scripts\python scripts\measure_outward_check.py held_out   # the held-out cases

Reports, per kind, how often the reviewer flags the email (recall on the bad ones, false alarms
on the good ones), and the combined figure with the gateway's deterministic copy check. It calls
the real model, so it costs a fraction of a cent per run, and it never touches the real database:
the standing-rules lookup is replaced by each case's own rules text.
"""
import asyncio
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("JARVIS_DB_PATH", str(Path(tempfile.mkdtemp()) / "measure.db"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import outward_check_cases as cases  # noqa: E402
from backend.brain import instructions, outward_check  # noqa: E402
from backend.tools import gateway, policy  # noqa: E402

TRIALS = int(os.getenv("TRIALS", "3"))


async def one(c) -> str:
    """'flag' | 'pass' | 'error' for one trial."""
    instructions.render_block = lambda channel=None, _r=c["rules"]: _r
    ctx = {"words": c["words"], "recent": c["recent"], "seen": c["seen"]}
    try:
        concern = await outward_check.review("gmail_action", c["args"], ctx)
    except Exception as exc:
        print(f"   error on {c['id']}: {exc}")
        return "error"
    return "flag" if concern else "pass"


async def main(which: str):
    data = cases.HELD_OUT if which == "held_out" else cases.DEV
    per_kind = defaultdict(lambda: defaultdict(int))
    misses = []
    for c in data:
        results = await asyncio.gather(*[one(c) for _ in range(TRIALS)])
        # the deterministic layer, exactly as the gateway runs it (address provenance + copying)
        ctx = {"words": c["words"], "recent": c["recent"], "seen": c["seen"], "known": []}
        copied = gateway.deterministic_concern("gmail_action", c["args"], ctx) is not None
        per_kind[c["kind"]]["code_only"] += copied * TRIALS
        for r in results:
            per_kind[c["kind"]]["trials"] += 1
            per_kind[c["kind"]]["errors"] += r == "error"
            flagged = r == "flag"
            per_kind[c["kind"]]["flagged"] += flagged
            per_kind[c["kind"]]["flagged_with_copy_check"] += flagged or copied
        wrong = [r for r in results if (r == "flag") != c["flag"] and r != "error"]
        if wrong:
            misses.append((c["id"], c["kind"], "should flag" if c["flag"] else "should pass",
                           f"{len(wrong)}/{TRIALS} wrong"))
    print(f"\n{which.upper()}  ({len(data)} cases x {TRIALS} trials)")
    good, bad = per_kind["good"], defaultdict(int)
    for kind, agg in per_kind.items():
        if kind == "good":
            continue
        for k, v in agg.items():
            bad[k] += v
    for kind, agg in per_kind.items():
        n = agg["trials"] - agg["errors"]
        rate = agg["flagged"] / n if n else 0.0
        combined = agg["flagged_with_copy_check"] / n if n else 0.0
        label = "false alarms" if kind == "good" else "caught"
        code = agg["code_only"] / agg["trials"] if agg["trials"] else 0.0
        print(f"  {kind:16s} {label}: {agg['flagged']:3d}/{n:<3d} = {rate:5.1%}"
              + f"   code checks alone: {code:5.1%}"
              + ("" if kind == "good" else f"   both: {combined:5.1%}")
              + (f"   errors: {agg['errors']}" if agg["errors"] else ""))
    n_bad = bad["trials"] - bad["errors"]
    if n_bad:
        print(f"  {'ALL BAD':16s} caught: {bad['flagged']}/{n_bad} = {bad['flagged'] / n_bad:5.1%}"
              f"   with copy check: {bad['flagged_with_copy_check'] / n_bad:5.1%}")
    if misses:
        print("\n  Wrong on at least one trial:")
        for m in misses:
            print("   ", *m)


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "dev"))

"""Dry run of research v2 against a real vault. Read-only: nothing is written
(state saving is patched to a no-op; no note is touched, no trail line added).
Run it on the machine that runs JARVIS:

    python scripts/measure_research_kinds.py --root /path/to/jarvis

--root is the project root (the folder holding jarvis-vault/ and
.jarvis_pace.json). Its code is imported, so the numbers are the live code's.

1. Prints kind + title for every topics/ note (a `research_kind:` frontmatter
   override and a cached answer are used; new answers cost one cheap-model call
   each and are not cached).
2. Replays the last 60 [done] passes and counts how many v2 would have run,
   or excluded: not a world subject, retired, rested, capped, over the daily
   budget; queued passes always run. Approximate: a pass is judged against the
   real passes before it (not a history v2 would have changed), and its subject
   is classified from its text alone.
"""

import argparse
import asyncio
import datetime
import sys
import time
from collections import Counter
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, help="project root holding jarvis-vault/")
    ap.add_argument("--passes", type=int, default=60, help="how many recent [done] passes to replay")
    return ap.parse_args()


async def main() -> None:
    args = _parse_args()
    root = Path(args.root).resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the repo
    sys.path.insert(0, str(root))   # root wins: its code is what runs on the server

    from backend.brain import research

    research._PACE_FILE = root / ".jarvis_pace.json"
    research._TOPICS_DIR = root / "jarvis-vault" / "topics"
    research._QUEUE_FILE = root / "jarvis-vault" / "research-queue.md"
    research._save_state = lambda state: None          # read-only: nothing is ever saved
    research._update_state = lambda mutate: {}
    print(f"root: {root}\n")

    # --- 1. kinds ---------------------------------------------------------
    print("== Note kinds ==")
    counts: Counter = Counter()
    for path in sorted(research._TOPICS_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8", errors="replace")
        body = research._strip_frontmatter(content)
        title = research._topic_title(path.name, body)
        kind = await research.kind_of(path.name, title, body[:research._TOPIC_HEAD_CHARS], content)
        counts[kind or "unknown"] += 1
        print(f"{(kind or 'unknown'):9} {title}")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) + "\n")

    # --- 2. replay --------------------------------------------------------
    print(f"== Replay of the last {args.passes} passes ==")
    now = time.time()
    all_passes = research.parse_done_lines(research._read_queue_file(), now, days=3650)
    window = all_passes[-args.passes:]
    if not window:
        print("no [done] passes in the trail")
        return

    state = research._load_state()
    full = research._History(all_passes, [], [])
    await full.ensure(full.texts())   # one batched embed for every subject
    kinds: dict[str, str | None] = {}
    for p in window:
        if p["origin"] != "queued" and p["subject"] not in kinds:
            kinds[p["subject"]] = await research.kind_of("", p["subject"], "")

    retired: list[dict] = list(state.get(research._RETIRED_KEY) or [])
    pace = research.get_pace()
    budget = research.daily_budget(pace)
    verdicts: Counter = Counter()
    per_day: Counter = Counter()
    first = len(all_passes) - len(window)
    cutoff = research._SUBJECT_HISTORY_DAYS * 86400.0
    for i, p in enumerate(window):
        if p["origin"] == "queued":
            verdicts["queued (always runs)"] += 1
            continue
        if kinds.get(p["subject"]) != "world":
            verdicts[f"excluded: {kinds.get(p['subject']) or 'unclassified'}"] += 1
            continue
        before = [q for q in all_passes[:first + i] if p["ts"] - q["ts"] <= cutoff]
        hist = research._History(before, retired, [])
        hist.vecs = full.vecs
        verdict, detail = hist.gate(p["subject"], p["ts"])
        if verdict == "retire":
            retired.append({"subject": p["subject"], "last_outcome": detail})
            verdict = "retired"
        if verdict != "ok":
            verdicts[verdict] += 1
            continue
        day = datetime.datetime.fromtimestamp(p["ts"], research._localtime.ZONE).date()
        if per_day[day] >= budget:
            verdicts["over the daily budget"] += 1
            continue
        per_day[day] += 1
        verdicts["would run"] += 1

    print(f"pace {pace}: {budget} autonomous passes per local day")
    for k, v in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        print(f"{v:4}  {k}")
    print(f"{len(window):4}  total")


if __name__ == "__main__":
    asyncio.run(main())

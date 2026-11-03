"""Calibrate Jev's `goal` question (G3, docs/archive/GOALS-ROADMAP.md section 12), and compare Jev's
latency with and without it on the same pinned model.

Paid (Jev, ~$0.00007 a call) and opt-in. Writes hashes, labels and numbers only, never the
text of a real message.

    .venv/Scripts/python.exe scripts/measure_goal_signal.py \\
        --real <real.json> --labels <labels.json> --out docs/goals-g3-calibration-<date>.jsonl \\
        [--latency 40] [--cap 0.10]

real.json: [{"sha", "session", "summary_only", "user"}]   (dumped read-only from jarvis_threads)
labels.json: {"<sha>": true|false}   (by hand against section 12's definition; missing = false)

The halves are split by group: a session's messages, or a boundary pair, stay together.
A threshold is chosen on each half (best F1, ties to the higher threshold) and scored on
the other, both directions. Candidate keyword phrases are scored on every negative: a
phrase may join KEYWORD_OR only with zero false fires.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from backend.brain import turn_signals as ts  # noqa: E402
from goal_calibration_cases import CASES  # noqa: E402

# Candidate keyword fallbacks, scored, never assumed safe.
KEYWORDS = [r"\bkeep (looking|chasing|working on|an eye on|after|track)\b",
            r"\bover the (next|coming) (few )?(days|weeks|month|months)\b",
            r"\buntil it'?s (done|sorted|solid|signed)\b", r"\btrack .{0,30}\bthrough\b",
            r"\bfollow up with\b", r"\bcontinue de chercher\b"]
_KW = re.compile("|".join(KEYWORDS), re.I)

SPEND = {"usd": Decimal(0), "calls": 0}


def _half(group: str) -> str:
    return "A" if int(hashlib.sha256(group.encode()).hexdigest(), 16) % 2 == 0 else "B"


async def _jev(text: str, with_goal: bool = True) -> tuple[float | None, float, float | None, str]:
    """(goal probability or None, latency_s, cost, error)."""
    body_fn = ts._request_body
    if not with_goal:
        def body_fn(t):                      # the same request without the goal question
            b = ts._request_body(t)
            b["questions"].pop("goal", None)
            return b
    from backend.brain.router import JEV_URL, OPENROUTER
    from backend.providers import gateway
    t0 = time.perf_counter()
    try:
        r = await gateway.get_http_client().post(
            JEV_URL, json=body_fn(text), timeout=ts._HTTP_TIMEOUT_S * 3,
            headers={"Authorization": f"Bearer {OPENROUTER.api_key}", **OPENROUTER.default_headers})
        r.raise_for_status()
        data = r.json()
        lat = time.perf_counter() - t0
        cost = (data.get("usage") or {}).get("cost")
        SPEND["calls"] += 1
        SPEND["usd"] += Decimal(str(cost or "0.0001"))
        p = float(data["answers"]["goal"]["noul"]) if with_goal else None
        return p, lat, cost, ""
    except Exception as e:
        SPEND["calls"] += 1
        SPEND["usd"] += Decimal("0.0001")
        return None, time.perf_counter() - t0, None, str(e)[:120]


def _f1(rows, thr):
    tp = sum(1 for r in rows if r["label"] and r["p"] >= thr)
    fp = sum(1 for r in rows if not r["label"] and r["p"] >= thr)
    fn = sum(1 for r in rows if r["label"] and r["p"] < thr)
    f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
    return f1, tp, fp, fn


def _choose(rows):
    best = (-1.0, 0.0)
    for k in range(5, 96):
        thr = k / 100
        f1 = _f1(rows, thr)[0]
        if f1 >= best[0]:
            best = (f1, thr)
    return best[1]


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--latency", type=int, default=40)
    ap.add_argument("--cap", type=float, default=0.10)
    a = ap.parse_args()
    cap = Decimal(str(a.cap))
    real = json.loads(Path(a.real).read_text(encoding="utf-8"))
    labels = json.loads(Path(a.labels).read_text(encoding="utf-8"))
    items = [{"source": "boundary", "id": f"c{i}", "group": g, "kind": k, "label": lab, "text": t}
             for i, (g, k, lab, t) in enumerate(CASES)]
    items += [{"source": "real", "id": r["sha"], "group": f"s:{r['session']}", "kind": "real",
               "label": bool(labels.get(r["sha"], False)), "text": r["user"],
               "summary_only": r.get("summary_only", False)} for r in real]

    from backend.providers import gateway
    await gateway.get_http_client().get("https://openrouter.ai/api/v1/models",
                                        params={"limit": 1}, timeout=6)   # warm the pool

    sem = asyncio.Semaphore(4)

    async def one(it):
        async with sem:
            if SPEND["usd"] > cap:
                it["error"] = "cap"
                return
            p, lat, cost, err = await _jev(it["text"])
            it.update(p=p, latency=lat, cost=cost, error=err)
    await asyncio.gather(*(one(it) for it in items))

    # latency: the same messages, with and without the goal question, alternating, one at a time
    lat_rows = []
    sample = [it for it in items if it["source"] == "real"][: a.latency]
    for i, it in enumerate(sample):
        order = (True, False) if i % 2 == 0 else (False, True)
        for wg in order:
            if SPEND["usd"] > cap:
                break
            _p, lat, _c, err = await _jev(it["text"], with_goal=wg)
            lat_rows.append({"with_goal": wg, "latency": lat, "error": err})
    await gateway.close_all()

    ok = [it for it in items if it.get("p") is not None]
    for it in ok:
        it["half"] = _half(it["group"])
        it["kw"] = bool(_KW.search(it["text"]))
    report = {"n": len(items), "scored": len(ok), "errors": len(items) - len(ok),
              "positives": sum(it["label"] for it in ok),
              "real_positives": sum(it["label"] for it in ok if it["source"] == "real"),
              "summary_only": sum(1 for it in ok if it.get("summary_only"))}
    halves = {h: [it for it in ok if it["half"] == h] for h in "AB"}
    report["halves"] = {h: {"n": len(v), "pos": sum(x["label"] for x in v)} for h, v in halves.items()}
    for tune, test in (("A", "B"), ("B", "A")):
        thr = _choose(halves[tune])
        f1, tp, fp, fn = _f1(halves[test], thr)
        report[f"tune_{tune}"] = {"threshold": thr, "held_out": test, "f1": round(f1, 3),
                                  "tp": tp, "fp": fp, "fn": fn}
    for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        by = {}
        for it in ok:
            key = "objective" if it["label"] else it["kind"]
            d = by.setdefault(key, [0, 0])
            d[0] += 1
            d[1] += it["p"] >= thr
        report[f"at_{thr}"] = {k: f"{v[1]}/{v[0]}" for k, v in sorted(by.items())}
    report["keyword"] = {
        "false_fires": sum(1 for it in ok if it["kw"] and not it["label"]),
        "hits_on_positives": sum(1 for it in ok if it["kw"] and it["label"]),
        "false_fire_ids": [it["id"] for it in ok if it["kw"] and not it["label"]]}
    lw = [r["latency"] for r in lat_rows if r["with_goal"] and not r["error"]]
    lo = [r["latency"] for r in lat_rows if not r["with_goal"] and not r["error"]]
    report["latency"] = {
        "with_goal": {"n": len(lw), "p50": _pct(lw, .5), "p90": _pct(lw, .9), "max": max(lw or [0]),
                      "over_0.8": sum(x > ts.DEADLINE_S for x in lw)},
        "without": {"n": len(lo), "p50": _pct(lo, .5), "p90": _pct(lo, .9), "max": max(lo or [0]),
                    "over_0.8": sum(x > ts.DEADLINE_S for x in lo)},
        "labelling_pass_p50": statistics.median([it["latency"] for it in ok]) if ok else None}
    report["spend_usd"] = float(SPEND["usd"])
    report["calls"] = SPEND["calls"]

    with open(a.out, "w", encoding="utf-8") as f:
        for it in items:
            row = {k: it.get(k) for k in ("source", "id", "group", "kind", "label", "p", "latency",
                                          "cost", "error", "half", "kw", "summary_only")}
            if it["source"] == "boundary":
                row["text"] = it["text"]
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.write(json.dumps({"report": report}, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    for it in sorted(ok, key=lambda x: -x["p"]):
        if it["label"] or it["p"] >= 0.3:
            tag = it["text"][:90] if it["source"] == "boundary" else f"real {it['id']}"
            print(f"{it['p']:.3f} {'POS' if it['label'] else 'neg'} {it['kind']:16} {tag}")


if __name__ == "__main__":
    asyncio.run(main())

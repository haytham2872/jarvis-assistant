"""Read-only: how today's long work actually runs on this host (docs/archive/GOALS-ROADMAP.md, G0).

    .venv/bin/python scripts/measure_long_work.py [--days 30] [--root /opt/jarvis]

Opens the runtime database with SQLite's URI `mode=ro` (never backend.storage.db.connect,
which would create tables and run migrations) and reads schedule.md, the chronos state
file and the Qdrant point counts. Writes nothing, makes no model call, prints no message
text: only counts, token figures, durations and costs.

Anything this host never recorded is printed as "unavailable", not estimated.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Pricing assumption (OpenRouter list price, verified 2026-09-21; CLAUDE.md "openrouter
# mode"). Used only to bound calls whose usage.cost is missing.
PRICE_DATE = "2026-09-21"
V41_PEAK = {"input": 0.30, "output": 1.20}     # $/Mtok; off-peak (about 79% of the week) is half


def pct(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))
    return vals[k]


def fmt(v, unit=""):
    return "unavailable" if v is None else f"{v:,}{unit}" if isinstance(v, int) else f"{v:.4g}{unit}"


def dist(values, unit=""):
    vals = [v for v in values if v is not None]
    if not vals:
        return "unavailable"
    return (f"n={len(vals)} p50={fmt(pct(vals, .5), unit)} p95={fmt(pct(vals, .95), unit)} "
            f"max={fmt(max(vals), unit)}")


def db_path() -> Path:
    override = os.getenv("JARVIS_DB_PATH", "")
    return Path(override) if override else ROOT / ".jarvis_runtime.db"


def _set_root(root: str | None) -> None:
    """--root: read another checkout (the script can then run from anywhere, e.g. /tmp)."""
    global ROOT
    if root:
        ROOT = Path(root).resolve()


def connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        sys.exit(f"No runtime database at {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error as exc:
        print(f"  (unavailable: {exc})")
        return None


def section(title):
    print(f"\n== {title} ==")


def llm_by_purpose(conn, since):
    section("LLM calls by purpose")
    calls = rows(conn, "SELECT * FROM llm_calls WHERE started_at >= ?", (since,))
    if calls is None:
        return []
    by = defaultdict(list)
    for c in calls:
        by[c["purpose"]].append(c)
    for purpose, cs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        outs = [c["output_tokens"] for c in cs]
        lengths = sum(1 for c in cs if c["finish_reason"] == "length")
        at_cap = sum(1 for c in cs if c["output_tokens"] and c["max_tokens"]
                     and c["output_tokens"] >= c["max_tokens"])
        no_cost = sum(1 for c in cs if c["cost"] is None)
        inp = sum(c["input_tokens"] or 0 for c in cs if c["cached_tokens"] is not None)
        cached = sum(c["cached_tokens"] or 0 for c in cs if c["cached_tokens"] is not None)
        hosts = Counter(c["host"] or "?" for c in cs).most_common(3)
        efforts = Counter(c["effort"] for c in cs).most_common()
        caps = Counter(c["max_tokens"] for c in cs).most_common(2)
        outcomes = Counter(c["outcome"] for c in cs).most_common()
        cost = sum(c["cost"] or 0 for c in cs)
        print(f"- {purpose}: {len(cs)} calls, ${cost:.4f} recorded"
              f" ({no_cost} with no cost recorded)")
        print(f"    output tokens {dist(outs)}; finish=length {lengths}; at max_tokens {at_cap};"
              f" max_tokens {caps}")
        print(f"    reasoning tokens {dist([c['reasoning_tokens'] for c in cs])}")
        print(f"    latency {dist([c['latency_ms'] for c in cs], 'ms')}")
        print(f"    cache rate {fmt(cached / inp if inp else None)}; effort {efforts};"
              f" outcome {outcomes}; hosts {hosts}")
    return calls


def background_headroom(calls):
    section("Background jobs: output per job and how each ended")
    if not calls:
        print("  unavailable")
        return
    bg = [c for c in calls if c["purpose"].startswith("background")]
    if not bg:
        print("  no background calls recorded in the window")
        return
    by_run = defaultdict(list)
    for c in bg:
        by_run[c["run_id"]].append(c)
    totals, ncalls, last_outcome, last_finish = [], [], Counter(), Counter()
    for run_id, cs in by_run.items():
        cs.sort(key=lambda c: c["started_at"])
        totals.append(sum(c["output_tokens"] or 0 for c in cs))
        ncalls.append(len(cs))
        last_outcome[cs[-1]["outcome"]] += 1
        last_finish[cs[-1]["finish_reason"]] += 1
    print(f"  jobs with calls: {len(by_run)} (run ids missing on "
          f"{sum(1 for c in bg if not c['run_id'])} calls)")
    print(f"  output tokens per job {dist(totals)}")
    print(f"  calls per job {dist(ncalls)}")
    print(f"  last call outcome {dict(last_outcome)}; last finish_reason {dict(last_finish)}")
    per_call = [c["output_tokens"] for c in bg if c["purpose"] == "background"]
    if per_call:
        hi = max(v for v in per_call if v is not None) if any(per_call) else None
        print(f"  ordinary background call output {dist(per_call)} against the 4,096 limit"
              + (f" (largest {hi / 4096:.0%} of it)" if hi else ""))


def jobs(conn, since):
    section("Durable background jobs (derivation_jobs)")
    js = rows(conn, "SELECT state, payload, created_at, updated_at, error, attempts"
                    " FROM derivation_jobs WHERE kind='background' AND created_at >= ?", (since,))
    if js is None:
        return
    print(f"  {len(js)} jobs; states {dict(Counter(j['state'] for j in js))}")
    kinds, durations, timeouts, has_result, dm, deep, notify, incomplete = (
        Counter(), [], 0, 0, 0, 0, 0, 0)
    fail_kinds = Counter()
    for j in js:
        try:
            p = json.loads(j["payload"] or "{}")
        except ValueError:
            continue
        kinds[p.get("kind", "agent")] += 1
        if p.get("finished") and p.get("started"):
            durations.append(round(float(p["finished"]) - float(p["started"])))
        summary = p.get("summary") or ""
        timeouts += summary.startswith("Timed out")
        incomplete += summary.startswith("Incomplete")
        has_result += "result" in p
        dm += bool(p.get("dm_sent"))
        deep += bool(p.get("deep"))
        notify += bool(p.get("notify"))
        if j["state"] == "failed":
            fail_kinds[(j["error"] or summary or "?")[:40]] += 1
    print(f"  kinds {dict(kinds)}; deep {deep}; notify {notify}")
    print(f"  duration {dist(durations, 's')}; timed out {timeouts}; incomplete {incomplete}")
    print(f"  payload keeps the full result: {has_result} of {len(js)} (G0 adds it);"
          f" dm_sent recorded: {dm}")
    if fail_kinds:
        print(f"  failure reasons (first 40 chars): {fail_kinds.most_common(6)}")


def runs_and_foreground(conn, since, calls):
    section("Runs by channel and state")
    rs = rows(conn, "SELECT run_id, channel, state, started_at, ended_at FROM runs"
                    " WHERE started_at >= ?", (since,))
    if rs is None:
        return
    by = defaultdict(Counter)
    for r in rs:
        by[r["channel"]][r["state"]] += 1
    for ch, c in sorted(by.items()):
        print(f"  {ch}: {dict(c)}")

    section("Foreground turns: tier, planner and plan (OpenRouter live loop)")
    if not calls:
        print("  unavailable")
        return
    fg = {r["run_id"]: r for r in rs if r["channel"] in ("local", "discord")}
    per = defaultdict(list)
    for c in calls:
        if c["run_id"] in fg:
            per[c["run_id"]].append(c)
    ops = rows(conn, "SELECT run_id, tool FROM operations WHERE started_at >= ?", (since,)) or []
    tools_per = defaultdict(list)
    for o in ops:
        tools_per[o["run_id"]].append(o["tool"])
    tiers, planner, plan_active, handoff = Counter(), Counter(), Counter(), 0
    tools_planned, tools_unplanned, cost_null, costs = [], [], 0, []
    for run_id, cs in per.items():
        chat = sorted((c for c in cs if c["purpose"] == "chat"), key=lambda c: c["started_at"])
        if not chat:
            continue
        tier = "thinking" if chat[0]["effort"] not in ("none", "default") else "standard"
        tiers[tier] += 1
        ran = any(c["purpose"] == "planner" for c in cs)
        active = any(c["plan_active"] for c in chat)
        planner[(tier, ran)] += 1
        plan_active[(tier, active)] += 1
        n_tools = len(tools_per.get(run_id, []))
        (tools_planned if active else tools_unplanned).append(n_tools)
        handoff += "continue_in_background" in tools_per.get(run_id, [])
        cost_null += any(c["cost"] is None for c in cs)
        costs.append(sum(c["cost"] or 0 for c in cs))
    total = sum(tiers.values())
    print(f"  turns with live-loop calls: {total}; first-call tier {dict(tiers)}")
    print(f"  planner ran (tier, ran): {dict(planner)}")
    print(f"  plan became active (tier, active): {dict(plan_active)}")
    print(f"  tool calls per turn with an active plan {dist(tools_planned)}; without {dist(tools_unplanned)}")
    print(f"  handoffs (continue_in_background): {handoff}")
    print(f"  turn cost {dist(costs, '$')}; turns with a call missing its cost: {cost_null}")
    print("  Jev complex probability per turn: unavailable (never stored before G0)")


def jev(calls):
    section("Jev (recorded from G0 on)")
    js = [c for c in (calls or []) if c["purpose"] == "jev"]
    if not js:
        print("  unavailable: no Jev rows yet (recording starts with the G0 deploy)")
        return
    lat = [c["latency_ms"] for c in js if c["finish_reason"] == "stop"]
    print(f"  {len(js)} calls; finish {dict(Counter(c['finish_reason'] for c in js))}")
    print(f"  answered latency {dist(lat, 'ms')}; p90 {fmt(pct(lat, .9), 'ms')};"
          f" answered after 800 ms {sum(1 for v in lat if v and v > 800)}")
    print(f"  cost ${sum(c['cost'] or 0 for c in js):.5f};"
          f" missing cost {sum(1 for c in js if c['cost'] is None and c['finish_reason'] == 'stop')}")


def deliveries(conn, since):
    section("Discord delivery receipts")
    di = rows(conn, "SELECT state FROM delivery_intents WHERE created_at >= ?", (since,))
    if di is not None:
        print(f"  intents {dict(Counter(d['state'] for d in di))}")
    dp = rows(conn, "SELECT p.state FROM delivery_parts p JOIN delivery_intents i"
                    " ON i.intent_id = p.intent_id WHERE i.created_at >= ?", (since,))
    if dp is not None:
        print(f"  parts {dict(Counter(d['state'] for d in dp))}")


def drafts_recall(conn, since):
    section("drafts/ and shared/ in the semantic index (D8)")
    try:
        import httpx
        host = os.getenv("QDRANT_HOST", "127.0.0.1")
        host = "127.0.0.1" if host == "localhost" else host
        base = f"http://{host}:{os.getenv('QDRANT_PORT', '6333')}/collections/jarvis_semantic/points/count"
        for folder in ("drafts", "shared"):
            body = {"exact": True, "filter": {"must": [
                {"key": "kind", "match": {"value": "vault"}},
                {"key": "folder", "match": {"value": folder}}]}}
            r = httpx.post(base, json=body, timeout=10)
            r.raise_for_status()
            print(f"  {folder}/ chunks indexed: {r.json()['result']['count']}")
    except Exception as exc:
        print(f"  chunk counts unavailable ({str(exc)[:80]})")
    ops = rows(conn, "SELECT tool, result_preview FROM operations WHERE started_at >= ?"
                     " AND tool IN ('memory_search', 'vault_read')", (since,))
    if ops is not None:
        ms = [o for o in ops if o["tool"] == "memory_search"]
        hit = sum(1 for o in ms if "drafts/" in (o["result_preview"] or ""))
        print(f"  memory_search calls {len(ms)}; with a drafts/ hit in the first 500 chars: {hit}")
        reads = sum(1 for o in ops if o["tool"] == "vault_read" and "drafts" in (o["result_preview"] or "")[:200])
        print(f"  vault_read of drafts (visible in preview): {reads}")
    print("  passive recall of drafts per turn: unavailable (not logged)")


def chronos_files():
    section("Scheduled and night work (schedule.md trail, chronos state)")
    sched = ROOT / "jarvis-vault" / "schedule.md"
    if sched.exists():
        trail = Counter()
        for line in sched.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("[done]") and "|" in line:
                what = line.split("|", 1)[1].strip().split(":", 1)[0]
                trail[what] += 1
        print(f"  [done] trail (last {sum(trail.values())} lines kept): {dict(trail)}")
    else:
        print("  schedule.md unavailable")
    state = ROOT / ".jarvis_chronos.json"
    if state.exists():
        s = json.loads(state.read_text(encoding="utf-8") or "{}")
        nr = s.get("night_results", []) or []
        print(f"  night results kept {len(nr)}; statuses {dict(Counter(r.get('status', 'pre-G0') for r in nr))}")
        print(f"  pending due runs {len(s.get('due_task_runs', []) or [])};"
              f" night task running {'yes' if s.get('night_running') else 'no'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--root", default=None)
    a = ap.parse_args()
    _set_root(a.root)
    since = time.time() - a.days * 86400
    print(f"Runtime database: {db_path()} (read-only); window {a.days:g} days")
    print(f"Pricing assumption: V4.1 Flash peak ${V41_PEAK['input']}/${V41_PEAK['output']} per Mtok"
          f" in/out, off-peak half (OpenRouter list price as of {PRICE_DATE})")
    conn = connect()
    calls = llm_by_purpose(conn, since)
    background_headroom(calls)
    jobs(conn, since)
    runs_and_foreground(conn, since, calls)
    jev(calls)
    deliveries(conn, since)
    drafts_recall(conn, since)
    chronos_files()


if __name__ == "__main__":
    main()

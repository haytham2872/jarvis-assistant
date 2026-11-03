"""Read-only: where the time and money of a Discord turn go on this host.

    .venv/bin/python scripts/measure_discord_speed.py [--days 14] [--root /opt/jarvis]

Opens the runtime database with SQLite's URI `mode=ro` (never backend.storage.db.connect,
which runs migrations). Writes nothing, makes no model call, prints no message text:
only durations, token counts and costs.

A Discord turn is split into stages from timestamps the backend already records:
  arrival     Discord's own send time (from the message id) -> ingress row
  queue       ingress -> run start (a message that arrived while JARVIS was busy)
  pre-model   run start -> first planner/chat call (Jev, embedding, retrieval, prompt)
  planner     planner calls
  model       chat / recovery / continuation calls (generation time)
  between     gaps between model calls (tool execution, mostly)
  post        last model call end -> run end (claim check, skill check, bookkeeping)
  delivery    run end -> last reply part confirmed by Discord (long-reply summary + send)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISCORD_EPOCH_MS = 1420070400000
MODEL_PURPOSES = {"chat", "recovery", "continuation"}
# List prices used ONLY to split recorded cost into input / cache / output (estimate).
PEAK_IN, CACHE_IN, PEAK_OUT = 0.30, 0.006, 1.20     # $/Mtok, V4.1 Flash, Together cache read


def pct(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))]


def dist(values, unit="s", nd=2):
    vals = [v for v in values if v is not None]
    if not vals:
        return "n=0"
    f = (lambda v: f"{v:.{nd}f}{unit}")
    return (f"n={len(vals)} p50={f(pct(vals, .5))} p90={f(pct(vals, .9))} "
            f"max={f(max(vals))} mean={f(sum(vals) / len(vals))}")


def connect(root: Path) -> sqlite3.Connection:
    path = Path(os.getenv("JARVIS_DB_PATH", "") or root / ".jarvis_runtime.db")
    if not path.exists():
        sys.exit(f"No runtime database at {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print(f"Runtime database: {path} (read-only)")
    return conn


def rows(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error as exc:
        print(f"  (unavailable: {exc})")
        return []


def section(title):
    print(f"\n== {title} ==")


def call_end(c):
    return c["started_at"] + (c["latency_ms"] or 0) / 1000.0


def discord_turns(conn, since):
    runs = rows(conn, "SELECT run_id, state, started_at, ended_at FROM runs"
                      " WHERE channel='discord' AND started_at >= ? ORDER BY started_at", (since,))
    calls = rows(conn, "SELECT * FROM llm_calls WHERE started_at >= ?", (since - 3600,))
    ops = rows(conn, "SELECT run_id, tool, outcome, started_at, duration_ms FROM operations"
                     " WHERE started_at >= ?", (since - 3600,))
    intents = rows(conn, "SELECT intent_id, created_at, sent_at, state FROM delivery_intents"
                         " WHERE created_at >= ? AND (intent_id LIKE 'reply:%'"
                         " OR intent_id LIKE 'answer:%')", (since,))
    parts = rows(conn, "SELECT p.intent_id, p.ordinal, p.sent_at FROM delivery_parts p"
                       " JOIN delivery_intents i ON i.intent_id=p.intent_id"
                       " WHERE i.created_at >= ?", (since,))
    ingress = {r["message_id"]: r["received_at"] for r in
               rows(conn, "SELECT message_id, received_at FROM ingress WHERE received_at >= ?",
                    (since - 3600,))}
    by_run, ops_by_run = defaultdict(list), defaultdict(list)
    for c in calls:
        by_run[c["run_id"]].append(c)
    for o in ops:
        ops_by_run[o["run_id"]].append(o)
    parts_by = defaultdict(list)
    for p in parts:
        parts_by[p["intent_id"]].append(p)
    intents.sort(key=lambda i: i["created_at"])
    return runs, calls, ops, by_run, ops_by_run, intents, parts_by, ingress


def stage_breakdown(runs, by_run, ops_by_run, intents, parts_by, ingress):
    section("Discord turns: where the wall time goes (completed turns)")
    st = defaultdict(list)
    tiers = Counter()
    tool_counts, call_counts = [], []
    used_intents = set()
    for r in runs:
        if r["state"] != "completed" or not r["ended_at"]:
            continue
        cs = sorted(by_run.get(r["run_id"], []), key=lambda c: c["started_at"])
        jev = [c for c in cs if c["purpose"] == "jev"]
        planner = [c for c in cs if c["purpose"] in ("planner", "replanner")]
        model = [c for c in cs if c["purpose"] in MODEL_PURPOSES]
        if not model:
            continue
        first = min([c["started_at"] for c in planner + model])
        last_end = max(call_end(c) for c in model)
        tier = "thinking" if model[0]["effort"] not in ("none", "default") else "standard"
        tiers[tier] += 1
        total = r["ended_at"] - r["started_at"]
        st["total"].append(total)
        st[f"total[{tier}]"].append(total)
        st["pre-model"].append(first - r["started_at"])
        st["jev"].append(sum((c["latency_ms"] or 0) for c in jev) / 1000 if jev else None)
        st["planner"].append(sum((c["latency_ms"] or 0) for c in planner) / 1000 if planner else 0)
        mt = sum((c["latency_ms"] or 0) for c in model) / 1000
        st["model"].append(mt)
        st[f"model[{tier}]"].append(mt)
        gaps = 0.0
        for a, b in zip(model, model[1:]):
            gaps += max(0.0, b["started_at"] - call_end(a))
        st["between calls"].append(gaps)
        st["post"].append(r["ended_at"] - last_end)
        others = [c for c in cs if c["purpose"] not in MODEL_PURPOSES | {"jev", "planner", "replanner"}]
        st["other llm in turn"].append(sum((c["latency_ms"] or 0) for c in others) / 1000)
        call_counts.append(len(model))
        tool_counts.append(len(ops_by_run.get(r["run_id"], [])))
        # The reply: first reply/answer intent created at or after the run ended.
        intent = next((i for i in intents if i["intent_id"] not in used_intents
                       and r["ended_at"] - 1 <= i["created_at"] <= r["ended_at"] + 180), None)
        if intent:
            used_intents.add(intent["intent_id"])
            ps = parts_by.get(intent["intent_id"], [])
            done = max((p["sent_at"] for p in ps if p["sent_at"]), default=None)
            if done:
                st["delivery"].append(done - r["ended_at"])
            msg_id = intent["intent_id"].split(":", 1)[1]
            if msg_id.isdigit():
                sent_ms = (int(msg_id) >> 22) + DISCORD_EPOCH_MS
                recv = ingress.get(msg_id)
                if recv:
                    st["arrival (discord->ingress)"].append(recv - sent_ms / 1000)
                    st["queue (ingress->run)"].append(r["started_at"] - recv)
                if done:
                    st["END TO END (sent->reply)"].append(done - sent_ms / 1000)
    print(f"  completed Discord turns with model calls: {sum(tiers.values())}; tier {dict(tiers)}")
    for k in ("END TO END (sent->reply)", "arrival (discord->ingress)", "queue (ingress->run)",
              "total", "total[standard]", "total[thinking]", "pre-model", "jev", "planner",
              "model", "model[standard]", "model[thinking]", "between calls", "post",
              "other llm in turn", "delivery"):
        print(f"  {k:28s} {dist(st.get(k, []))}")
    print(f"  model calls per turn {dist(call_counts, '', 0)}; tool calls per turn {dist(tool_counts, '', 0)}")
    tot = sum(st["total"]) or 1
    shares = {k: sum(v for v in st[k] if v) / tot for k in
              ("pre-model", "planner", "model", "between calls", "post")}
    print("  share of summed turn time: " + ", ".join(f"{k} {v:.0%}" for k, v in shares.items()))


def model_calls(calls, runs):
    section("Foreground model calls (Discord runs): size, cache, speed, host")
    ids = {r["run_id"] for r in runs}
    cs = [c for c in calls if c["run_id"] in ids and c["purpose"] in MODEL_PURPOSES]
    if not cs:
        print("  none")
        return
    for eff in sorted({c["effort"] for c in cs}):
        sub = [c for c in cs if c["effort"] == eff]
        inp = [c["input_tokens"] for c in sub]
        cache = [((c["cached_tokens"] or 0) / c["input_tokens"]) if c["input_tokens"] else None for c in sub]
        tps = [(c["output_tokens"] or 0) / (c["latency_ms"] / 1000) for c in sub
               if c["latency_ms"] and c["output_tokens"]]
        print(f"- effort {eff}: {len(sub)} calls")
        print(f"    input tokens {dist(inp, '', 0)}")
        print(f"    cache share  {dist(cache, '', 2)}")
        print(f"    output       {dist([c['output_tokens'] for c in sub], '', 0)}")
        print(f"    reasoning    {dist([c['reasoning_tokens'] for c in sub], '', 0)}")
        print(f"    latency      {dist([(c['latency_ms'] or 0) / 1000 for c in sub])}")
        print(f"    output tok/s {dist(tps, '', 1)}")
    section("Model call latency and throughput by host")
    by = defaultdict(list)
    for c in cs:
        by[c["host"] or "?"].append(c)
    for h, sub in sorted(by.items(), key=lambda kv: -len(kv[1])):
        tps = [(c["output_tokens"] or 0) / (c["latency_ms"] / 1000) for c in sub
               if c["latency_ms"] and c["output_tokens"] and c["output_tokens"] > 50]
        cache = [((c["cached_tokens"] or 0) / c["input_tokens"]) for c in sub if c["input_tokens"]]
        print(f"  {h:14s} {len(sub):4d} calls; latency {dist([(c['latency_ms'] or 0)/1000 for c in sub])};"
              f" tok/s p50 {pct(tps, .5) or 0:.0f}; cache p50 {pct(cache, .5) or 0:.2f}")


def first_call_cache(calls, runs):
    """The first model call of each turn is where a broken prefix shows: later calls in the
    same turn re-read what the first one just cached."""
    section("Prompt cache on each turn's FIRST model call (Discord)")
    ids = {r["run_id"] for r in runs}
    firsts = {}
    for c in calls:
        if c["run_id"] in ids and c["purpose"] == "chat":
            if c["run_id"] not in firsts or c["started_at"] < firsts[c["run_id"]]["started_at"]:
                firsts[c["run_id"]] = c
    groups = defaultdict(list)
    for c in firsts.values():
        if not c["input_tokens"]:
            continue
        size = "<25k" if c["input_tokens"] < 25000 else "25-35k" if c["input_tokens"] < 35000 else ">35k"
        key = f"plan={'yes' if c['plan_active'] else 'no '} input {size}"
        groups[key].append(c)
    for k, cs in sorted(groups.items()):
        share = [(c["cached_tokens"] or 0) / c["input_tokens"] for c in cs]
        unc = [c["input_tokens"] - (c["cached_tokens"] or 0) for c in cs]
        print(f"  {k}: {dist(share, '', 2)} | uncached tokens {dist(unc, '', 0)}")
    later = [c for c in calls if c["run_id"] in ids and c["purpose"] == "chat"
             and c is not firsts.get(c["run_id"]) and c["input_tokens"]]
    print(f"  later calls in the same turn: cache "
          f"{dist([(c['cached_tokens'] or 0) / c['input_tokens'] for c in later], '', 2)}")


def network():
    """Connection setup from this host: a new connection vs one kept open (HEAD, no key)."""
    import ssl, urllib.request, http.client
    section("Network: new connection vs open connection (HEAD requests, free)")
    for host in ("api.search.tinyfish.ai", "api.fetch.tinyfish.ai", "openrouter.ai"):
        fresh = []
        for _ in range(3):
            t = time.perf_counter()
            try:
                c = http.client.HTTPSConnection(host, timeout=10, context=ssl.create_default_context())
                c.request("HEAD", "/"); c.getresponse().read(); c.close()
                fresh.append(time.perf_counter() - t)
            except Exception as exc:
                print(f"  {host}: failed ({exc})")
                break
        warm = []
        try:
            c = http.client.HTTPSConnection(host, timeout=10, context=ssl.create_default_context())
            c.request("HEAD", "/"); c.getresponse().read()
            for _ in range(3):
                t = time.perf_counter()
                c.request("HEAD", "/"); c.getresponse().read()
                warm.append(time.perf_counter() - t)
            c.close()
        except Exception:
            pass
        print(f"  {host:24s} new {dist(fresh)} | open {dist(warm)}")


def tools(ops, runs):
    section("Tool calls in Discord turns: duration by tool")
    ids = {r["run_id"] for r in runs}
    by = defaultdict(list)
    for o in ops:
        if o["run_id"] in ids:
            by[o["tool"]].append((o["duration_ms"] or 0) / 1000)
    for t, ds in sorted(by.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {t:24s} {dist(ds)} total={sum(ds):.0f}s")


def spending(calls, since, days):
    section("Spending by purpose (recorded cost; whole host, all channels)")
    by = defaultdict(list)
    for c in calls:
        if c["started_at"] >= since:
            by[c["purpose"]].append(c)
    grand = sum(c["cost"] or 0 for c in calls if c["started_at"] >= since) or 1e-9
    for p, cs in sorted(by.items(), key=lambda kv: -sum(c["cost"] or 0 for c in kv[1])):
        cost = sum(c["cost"] or 0 for c in cs)
        inp = sum(c["input_tokens"] or 0 for c in cs)
        cached = sum(c["cached_tokens"] or 0 for c in cs)
        out = sum(c["output_tokens"] or 0 for c in cs)
        est_in = (inp - cached) * PEAK_IN / 1e6
        est_cache = cached * CACHE_IN / 1e6
        est_out = out * PEAK_OUT / 1e6
        est = est_in + est_cache + est_out or 1e-9
        print(f"  {p:30s} {len(cs):5d} calls ${cost:8.4f} ({cost / grand:5.1%}) ${cost / days:.4f}/day"
              f"  in {inp / max(len(cs), 1):7.0f}/call cache {cached / inp if inp else 0:4.0%}"
              f"  est split in/cache/out {est_in / est:.0%}/{est_cache / est:.0%}/{est_out / est:.0%}")
    print(f"  TOTAL ${grand:.4f} over {days:g} days = ${grand / days:.4f}/day")


def per_turn_cost(by_run, runs):
    section("Cost per completed Discord turn")
    costs, calls_n = [], []
    for r in runs:
        if r["state"] != "completed":
            continue
        cs = by_run.get(r["run_id"], [])
        if cs:
            costs.append(sum(c["cost"] or 0 for c in cs))
    print(f"  {dist(costs, '$', 4)}; total ${sum(costs):.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--root", default=None)
    ap.add_argument("--no-net", action="store_true", help="skip the connection-setup check")
    a = ap.parse_args()
    root = Path(a.root).resolve() if a.root else ROOT
    since = time.time() - a.days * 86400
    conn = connect(root)
    print(f"Window: last {a.days:g} days")
    runs, calls, ops, by_run, ops_by_run, intents, parts_by, ingress = discord_turns(conn, since)
    print(f"Discord runs: {len(runs)} ({dict(Counter(r['state'] for r in runs))})")
    stage_breakdown(runs, by_run, ops_by_run, intents, parts_by, ingress)
    per_turn_cost(by_run, runs)
    model_calls(calls, runs)
    first_call_cache(calls, runs)
    tools(ops, runs)
    spending(calls, since, a.days)
    if not a.no_net:
        network()


if __name__ == "__main__":
    main()

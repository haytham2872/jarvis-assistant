"""Read-only: what JARVIS's memory layers hold, what reaches a turn, and what the logs can say.

    .venv/bin/python scripts/measure_memory_baseline.py [--days 30] [--root /opt/jarvis]
        [--journal FILE | --no-journal] [--qdrant http://127.0.0.1:6333]

Baseline for the Hermes-inspired memory work (HERMES-ANALYSIS.md). Writes nothing, loads no
model, makes no model call and prints no message text: only counts, sizes and durations.

Sources, all read-only:
  - the runtime database, opened with SQLite's URI mode=ro (never backend.storage.db.connect,
    which runs migrations);
  - Qdrant's REST API on the loopback port (collection listing, count and scroll only);
  - the systemd journal (`journalctl -u jarvis -o short-unix`), for process restarts, /new and
    the per-turn "turn timing:" lines (logged since 2026-10-09);
  - a few vault files, for their sizes only.

What the logs cannot say is printed as such. In particular the content and size of what
passive recall injected on a turn is not recorded anywhere; scripts/measure_recall_replay.py
approximates it by replaying recall against the live stores.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY_TURNS = 40           # main._MAX_HISTORY_TURNS
BRIDGE_LINES = 20            # history_bridge._MAX_LINES
SESSION_GAP_S = 2 * 3600     # threads.SESSION_GAP_THRESHOLD default
CHARS_PER_TOKEN = 3.82       # context.budget.CHARS_PER_TOKEN (worst case measured)
DISCORD_USER_ONLY_INGEST = dt.datetime(2026, 9, 26, 15, 37).timestamp()   # commit 193c6fa
MODEL_PURPOSES = {"chat", "recovery", "continuation"}
MEMORY_TOOLS = ("memory_search", "memory_timeline", "correct_memory", "vault_read")
WRITE_TOOLS = ("vault_write", "vault_write_queue", "standing_instruction", "person_profile",
               "skill", "correct_memory")


# ---------------------------------------------------------------- formatting helpers

def pct(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))]


def dist(values, unit="", nd=0):
    vals = [v for v in values if v is not None]
    if not vals:
        return "n=0"
    f = (lambda v: f"{v:,.{nd}f}{unit}")
    return (f"n={len(vals)} p50={f(pct(vals, .5))} p90={f(pct(vals, .9))} "
            f"max={f(max(vals))} mean={f(sum(vals) / len(vals))}")


def share(n, d):
    return f"{n}/{d} ({n / d:.0%})" if d else f"{n}/0"


def section(title):
    print(f"\n== {title} ==")


def day(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- sources

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


_LINE = re.compile(r"^(\d+\.\d+)\s+\S+\s+([^\[\s]+)\[(\d+)\]:\s?(.*)$")
_PYLOG = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) (\S+): (.*)$")


_KEEP = ("turn timing:", "context: ", "rerank:", "Mem0 ", "force_new_session(discord)")


def read_journal(days: float, path: str | None, unit: str = "jarvis"):
    """Backend log lines, aggregated as they stream past so a long journal never sits in
    memory: {"procs": {pid: [first_ts, last_ts]}, "lines": [(ts, message)] for the few
    messages this script reads, "first", "last", "n"}. None when the journal cannot be
    read (the caller says so instead of guessing)."""
    if path:
        try:
            src = open(path, encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  journal file unreadable: {exc}")
            return None
        lines = src
    else:
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", unit, "-o", "short-unix", "--no-pager",
                 "--since", f"{int(days)} days ago"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        except (OSError, FileNotFoundError) as exc:
            print(f"  journalctl unavailable: {exc}")
            return None
        lines = proc.stdout
    out = {"procs": {}, "lines": [], "first": None, "last": None, "n": 0}
    for raw in lines:
        m = _LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        ts, pid, msg = float(m.group(1)), int(m.group(3)), m.group(4)
        pm = _PYLOG.match(msg)
        if not pm:
            continue            # systemd, docker compose and uvicorn lines: not the backend's
        out["n"] += 1
        out["first"] = ts if out["first"] is None else out["first"]
        out["last"] = ts
        p = out["procs"].setdefault(pid, [ts, ts])
        p[1] = ts
        text = pm.group(3)
        if any(k in text for k in _KEEP):
            out["lines"].append((ts, text))
    if path:
        src.close()
    else:
        proc.wait()
        if proc.returncode not in (0, None) and not out["n"]:
            err = (proc.stderr.read() or "").strip().splitlines()[-1:] if proc.stderr else []
            print(f"  journalctl returned {proc.returncode}: {' '.join(err)}"
                  " (try running with sudo, or pass --journal FILE)")
            return None
    return out


class Qdrant:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["result"]

    def collections(self):
        return [c["name"] for c in self._req("GET", "/collections")["collections"]]

    def count(self, col, flt=None):
        body = {"exact": True}
        if flt:
            body["filter"] = flt
        return int(self._req("POST", f"/collections/{col}/points/count", body)["count"])

    def scroll(self, col, fields, flt=None, limit=256):
        offset = None
        while True:
            body = {"limit": limit, "with_payload": fields, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            if flt:
                body["filter"] = flt
            res = self._req("POST", f"/collections/{col}/points/scroll", body)
            for p in res.get("points", []):
                yield p.get("payload") or {}
            offset = res.get("next_page_offset")
            if offset is None:
                break


# ---------------------------------------------------------------- exact-token features

_STOP_CAPS = {"I", "A", "OK", "AI", "TV", "PC", "OS", "AM", "PM", "MY", "ME", "NO", "SO",
              "IT", "IS", "IN", "ON", "OR", "AN", "AT", "BE", "DO", "GO", "HE", "IF", "OF",
              "TO", "UP", "US", "WE"}
_FEATURES = {
    "number": re.compile(r"\d"),
    "money_or_pct": re.compile(r"[$€£]\s?\d|\d\s?(%|€|\$|eur|usd)", re.I),
    "caps_token": re.compile(r"\b[A-Z][A-Z0-9]{1,5}\b"),
    "url": re.compile(r"https?://|www\.", re.I),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "dotted_id": re.compile(r"\b\w+[._/-]\w+[._/-]?\w*\.(md|py|json|csv|pdf|txt)\b|\bv?\d+\.\d+(\.\d+)?\b", re.I),
    "date": re.compile(r"\b\d{4}-\d\d-\d\d\b|\b\d{1,2}/\d{1,2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b", re.I),
    "quoted": re.compile(r"[\"“«][^\"”»]{3,}[\"”»]"),
}


def features(text: str) -> set[str]:
    found = set()
    for name, rx in _FEATURES.items():
        if name == "caps_token":
            if any(t not in _STOP_CAPS for t in rx.findall(text or "")):
                found.add(name)
        elif rx.search(text or ""):
            found.add(name)
    return found


def feature_report(texts, label):
    texts = [t for t in texts if t and t.strip()]
    n = len(texts)
    counts = Counter()
    anyc = 0
    for t in texts:
        f = features(t)
        counts.update(f)
        if f - {"quoted"}:
            anyc += 1
    print(f"  {label}: {n} texts; any exact token (number/money/caps/url/email/id/date): {share(anyc, n)}")
    print("    " + ", ".join(f"{k} {counts[k] / n:.0%}" if n else f"{k} -" for k in _FEATURES))


# ---------------------------------------------------------------- 1. canonical exchanges

def exchanges_store(conn, since):
    section("1. Canonical exchanges (SQLite) -- what an FTS5 index would cover")
    ex = rows(conn, "SELECT id, session_id, channel, timestamp, length(user_text) AS ul,"
                    " length(assistant_text) AS al, length(summary) AS sl, high FROM exchanges")
    if not ex:
        print("  exchanges table is empty")
        return []
    by_ch = Counter(e["channel"] for e in ex)
    print(f"  rows: {len(ex)} by channel {dict(by_ch)}")
    real = [e for e in ex if e["channel"] != "canary"]
    if not real:
        print("  only canary rows")
        return []
    print(f"  date range (excl. canary): {day(min(e['timestamp'] for e in real))} -> "
          f"{day(max(e['timestamp'] for e in real))}")
    full = [e for e in real if e["ul"] and e["al"]]
    summary_only = [e for e in real if not e["ul"] and e["sl"]]
    print(f"  full text rows: {share(len(full), len(real))}; summary-only (pre-A02 low-significance"
          f" backfill): {len(summary_only)}")
    print(f"  user chars      {dist([e['ul'] for e in full])}")
    print(f"  assistant chars {dist([e['al'] for e in full])}")
    total = sum((e["ul"] or 0) + (e["al"] or 0) + (e["sl"] or 0) for e in real)
    print(f"  total text: {total:,} chars (~{total / CHARS_PER_TOKEN / 1000:,.0f}K tokens) -- an FTS5"
          f" index over it is roughly the same size again")
    recent = [e for e in real if e["timestamp"] >= since]
    print(f"  in window: {len(recent)} exchanges; by channel {dict(Counter(e['channel'] for e in recent))}")
    sess = defaultdict(int)
    for e in real:
        sess[(e["channel"], e["session_id"])] += 1
    disc = [n for (ch, _), n in sess.items() if ch == "discord"]
    print(f"  discord sessions: {len(disc)}; exchanges per session {dist(disc)}")
    # Exact-token density of what Sam writes (texts read in place, never printed).
    texts = [r["user_text"] for r in rows(
        conn, "SELECT user_text FROM exchanges WHERE channel='discord' AND timestamp >= ?"
              " AND length(user_text) > 0", (since,))]
    feature_report(texts, "discord user messages in window")
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61 remove_diacritics 2')")
        fts5 = "yes"
    except sqlite3.Error as exc:
        fts5 = f"NO ({exc})"
    try:
        probe.execute("CREATE VIRTUAL TABLE t3 USING fts5(x, tokenize='trigram')")
        tri = "yes"
    except sqlite3.Error as exc:
        tri = f"no ({exc})"
    print(f"  this Python's SQLite {sqlite3.sqlite_version}: FTS5 {fts5}; trigram tokenizer {tri}")
    return real


# ---------------------------------------------------------------- 2. journal

def journal_summary(jl, since):
    section("2. Journal: processes, /new, per-turn timing lines")
    if jl is None:
        print("  journal not available -> restarts, /new, deep share and rerank cost CANNOT be said here")
        return None
    if not jl["n"]:
        print("  journal readable but no backend lines in the window (not in the adm or"
              " systemd-journal group? run with sudo, or pass --journal FILE)")
        return None
    print(f"  coverage: {day(jl['first'])} -> {day(jl['last'])} ({jl['n']:,} backend log lines)")
    procs = jl["procs"]
    starts = sorted(v[0] for v in procs.values())
    span_days = max((jl["last"] - jl["first"]) / 86400, 1e-9)
    print(f"  backend processes seen: {len(procs)} (restarts ~{max(len(procs) - 1, 0) / span_days:.1f}/day)")
    lifetimes = [(v[1] - v[0]) / 3600 for v in procs.values()]
    print(f"  process lifetime (h, last line - first line) {dist(lifetimes, 'h', 1)}")
    news = [ts for ts, msg in jl["lines"] if "force_new_session(discord): closing session" in msg]
    print(f"  /new on Discord with an active session: {len(news)}")
    # The first process in the window was already running when the window opened, so its
    # first line is not a start; every later process logs from its own startup.
    return {"starts": starts[1:], "news": sorted(news), "first": jl["first"], "last": jl["last"]}


_TIMING = re.compile(
    r"turn timing: model call at ([\d.]+)s \| embed ([\d.]+)s, retrieval ([\d.]+)s(?: \[(.*?)\])?, "
    r"jev wait ([\d.]+)s \((\w+)\), assembly ([\d.]+)s \| deep=(yes|no) tier=(\w+)"
    r"(?: \| rerank (\d+) calls, (\d+) pairs, ([\d.]+)s thread time)?")
_SRC = re.compile(r"(\S+) ([\d.]+)s")
_OMIT = re.compile(r"context: \d+ source\(s\) omitted: (.*)$")
_OMIT_ITEM = re.compile(r"([^,\s(]+)\(([^)]+)\)")
_MISS = re.compile(r"context: \d+ source\(s\) missed their deadline this turn: (.*)$")
_MISS_ITEM = re.compile(r"([^,\s(]+) \(([\d.]+)s\)")


def timing_lines(jl):
    section("3. Passive recall as the logs record it (turn timing lines)")
    if jl is None:
        print("  journal not available -- cannot say")
        return []
    turns = []
    omitted, missed = Counter(), Counter()
    rerank_load = Counter()
    mem0_err = Counter()
    for ts, msg in jl["lines"]:
        m = _TIMING.search(msg)
        if m:
            g = m.groups()
            turns.append({
                "ts": ts, "model_at": float(g[0]), "embed": float(g[1]), "retrieval": float(g[2]),
                "slow": [(s, float(v)) for s, v in _SRC.findall(g[3] or "")],
                "jev_wait": float(g[4]), "jev": g[5], "assembly": float(g[6]), "deep": g[7] == "yes",
                "tier": g[8], "rr_calls": int(g[9]) if g[9] else 0, "rr_pairs": int(g[10]) if g[10] else 0,
                "rr_secs": float(g[11]) if g[11] else 0.0})
            continue
        m = _OMIT.search(msg)
        if m:
            omitted.update(f"{s} ({r})" for s, r in _OMIT_ITEM.findall(m.group(1)))
            continue
        m = _MISS.search(msg)
        if m:
            missed.update(s for s, _ in _MISS_ITEM.findall(m.group(1)))
            continue
        if msg.startswith("rerank:"):
            rerank_load["loaded" if " loaded" in msg else "unavailable" if "unavailable" in msg
                        else "scoring failed" if "scoring failed" in msg else "other"] += 1
        if msg.startswith("Mem0 recall failed"):
            mem0_err["recall failed"] += 1
        elif msg.startswith("Mem0 ingest failed"):
            mem0_err["ingest failed"] += 1
        elif msg.startswith("Mem0 init failed"):
            mem0_err["init failed"] += 1
    if not turns:
        print("  no 'turn timing:' lines in the journal window (logged only since 2026-10-09)")
    else:
        deep = [t for t in turns if t["deep"]]
        print(f"  turns with a timing line: {len(turns)} ({day(turns[0]['ts'])} -> {day(turns[-1]['ts'])})")
        print(f"  deep turns (facts + vault passage + cross-session exchanges + letter + topic): "
              f"{share(len(deep), len(turns))}")
        print(f"  jev: {dict(Counter(t['jev'] for t in turns))}; tier: {dict(Counter(t['tier'] for t in turns))}")
        for label, sub in (("deep", deep), ("not deep", [t for t in turns if not t["deep"]])):
            print(f"  [{label}] model call at {dist([t['model_at'] for t in sub], 's', 2)}")
            print(f"  [{label}] retrieval     {dist([t['retrieval'] for t in sub], 's', 2)}")
        slow = Counter(s for t in turns for s, v in t["slow"] if v >= 0.25)
        print(f"  sources >= 0.25s (count of turns): {dict(slow.most_common(8))}")
        rr = [t for t in deep if t["rr_calls"]]
        print(f"  deep turns that ran the cross-encoder: {share(len(rr), len(deep))}"
              f" (0 means the model is not on this host -> cosine only)")
        if rr:
            print(f"    pairs per deep turn {dist([t['rr_pairs'] for t in rr])}")
            print(f"    cross-encoder thread time {dist([t['rr_secs'] for t in rr], 's', 2)}")
    print(f"  rerank load lines: {dict(rerank_load) or 'none in window'}")
    print(f"  context sources dropped by the budget: {dict(omitted.most_common(10)) or 'none'}")
    print(f"  sources that missed their deadline: {dict(missed.most_common(10)) or 'none'}")
    print(f"  mem0 errors: {dict(mem0_err) or 'none'}")
    print("  NOT LOGGED: which recall blocks (facts / vault passage / exchanges / topic) were injected"
          " and how large -> scripts/measure_recall_replay.py")
    return turns


# ---------------------------------------------------------------- 4. live history window

def history_window(real_exchanges, jinfo, since):
    section("4. Live history window: the 40-turn bridge and restart amnesia (Discord)")
    if jinfo is None:
        print("  journal not available -> process restarts unknown -> cannot say how many turns hit"
              " the bridge or started with an empty history")
        return {}
    bounds = sorted([(t, "restart") for t in jinfo["starts"]] + [(t, "new") for t in jinfo["news"]])
    if not bounds:
        print("  no restart or /new inside the journal window, so the history state at the window's"
              " start is unknown -> widen --days")
        return {}
    # Before the first boundary the live history's contents are unknown; start there.
    t0 = bounds[0][0]
    ex = sorted((e for e in real_exchanges if e["channel"] == "discord"
                 and t0 < e["timestamp"] <= jinfo["last"] + 60),
                key=lambda e: e["timestamp"])
    if not ex:
        print("  no Discord exchanges after the first restart or /new in the journal window")
        return {}
    print(f"  analysed from the first boundary ({day(t0)}, {bounds[0][1]}); boundaries:"
          f" {dict(Counter(k for _, k in bounds))}")

    def boundary_between(a, b):
        """The kinds of boundaries strictly between timestamps a and b."""
        return {k for t, k in bounds if a < t <= b}

    seg_idx = 0          # turns since the last restart or /new
    since_new = 0        # turns since the last /new (what the window would hold with no restarts)
    prev = None
    window, bridge, hist_chars, lost = [], [], [], []
    after_restart_same_session = 0
    cold_turns = 0
    by_id = {}
    for e in ex:
        if prev is not None:
            kinds = boundary_between(prev["timestamp"], e["timestamp"])
            if "new" in kinds:
                seg_idx, since_new = 0, 0
            elif "restart" in kinds:
                if e["timestamp"] - prev["timestamp"] < SESSION_GAP_S:
                    after_restart_same_session += 1
                seg_idx = 0
        else:
            seg_idx = since_new = 0   # first exchange after the first boundary
        w = min(seg_idx, HISTORY_TURNS)
        window.append(w)
        bridge.append(min(max(0, seg_idx - HISTORY_TURNS), BRIDGE_LINES))
        lost.append(min(since_new, HISTORY_TURNS) - w)
        if seg_idx == 0:
            cold_turns += 1
        by_id[e["id"]] = w
        seg_idx += 1
        since_new += 1
        prev = e
    # History size actually sent: previous `w` exchanges of the same segment.
    seg = []
    for e in ex:
        w = by_id[e["id"]]
        if w == 0:
            seg = []
        prior = seg[-w:] if w else []
        hist_chars.append(sum((p["ul"] or 0) + (p["al"] or 0) for p in prior))
        seg.append(e)
    n = len(ex)
    print(f"  Discord turns (exchanges) in the journal window: {n}")
    print(f"  prior turns in the live window {dist(window)}")
    print(f"  turns with a full 40-turn window: {share(sum(1 for w in window if w >= HISTORY_TURNS), n)}")
    print(f"  turns where the history bridge was non-empty: {share(sum(1 for b in bridge if b), n)}"
          f"; bridge lines {dist([b for b in bridge if b])}")
    print(f"  turns that started with NO history (first after a restart or /new): {share(cold_turns, n)}")
    print(f"  ...of which the conversation was still going (< 2h since the previous exchange) when"
          f" the process restarted: {after_restart_same_session}")
    print(f"  turns that saw fewer prior turns than they would have without restarts:"
          f" {share(sum(1 for l in lost if l > 0), n)}; turns lost {dist([l for l in lost if l > 0])}")
    print(f"  history sent (est. tokens) {dist([c / CHARS_PER_TOKEN for c in hist_chars])}")
    return by_id


# ---------------------------------------------------------------- 5. context size per call

def llm_context(conn, since, days, turns, by_id_window, real_exchanges):
    section("5. Context size per LLM call, by purpose (all channels)")
    calls = rows(conn, "SELECT * FROM llm_calls WHERE started_at >= ?", (since,))
    if not calls:
        print("  no llm_calls rows in window")
        return
    by = defaultdict(list)
    for c in calls:
        by[c["purpose"]].append(c)
    print(f"  {'purpose':24s} {'calls':>6s} {'input p50':>10s} {'p90':>8s} {'max':>8s} {'cached':>7s}"
          f" {'out p50':>8s} {'$/day':>8s}")
    for p, cs in sorted(by.items(), key=lambda kv: -sum(c["input_tokens"] or 0 for c in kv[1])):
        inp = [c["input_tokens"] for c in cs if c["input_tokens"]]
        tot_in = sum(inp)
        cached = sum(c["cached_tokens"] or 0 for c in cs)
        cost = sum(c["cost"] or 0 for c in cs)
        print(f"  {p:24s} {len(cs):6d} {pct(inp, .5) or 0:10,d} {pct(inp, .9) or 0:8,d}"
              f" {max(inp) if inp else 0:8,d} {cached / tot_in if tot_in else 0:7.0%}"
              f" {pct([c['output_tokens'] for c in cs], .5) or 0:8,d} {cost / days:8.4f}")
    runs = rows(conn, "SELECT run_id, channel, state, started_at, ended_at FROM runs"
                      " WHERE started_at >= ? AND channel='discord'", (since,))
    rmap = {r["run_id"]: r for r in runs}
    chat = [c for c in calls if c["purpose"] == "chat" and c["run_id"] in rmap]
    firsts = {}
    for c in chat:
        if c["run_id"] not in firsts or c["started_at"] < firsts[c["run_id"]]["started_at"]:
            firsts[c["run_id"]] = c
    later = [c for c in chat if c is not firsts.get(c["run_id"])]
    print(f"\n  Discord chat calls: first call of a turn input {dist([c['input_tokens'] for c in firsts.values()])}")
    print(f"  Discord chat calls: later calls in a turn input {dist([c['input_tokens'] for c in later])}")
    # Join each turn to its timing line (deep or not) and its exchange (history depth).
    tl = sorted(turns or [], key=lambda t: t["ts"])
    ex = sorted((e for e in real_exchanges if e["channel"] == "discord"), key=lambda e: e["timestamp"])
    groups = defaultdict(list)
    for rid, c in firsts.items():
        r = rmap[rid]
        end = (r["ended_at"] or r["started_at"] + 600) + 15
        t = next((t for t in tl if r["started_at"] - 1 <= t["ts"] <= end), None)
        e = next((e for e in ex if r["started_at"] - 1 <= e["timestamp"] <= end), None)
        w = by_id_window.get(e["id"]) if e and by_id_window else None
        deep = None if t is None else t["deep"]
        groups[("deep" if deep else "not deep" if deep is not None else "unknown",
                "?" if w is None else "0" if w == 0 else "1-9" if w < 10 else "10-39" if w < 40 else "40")
               ].append(c["input_tokens"])
    print("  first-call input tokens by (deep, prior turns in window):")
    for k in sorted(groups):
        print(f"    deep={k[0]:9s} window={k[1]:6s} {dist(groups[k])}")
    # Research-shaped turns: many tool calls in one turn, all in the main context.
    ops = rows(conn, "SELECT run_id, tool FROM operations WHERE started_at >= ?", (since,))
    tools_by = defaultdict(list)
    for o in ops:
        if o["run_id"] in rmap:
            tools_by[o["run_id"]].append(o["tool"])
    calls_by = defaultdict(list)
    for c in chat:
        calls_by[c["run_id"]].append(c)
    n_turns = len(firsts)
    for lo in (3, 6, 10):
        sub = [rid for rid in firsts if len(tools_by.get(rid, [])) >= lo]
        if not sub:
            print(f"  turns with >= {lo} tool calls: 0/{n_turns}")
            continue
        last_in = [max(c["input_tokens"] or 0 for c in calls_by[rid]) for rid in sub]
        sum_in = [sum(c["input_tokens"] or 0 for c in calls_by[rid]) for rid in sub]
        cache = [sum(c["cached_tokens"] or 0 for c in calls_by[rid]) / max(1, sum(c["input_tokens"] or 0 for c in calls_by[rid])) for rid in sub]
        cost = [sum(c["cost"] or 0 for c in calls_by[rid]) for rid in sub]
        wall = [(rmap[rid]["ended_at"] or 0) - rmap[rid]["started_at"] for rid in sub if rmap[rid]["ended_at"]]
        web = [sum(1 for t in tools_by[rid] if t in ("web_browse", "fetch_page", "web_research")) for rid in sub]
        print(f"  turns with >= {lo} tool calls: {share(len(sub), n_turns)}")
        print(f"    largest call input {dist(last_in)}; summed input {dist(sum_in)}")
        print(f"    cached share {dist(cache, '', 2)}; cost {dist(cost, '$', 4)}; wall {dist(wall, 's', 1)};"
              f" web calls {dist(web)}")


# ---------------------------------------------------------------- 6. mem0 and the semantic index

def qdrant_stores(qd: Qdrant, conn):
    section("6. mem0 facts and the semantic index (Qdrant, read-only)")
    try:
        cols = qd.collections()
    except Exception as exc:
        print(f"  Qdrant unreachable at {qd.base}: {exc}")
        return
    print(f"  collections: {sorted(cols)}")
    col = "mem0" if "mem0" in cols else next(
        (c for c in cols if c.startswith("mem0") and c not in ("mem0migrations", "mem0_entities")), None)
    if col:
        facts = list(qd.scroll(col, ["user_id", "attributed_to", "consolidated", "created_at", "data"]))
        users = Counter(f.get("user_id") for f in facts)
        print(f"  mem0 facts ('{col}'): {len(facts)} across {len(users)} user id(s)"
              f" (largest holds {users.most_common(1)[0][1] if users else 0})")
        attr = lambda f: f.get("attributed_to") or "(none)"
        cons = lambda f: "consolidated" if f.get("consolidated") else "unconsolidated"
        cross = Counter((attr(f), cons(f)) for f in facts)
        for a in sorted({attr(f) for f in facts}):
            print(f"    attributed_to={a:10s} consolidated {cross[(a, 'consolidated')]:5d}"
                  f"  unconsolidated {cross[(a, 'unconsolidated')]:5d}")
        print(f"    fact length (chars) {dist([len(str(f.get('data') or '')) for f in facts])}")

        def ts_of(f):
            try:
                return dt.datetime.fromisoformat(str(f.get("created_at")).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                return None
        months = defaultdict(Counter)
        before, after = Counter(), Counter()
        for f in facts:
            t = ts_of(f)
            if t is None:
                months["(no date)"][attr(f)] += 1
                continue
            months[dt.datetime.fromtimestamp(t).strftime("%Y-%m")][attr(f)] += 1
            (before if t < DISCORD_USER_ONLY_INGEST else after)[attr(f)] += 1
        print("    created per month by attribution: " + "; ".join(
            f"{m} {dict(c)}" for m, c in sorted(months.items())))
        # Passive recall only sees UNconsolidated facts; consolidation marks a session's
        # facts when it closes. Their age says whether passive facts can reach beyond the
        # session still open (which the live history already covers).
        now = time.time()
        uncons_age = [(now - t) / 3600 for f in facts if not f.get("consolidated")
                      for t in [ts_of(f)] if t is not None]
        print(f"    unconsolidated (eligible for passive recall) age {dist(uncons_age, 'h', 1)}")
        print(f"    before 2026-09-26 (assistant text still ingested on Discord): {dict(before)}")
        print(f"    after  2026-09-26 (Discord ingests Sam's text only):      {dict(after)}")
        corr = rows(conn, "SELECT COUNT(*) AS n FROM fact_corrections")
        print(f"    fact corrections on file: {corr[0]['n'] if corr else '?'}")
    else:
        print("  no mem0 fact collection found")
    if "jarvis_semantic" in cols:
        pts = list(qd.scroll("jarvis_semantic", ["kind", "folder", "channel", "exchange_id"]))
        kinds = Counter(p.get("kind") for p in pts)
        print(f"  jarvis_semantic points: {len(pts)} by kind {dict(kinds)}")
        ex = [p for p in pts if p.get("kind") == "exchange"]
        ids = Counter(p.get("exchange_id") for p in ex if p.get("exchange_id"))
        legacy = sum(1 for p in ex if not p.get("exchange_id"))
        print(f"    exchange points by channel {dict(Counter(p.get('channel') or '(none)' for p in ex))};"
              f" exchanges with an id {len(ids)} (chunks each {dist(list(ids.values()))});"
              f" legacy points without one {legacy}")
        print(f"    vault points by folder {dict(Counter(p.get('folder') for p in pts if p.get('kind') == 'vault').most_common(12))}")
    for name in ("jarvis_threads", "jarvis_sleep", "mem0_entities"):
        if name in cols:
            try:
                print(f"  {name}: {qd.count(name)} points")
            except Exception as exc:
                print(f"  {name}: count failed ({exc})")


# ---------------------------------------------------------------- 7. on-demand memory tools

_Q = re.compile(r"'query':\s*'((?:[^'\\]|\\.)*)'|'query':\s*\"((?:[^\"\\]|\\.)*)\"")
_KIND = re.compile(r"'kind':\s*'(\w+)'")
_DAYS = re.compile(r"'days_back':\s*(\d+)")
_ACTION = re.compile(r"'action':\s*'(\w+)'")


def _args(preview: str) -> dict:
    try:
        v = ast.literal_eval(preview)
        if isinstance(v, dict):
            return v
    except Exception:
        pass
    out = {}
    m = _Q.search(preview or "")
    if m:
        out["query"] = m.group(1) or m.group(2) or ""
    for key, rx in (("kind", _KIND), ("action", _ACTION)):
        m = rx.search(preview or "")
        if m:
            out[key] = m.group(1)
    m = _DAYS.search(preview or "")
    if m:
        out["days_back"] = int(m.group(1))
    return out


def memory_tools(conn, since, days):
    section("7. On-demand memory tools and memory writes (operation receipts)")
    ops = rows(conn, "SELECT o.run_id, o.tool, o.outcome, o.started_at, o.duration_ms, o.args_preview,"
                     " o.result_preview, r.channel FROM operations o LEFT JOIN runs r ON r.run_id=o.run_id"
                     " WHERE o.started_at >= ?", (since,))
    if not ops:
        print("  no operation receipts in window")
        return
    runs = rows(conn, "SELECT run_id FROM runs WHERE channel='discord' AND state='completed'"
                      " AND started_at >= ?", (since,))
    disc = {r["run_id"] for r in runs}
    for tool in MEMORY_TOOLS:
        sub = [o for o in ops if o["tool"] == tool]
        if not sub:
            print(f"  {tool}: 0 calls")
            continue
        per_turn = Counter(o["run_id"] for o in sub if o["run_id"] in disc)
        print(f"  {tool}: {len(sub)} calls ({len(sub) / days:.1f}/day), outcomes"
              f" {dict(Counter(o['outcome'] for o in sub))}, by channel"
              f" {dict(Counter(o['channel'] or 'background/none' for o in sub))};"
              f" Discord turns using it {share(len(per_turn), len(disc))};"
              f" duration {dist([(o['duration_ms'] or 0) / 1000 for o in sub], 's', 2)}")
        if tool == "memory_search":
            args = [_args(o["args_preview"]) for o in sub]
            print(f"    kinds: {dict(Counter((a.get('kind') or 'all') for a in args))};"
                  f" days_back set: {share(sum(1 for a in args if a.get('days_back')), len(args))}")
            res = Counter("hits" if (o["result_preview"] or "").startswith("Found") else
                          "nothing matched" if (o["result_preview"] or "").startswith("No ") else
                          "error" if (o["result_preview"] or "").startswith("Error") else "other"
                          for o in sub)
            print(f"    results: {dict(res)}")
            qs = [a.get("query") or "" for a in args]
            print(f"    query words {dist([len(q.split()) for q in qs if q])}")
            feature_report(qs, "memory_search queries")
            none_q = [a.get("query") or "" for a, o in zip(args, sub)
                      if (o["result_preview"] or "").startswith("No ")]
            if none_q:
                feature_report(none_q, "queries that matched NOTHING")
        if tool == "memory_timeline":
            res = Counter("none" if "No conversations recorded" in (o["result_preview"] or "") else
                          "error" if (o["result_preview"] or "").startswith("Error") else "ok" for o in sub)
            print(f"    results: {dict(res)}")
    section("7b. Durable-memory writes mid-conversation (how often a curated memory would change)")
    for tool in WRITE_TOOLS:
        sub = [o for o in ops if o["tool"] == tool]
        if not sub:
            print(f"  {tool}: 0 calls")
            continue
        acts = Counter((_args(o["args_preview"]).get("action") or "-") for o in sub)
        folders = Counter()
        if tool == "vault_write":
            for o in sub:
                m = re.search(r"'folder':\s*'(\w+)'", o["args_preview"] or "")
                folders[m.group(1) if m else "?"] += 1
        live = sum(1 for o in sub if o["run_id"] in disc)
        print(f"  {tool}: {len(sub)} calls ({len(sub) / days:.2f}/day), in Discord turns {live},"
              f" actions {dict(acts)}" + (f", folders {dict(folders)}" if folders else ""))


# ---------------------------------------------------------------- 8. curated memory sizes

def curated_sizes(root: Path, conn):
    section("8. What JARVIS already keeps as curated memory (sizes only)")
    vault = root / "jarvis-vault"

    def size(p: Path):
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
            return len(t), len(t.split())
        except OSError:
            return None
    for rel, where in (("people/owner.md", "stable block, every turn"),
                       ("queue.md", "stable block, every turn"),
                       ("self/positions.md", "volatile, opinion questions"),
                       ("people/predictions.md", "volatile, deep turns")):
        s = size(vault / rel)
        text = f"{s[0]:,} chars, {s[1]:,} words" if s else "missing"
        print(f"  {rel:24s} {text:28s} ({where})")
    q = vault / "queue.md"
    if q.exists():
        lines = q.read_text(encoding="utf-8", errors="replace").splitlines()
        st = Counter(m.group(1) for m in (re.match(r"\s*\[(open|in_progress|resolved)\]", l) for l in lines) if m)
        print(f"    queue lines by status {dict(st)}")
    letters = sorted((vault / "letters").glob("*.md"), key=lambda p: p.stat().st_mtime) if (vault / "letters").exists() else []
    if letters:
        s = size(letters[-1])
        print(f"  newest letter            {s[0]:,} chars (volatile, deep turns); letters on file: {len(letters)}")
    skills = list((vault / "skills").glob("*.md")) if (vault / "skills").exists() else []
    active, desc = 0, []
    for p in skills:
        head = p.read_text(encoding="utf-8", errors="replace")[:2000]
        if re.search(r"^status:\s*retired", head, re.M):
            continue
        active += 1
        m = re.search(r"^description:\s*(.+)$", head, re.M)
        desc.append(len(m.group(1)) if m else 0)
    print(f"  skills: {active} active; catalog description chars {sum(desc):,} (stable block)")
    people = [p for p in (vault / "people").glob("*.md")
              if p.name not in ("owner.md", "_template.md", "predictions.md")] if (vault / "people").exists() else []
    print(f"  people profiles: {len(people)} ({sum((size(p) or (0, 0))[0] for p in people):,} chars;"
          f" loaded only when the person is named)")
    rules = rows(conn, "SELECT state, scope, length(rule) AS n FROM standing_instructions")
    act = [r for r in rules if r["state"] == "active"]
    print(f"  standing rules: {len(act)} active ({sum(r['n'] for r in act):,} chars, on every message"
          f" in scope); all states {dict(Counter(r['state'] for r in rules))}")
    for folder in ("topics", "letters", "drafts", "shared", "attachments", "self", "creative"):
        d = vault / folder
        if d.exists():
            files = [p for p in d.rglob("*.md")]
            print(f"  vault/{folder:12s} {len(files):4d} notes, {sum(p.stat().st_size for p in files) / 1e6:.2f} MB")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--root", default=None)
    ap.add_argument("--journal", default=None, help="a saved `journalctl -u jarvis -o short-unix` file")
    ap.add_argument("--no-journal", action="store_true")
    ap.add_argument("--unit", default="jarvis", help="systemd unit name")
    ap.add_argument("--qdrant", default="http://127.0.0.1:6333")
    a = ap.parse_args()
    root = Path(a.root).resolve() if a.root else ROOT
    since = time.time() - a.days * 86400
    conn = connect(root)
    print(f"Window: last {a.days:g} days (from {day(since)})")
    real = exchanges_store(conn, since)
    jl = None if a.no_journal else read_journal(a.days, a.journal, a.unit)
    jinfo = journal_summary(jl, since)
    turns = timing_lines(jl)
    by_id = history_window(real, jinfo, since)
    llm_context(conn, since, a.days, turns, by_id, real)
    qdrant_stores(Qdrant(a.qdrant), conn)
    memory_tools(conn, since, a.days)
    curated_sizes(root, conn)


if __name__ == "__main__":
    main()

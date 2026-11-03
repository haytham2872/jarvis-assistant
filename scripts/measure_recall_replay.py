"""Read-only replay: what passive recall would inject for Sam's recent Discord messages.

    .venv/bin/python scripts/measure_recall_replay.py [--last 40] [--root /opt/jarvis]
        [--compare-cosine] [--force]

The logs do not record what recall injected on a turn (measure_memory_baseline.py says so).
This replays the production recall path -- memory.recall() and vault.read_relevant_topic(),
unchanged -- against the live stores for the last N Discord messages, once as a deep turn and
once as a not-deep turn, and reports how often each block is injected (mem0 facts, vault
passage, past exchanges, topic note, sleep marker), how large it is, and how long recall
takes on this host. --compare-cosine repeats the deep run with the cross-encoder switched off.

Read-only by construction:
  - SQLite: copied first (SQLite backup API from a mode=ro connection) to a temp directory;
    JARVIS_DB_PATH points the backend at the copy, deleted at the end.
  - Qdrant and mem0: searches and scrolls only. mem0's startup cleanup
    (memory._drop_stale_collections) and the embedder sentinel write are patched out, and
    mem0 telemetry is switched off for this process.
  - No model call: mem0.search() does not call an LLM; the embedder and the cross-encoder
    load from the local Hugging Face cache (HF_HUB_OFFLINE=1).
  - Prints counts, sizes and timings, never message or memory text.

An approximation, stated plainly: it shows what recall finds TODAY for those messages, not
what it found then. Exchanges and facts newer than each message are filtered out (by
timestamp / created_at), the message's own exchange too; vault notes and the consolidated
flag are today's. Each message's live history is taken to start at its session's first
earlier exchange, as recall's history_since. It loads gte-small, the cross-encoder and mem0 (several hundred MB beside
the running backend): it refuses to start with less than 1.5 GB available unless --force,
and runs at low CPU priority. Run it when JARVIS is idle.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def mem_available_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def copy_db(src: Path) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-replay-"))
    dst = tmpdir / "runtime.db"
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst


def created_ts(item) -> float | None:
    raw = item.get("created_at") if isinstance(item, dict) else None
    try:
        return dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


async def main_async(a) -> None:
    root = Path(a.root).resolve() if a.root else ROOT
    src = Path(os.getenv("JARVIS_DB_PATH", "") or root / ".jarvis_runtime.db")
    if not src.exists():
        sys.exit(f"No runtime database at {src}")
    avail = mem_available_mb()
    if avail is not None and avail < 1500 and not a.force:
        sys.exit(f"Only {avail} MB available; this loads ~600 MB of models beside the backend."
                 " Re-run when idle, or with --force.")
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass

    copy = copy_db(src)
    try:
        # Same environment the backend reads (JARVIS_USER_NAME keys the facts), but this
        # process writes nowhere real.
        try:
            from dotenv import load_dotenv
            load_dotenv(root / ".env", override=False)
        except Exception:
            pass
        os.environ["JARVIS_DB_PATH"] = str(copy)
        os.environ["MEM0_TELEMETRY"] = "False"
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        sys.path.insert(0, str(root))
        os.chdir(root)

        from backend.brain import embeddings, memory, rerank, semindex, threads
        from backend.tools import vault

        memory._drop_stale_collections = lambda: None      # no startup cleanup, ever
        memory._record_embedder_identity = lambda: None     # no sentinel write

        cutoff = {"ts": None}
        orig_search = semindex._search_sync

        def filtered_search(query, kind, k, score_min, folder, query_vec=None,
                            session_id=None, rerank=False):
            """Exchanges newer than the replayed message did not exist then: drop them.
            A plain (not reranked) exchange search fetches 3x and trims, so newer hits do
            not crowd older ones out of the top k. The reranked path builds its shortlist
            through this same function, so it is filtered too."""
            if kind == "exchange" and not rerank and cutoff["ts"] is not None:
                hits = orig_search(query, kind, k * 3, score_min, folder, query_vec, session_id, False)
                return [h for h in hits if float(h.get("timestamp", 0.0)) < cutoff["ts"]][:k]
            return orig_search(query, kind, k, score_min, folder, query_vec, session_id, rerank)
        semindex._search_sync = filtered_search

        seen = {"cands": [], "session": None}
        orig_candidates = threads.exchange_candidates

        async def recording_candidates(query, deep=True, query_vec=None, k=3, channel=None,
                                       exclude_since=None):
            out = await orig_candidates(query, deep=deep, query_vec=query_vec, k=k,
                                        channel=channel, exclude_since=exclude_since)
            seen["cands"] = [(c.get("session_id") == seen["session"], (c.get("text") or ""))
                             for c in out]
            return out
        threads.exchange_candidates = recording_candidates

        t = time.perf_counter()
        embeddings._get_model()
        print(f"embedder loaded in {time.perf_counter() - t:.1f}s")
        if rerank.enabled():
            t = time.perf_counter()
            loaded = rerank._get_model() is not None
            print(f"cross-encoder {'loaded' if loaded else 'NOT available (cosine only, as on a host without it)'}"
                  f" in {time.perf_counter() - t:.1f}s")
        t = time.perf_counter()
        mem = memory._get_mem()
        print(f"mem0 {'ready' if mem is not None else 'UNAVAILABLE (facts will read as empty)'}"
              f" in {time.perf_counter() - t:.1f}s")
        if mem is not None:
            orig_mem_search = mem.search

            def mem_search(query, **kw):
                res = orig_mem_search(query, **kw)
                items = res.get("results", res) if isinstance(res, dict) else (res or [])
                keep = [it for it in items
                        if cutoff["ts"] is None or (created_ts(it) or 0.0) < cutoff["ts"]]
                return {"results": keep} if isinstance(res, dict) else keep
            mem.search = mem_search

        conn = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        msgs = [dict(r) for r in conn.execute(
            "SELECT id, session_id, timestamp, user_text FROM exchanges WHERE channel='discord'"
            " AND length(user_text) > 0 ORDER BY timestamp DESC LIMIT ?", (a.last,)).fetchall()]
        # Where each message's live history began: the session's first earlier exchange
        # (the 40-turn window never fills). Production passes this as history_since.
        for m in msgs:
            row = conn.execute(
                "SELECT MIN(timestamp) FROM exchanges WHERE channel='discord' AND session_id=?"
                " AND timestamp < ?", (m["session_id"], m["timestamp"])).fetchone()
            m["history_since"] = row[0]
        conn.close()
        msgs.reverse()
        if not msgs:
            print("No Discord exchanges with full text to replay.")
            return
        print(f"replaying {len(msgs)} Discord messages from "
              f"{dt.datetime.fromtimestamp(msgs[0]['timestamp']):%Y-%m-%d} to "
              f"{dt.datetime.fromtimestamp(msgs[-1]['timestamp']):%Y-%m-%d}\n")

        async def one(m, deep: bool) -> dict:
            text = m["user_text"]
            cutoff["ts"] = float(m["timestamp"]) - 1.0
            seen["cands"] = []
            seen["session"] = m["session_id"]
            rerank.reset_turn()
            t0 = time.perf_counter()
            vecs = embeddings._embed_many_sync([text[:1000]])     # as intent.classify does
            qv = list(vecs[0]) if vecs else None
            rec = await memory.recall(text, deep, query_vec=qv, channel="discord",
                                      history_since=m["history_since"])
            topic = await vault.read_relevant_topic(text, query_vec=qv) if deep else ""
            secs = time.perf_counter() - t0
            rr = rerank.turn_stats()
            fact_lines = [l for l in (rec.facts or "").splitlines() if l.startswith("- ")]
            injected_ex = [c for c in seen["cands"] if c[1] and c[1] in (rec.exchange or "")]
            return {
                "facts": len(rec.facts or ""), "fact_lines": len(fact_lines),
                "fact_lines_assistant": sum(1 for l in fact_lines if "(you said this, not Sam)" in l),
                "vault": len(rec.vault_passage or ""), "exchange": len(rec.exchange or ""),
                "ex_n": len(injected_ex), "ex_same": sum(1 for s, _ in injected_ex if s),
                "ex_cands": len(seen["cands"]),
                "topic": len(topic or ""), "sleep": len(rec.sleep_info or ""),
                "secs": secs, "rr_pairs": rr["pairs"], "rr_secs": rr["seconds"],
            }

        def report(label, res):
            n = len(res)
            print(f"== {label}: {n} messages ==")
            for key, name in (("facts", "mem0 facts [Memory]"), ("vault", "vault passage"),
                              ("exchange", "past exchange(s)"), ("topic", "topic note"),
                              ("sleep", "[Last sleep]")):
                got = [r[key] for r in res if r[key]]
                print(f"  {name:22s} injected {share(len(got), n)}; chars when injected {dist(got)}")
            fl = [r for r in res if r["fact_lines"]]
            if fl:
                tot = sum(r["fact_lines"] for r in fl)
                asst = sum(r["fact_lines_assistant"] for r in fl)
                print(f"    fact lines per injection {dist([r['fact_lines'] for r in fl])};"
                      f" labelled 'you said this, not Sam': {share(asst, tot)}")
            ex = [r for r in res if r["ex_n"]]
            if ex:
                tot = sum(r["ex_n"] for r in ex)
                same = sum(r["ex_same"] for r in ex)
                print(f"    exchanges per injection {dist([r['ex_n'] for r in ex])}; same-session {share(same, tot)};"
                      f" candidates found {dist([r['ex_cands'] for r in res])}")
            total = [r["facts"] + r["vault"] + r["exchange"] + r["topic"] + r["sleep"] for r in res]
            print(f"  total injected chars {dist(total)} (~{(pct(total, .5) or 0) / 3.82:,.0f} tokens p50)")
            print(f"  nothing injected (besides sleep): {share(sum(1 for r in res if not (r['facts'] or r['vault'] or r['exchange'] or r['topic'])), n)}")
            print(f"  recall + topic time on this host {dist([r['secs'] for r in res], 's', 2)}")
            rr = [r for r in res if r["rr_pairs"]]
            if rr:
                print(f"  cross-encoder pairs {dist([r['rr_pairs'] for r in rr])};"
                      f" thread time {dist([r['rr_secs'] for r in rr], 's', 2)}")
            print()

        deep_res = [await one(m, True) for m in msgs]
        shallow_res = [await one(m, False) for m in msgs]
        report("DEEP turn (complex, or within 2 turns of one)", deep_res)
        report("NOT-DEEP turn (sleep marker only since 2026-10-11)", shallow_res)
        if a.compare_cosine:
            before = os.environ.get("JARVIS_RERANK")
            os.environ["JARVIS_RERANK"] = "0"
            try:
                cos_res = [await one(m, True) for m in msgs]
            finally:
                if before is None:
                    os.environ.pop("JARVIS_RERANK", None)
                else:
                    os.environ["JARVIS_RERANK"] = before
            report("DEEP turn, cross-encoder OFF (cosine floors only)", cos_res)
        letter = await vault.read_latest_letter()
        try:
            from backend.brain import predictions
            preds = await predictions.read_pending_block()
        except Exception:
            preds = ""
        sam = await vault.read_owner()
        queue = await vault.read_queue()
        print("Constant blocks today (chars): "
              f"portrait {len(sam):,} + queue {len(queue):,} (stable, every turn); "
              f"letter {len(letter):,} + predictions {len(preds):,} (volatile, deep turns)")
        print("Weight the two runs with the deep share in measure_memory_baseline.py section 3.")
    finally:
        try:
            from backend.storage import db as _db
            _db.close_all()
        except Exception:
            pass
        shutil.rmtree(copy.parent, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=40)
    ap.add_argument("--root", default=None)
    ap.add_argument("--compare-cosine", action="store_true")
    ap.add_argument("--force", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()

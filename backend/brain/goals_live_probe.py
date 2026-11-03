"""G0 live probes for the goals roadmap (docs/archive/GOALS-ROADMAP.md). Paid, opt-in, scratch world.

Run from the repo root with the suite's isolation rails loaded:

  JARVIS_GOALS_G0_LIVE=1 JARVIS_G0_REAL_SAMPLE=<sample.json> JARVIS_G0_REAL_LABELS=<labels.json> \
    .venv/Scripts/python.exe -m pytest backend/brain/goals_live_probe.py -p backend.conftest -s -q

Three probes, one aggregate spending ceiling (`CAP_USD`, checked before every request with a
conservative bound, then charged at OpenRouter's reported cost):

* `test_routing_baseline` -- the six baseline requests (3 trials) and a labelled sample of
  real messages through real Jev, `router.route_tier` and, when the thinking tier is chosen,
  the real `_plan_task`. No full chat(): Jev's probabilities, the tier and whether the planner
  returned a plan are what G0 baselines (section 13). Real message text never leaves the
  scratch files; the artifact keeps hashes, labels and numbers.
* `test_protocol` -- (a) a final step offered only one tool (a minimal goal_finish), pinned
  in turn to Together, BaseTen and Novita, with an earlier call to a tool no longer offered
  in the history; (b) real `_run_compat_loop` jobs (stub tools, real model) ending complete,
  continued after a forced cut-off, and incomplete when the continuation is cut off too.
* `test_goal_end_to_end` (G1a; JARVIS_GOALS_G1A_LIVE=1, ceiling $0.20) -- one sourced read
  goal started on his words through the real gateway, planned, worked in real sessions with
  real web reads, cut off once mid-session and resumed, checked and delivered.
* `test_goal_card_and_mirror_live` (G1b; JARVIS_GOALS_G1B_LIVE=1, ceiling $0.10) -- one real
  goal card in his Discord DMs (REST only, approved 2026-10-06) and the vault mirror.
* `test_g2_live` (G2; JARVIS_GOALS_G2_LIVE=1, ceiling $0.10, smaller with
  JARVIS_G2_CAP_USD) -- real sessions on seeded milestones: does the model pick goal_action,
  ask, goal_job and wait_for_event, with email, calendar and the research launch faked; and
  the new session schemas on Together, BaseTen and Novita (JARVIS_G2_HOSTS=0 skips them).
* `test_g3_live` (G3; JARVIS_GOALS_G3_LIVE=1, JARVIS_G3_CAP_USD per case) -- real chat() with
  Jev on and goals enabled: is a goal-worthy request offered a goal and nothing started, does an
  ordinary one stay a turn, does a direct request start one goal, does a late correction reach
  the running turn.
* `test_baseline` -- Appendix A of docs/archive/LONG-RUNNING-TASKS-ROADMAP-CLAUDE.md, moved here. Full
  chat() per trial ($0.04 cap each); separately opt-in (JARVIS_MISSION_BASELINE=1) and NOT
  part of G0's envelope.

Nothing real is written or sent: the database and vault are per-test temporary files, every
tool other than the stubbed one returns an error, and no email or calendar call is made.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CAP_USD = Decimal("0.15")          # docs/archive/GOALS-ROADMAP.md section 14, G0 envelope
_SPEND = {"usd": Decimal(0), "requests": 0, "jev": 0, "stopped": []}

PROMPTS = {
    "flat":     "I'm looking for a 2-bedroom flat in Lyon under 1,000 euros a month. Keep looking over the next two weeks and send me anything good.",
    "etf":      "Compare the three main Europe-domiciled S&P 500 ETFs (fees, tracking difference, domicile, replication) and write me a properly sourced report. No rush, keep working on it until it's solid.",
    "notary":   "My notary Maitre Durand still hasn't answered about the compromis. If he hasn't replied by Friday, draft a follow-up for me, and keep chasing until it's sorted.",
    "japan":    "Plan my trip to Japan in April: find flights from Paris under 700 euros, keep an eye on prices over the next month, and build an itinerary as things firm up.",
    "nvda":     "Track my NVDA thesis through earnings next month and tell me what changed.",
    "marathon": "Over the next few weeks, help me get ready for a half marathon on 15 November: build the plan, check in on how my runs went, and adjust as you go.",
}
# Section 12's definition: an ongoing objective with linked work, follow-up or research
# beyond one response. All six ask for work over days or weeks.
PROMPT_LABELS = dict.fromkeys(PROMPTS, True)

LIVE = os.getenv("JARVIS_GOALS_G0_LIVE") == "1"


def _bound(max_tokens: int, payload) -> Decimal:
    """Conservative pre-dispatch bound at V4.1 Flash's peak, cache-miss price."""
    est_in = len(json.dumps(payload, default=str)) // 3 + 500
    return Decimal("0.0000012") * max_tokens + Decimal("0.0000003") * est_in


def _admit(bound: Decimal, what: str) -> None:
    if _SPEND["usd"] + bound > CAP_USD:
        _SPEND["stopped"].append(what)
        raise RuntimeError(f"G0 live envelope reached before {what}")


def _charge(cost, bound: Decimal) -> None:
    _SPEND["usd"] += Decimal(str(cost)) if cost is not None else bound


_REAL: dict = {}


async def _request(**kwargs):
    """Every completion the probes send: admitted under the envelope, recorded in the
    scratch database's llm_calls like any call site, charged at the reported cost."""
    from backend.storage import llm_calls
    bound = _bound(int(kwargs.get("max_tokens") or 4096),
                   [kwargs.get("messages"), kwargs.get("tools")])
    _admit(bound, f"request {_SPEND['requests'] + 1}")
    _SPEND["requests"] += 1
    started = time.time()
    resp = await _REAL["client"].chat.completions.create(**kwargs)
    llm_calls.record_response(resp, model=kwargs.get("model", "?"), started_at=started,
                              purpose=llm_calls.current_purpose.get() or "probe.g0",
                              extra_body=kwargs.get("extra_body"),
                              max_tokens=kwargs.get("max_tokens"))
    _charge(getattr(getattr(resp, "usage", None), "cost", None), bound)
    return resp


def _world(monkeypatch, tmp_path):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from backend.brain import engine as C, router
    from backend.providers import gateway as providers
    from backend.tools import vault
    assert router.OPENROUTER.configured, "OpenRouter key is not configured"
    monkeypatch.setenv("JARVIS_JEV", "1")
    (tmp_path / "vault").mkdir()
    monkeypatch.setattr(vault, "_VAULT", tmp_path / "vault")
    _REAL["client"] = providers.get_async_client(router.OPENROUTER).with_options(max_retries=0)
    proxy = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_request)))
    monkeypatch.setattr(C, "_get_compat_client", lambda: proxy)
    return C, router, providers, proxy


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


# ---------------------------------------------------------------------------- routing

def _real_items() -> list[dict]:
    sample, labels = os.getenv("JARVIS_G0_REAL_SAMPLE"), os.getenv("JARVIS_G0_REAL_LABELS")
    if not (sample and labels):
        return []
    rows = json.loads(Path(sample).read_text(encoding="utf-8"))
    lab = json.loads(Path(labels).read_text(encoding="utf-8"))
    out = []
    for r in rows:
        sha = hashlib.sha256(r["user"].encode()).hexdigest()[:12]
        if sha in lab:
            out.append({"source": "real", "case": sha, "text": r["user"],
                        "channel": r.get("channel") or "local", "goal": lab[sha]["goal"],
                        "note": lab[sha].get("note", ""), "words": len(r["user"].split())})
    return out


def _baseline_work() -> dict:
    """What each baseline trial actually began, from the saved 5 October run."""
    out = {}
    path = DOCS / "goals-baseline-2026-10-05.jsonl"
    # The 5 October file was written in the desk's code page (cp1252), not UTF-8; only its
    # tool names are read here.
    for line in path.read_bytes().decode("utf-8", errors="replace").splitlines():
        r = json.loads(line)
        out[(r["case"], r["trial"])] = [t["tool"] for t in r["tools"]]
    return out


async def _classify(text: str):
    """One real Jev call, timed. Returns (answers|None, latency_s, cost, error)."""
    from backend.brain import turn_signals as ts
    bound = Decimal("0.0005")
    _admit(bound, "a Jev call")
    _SPEND["jev"] += 1
    t0 = time.perf_counter()
    try:
        answers, _, u = await ts._call(text)
        err, cost = None, u.get("cost")
    except Exception as e:                      # recorded as a fallback trial, not a success
        answers, err, cost = None, str(e)[:120], None
    latency = time.perf_counter() - t0
    _charge(cost, bound)
    return answers, latency, cost, err


@pytest.mark.skipif(not LIVE, reason="explicit paid probe only (JARVIS_GOALS_G0_LIVE=1)")
def test_routing_baseline(monkeypatch, tmp_path):
    C, router, providers, proxy = _world(monkeypatch, tmp_path)
    from backend.brain import turn_signals as ts
    items = [{"source": "baseline", "case": k, "text": v, "goal": PROMPT_LABELS[k],
              "trial": t, "words": len(v.split())} for k, v in PROMPTS.items() for t in (1, 2, 3)]
    items += [dict(r, trial=1) for r in _real_items()]
    work = _baseline_work()
    rows = []

    async def go():
        await ts.warm()
        for i, it in enumerate(items):
            answers, latency, cost, err = await _classify(it["text"])
            signals = None
            if answers is not None and latency <= ts.DEADLINE_S:
                signals = ts.interpret(answers, it["text"])
            fallback = (None if signals is not None else
                        f"error: {err}" if err else f"deadline: {latency:.2f}s")
            complex_ = signals.get("complex") if signals else None
            model, use_thinking = router.route_tier(it["text"], None, complex_=complex_)
            plan, plan_cost = None, None
            if use_thinking:
                before = _SPEND["usd"]
                plan = await C._plan_task(proxy, it["text"])
                plan_cost = float(_SPEND["usd"] - before)
            probs = (answers and {k: round(float(answers[k]["noul"]), 3)
                                  for k in ("complex", "persistence")}) or {}
            row = {
                "source": it["source"], "case": it["case"], "trial": it["trial"],
                "goal_label": it["goal"], "words": it["words"],
                "p_complex": probs.get("complex"), "p_persistence": probs.get("persistence"),
                "complex": complex_,
                "persistence": signals.get("persistence") if signals else None,
                "fallback": fallback, "jev_latency_s": round(latency, 3),
                "jev_cost": cost, "cold": i == 0,
                "tier": "thinking" if use_thinking else "standard",
                "planner_ran": use_thinking, "plan_steps": len(plan) if plan else 0,
                "plan_tools": [s.get("tool") for s in plan or [] if s.get("tool")],
                "planner_cost": plan_cost,
            }
            if it["source"] == "baseline":
                row["work_begun"] = work.get((it["case"], it["trial"]))   # trial 3: none saved
            else:
                row["channel"], row["note"] = it.get("channel"), it.get("note", "")
            rows.append(row)
            print(json.dumps(row), flush=True)
        await providers.close_all()
    try:
        asyncio.run(go())
    finally:
        _write(DOCS / "goals-g0-routing-2026-10-05.jsonl", rows)
        print(f"\nSPEND ${_SPEND['usd']:.5f} requests={_SPEND['requests']} jev={_SPEND['jev']}"
              f" stopped={_SPEND['stopped']}", flush=True)


# --------------------------------------------------------------------------- protocol

_FINISH_TOOL = {"type": "function", "function": {
    "name": "goal_finish",
    "description": "End this bounded session with one disposition.",
    "parameters": {"type": "object", "properties": {
        "ending": {"type": "string", "enum": ["continue", "milestone_done", "stuck"]},
        "summary": {"type": "string"}}, "required": ["ending", "summary"]}}}

_HISTORY = [
    {"role": "system", "content": "You are JARVIS, running one bounded goal session. Work only "
                                  "the assigned milestone, then end the session with goal_finish."},
    {"role": "user", "content": "Milestone: find the total expense ratio of iShares Core S&P 500 "
                                "UCITS ETF (CSPX), with its source and date."},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "web_browse", "arguments": '{"task": "CSPX TER"}'}}]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": "iShares CSPX factsheet, 30 Sep 2026: total expense ratio 0.07%."},
]


@pytest.mark.skipif(not LIVE, reason="explicit paid probe only (JARVIS_GOALS_G0_LIVE=1)")
def test_protocol(monkeypatch, tmp_path):
    C, router, providers, proxy = _world(monkeypatch, tmp_path)
    from backend.brain import background as B
    provider = router.OPENROUTER
    rows: list[dict] = []

    async def finish_step(host: str, tool_choice: str) -> dict:
        body = dict(provider.no_thinking_body)
        body["provider"] = {"only": [host], "allow_fallbacks": False}
        kwargs = dict(model=provider.standard, messages=_HISTORY, tools=[_FINISH_TOOL],
                      tool_choice=tool_choice, max_tokens=400, stream=False, extra_body=body)
        try:
            r = await _request(**kwargs)
            m = r.choices[0].message
            calls = m.tool_calls or []
            args_ok = False
            if calls:
                try:
                    a = json.loads(calls[0].function.arguments or "{}")
                    args_ok = a.get("ending") in ("continue", "milestone_done", "stuck") and bool(a.get("summary"))
                except ValueError:
                    pass
            return {"probe": "final_step_subset", "host": host, "tool_choice": tool_choice,
                    "served_by": getattr(r, "provider", None), "accepted": True,
                    "finish_reason": r.choices[0].finish_reason,
                    "called": [c.function.name for c in calls], "args_valid": args_ok,
                    "answered_text": bool((m.content or "").strip()),
                    "cost": getattr(r.usage, "cost", None)}
        except Exception as e:
            return {"probe": "final_step_subset", "host": host, "tool_choice": tool_choice,
                    "accepted": False, "error": str(e)[:200]}

    def stub_tools():
        async def browse(**kw):
            return ("Search results:\n1. iShares CSPX factsheet (30 Sep 2026): TER 0.07%, Ireland, "
                    "physical replication.\n2. Invesco SPXS factsheet (30 Sep 2026): TER 0.05%, "
                    "Ireland, synthetic (swap-based).\n3. Vanguard VUAA (30 Sep 2026): TER 0.07%, "
                    "Ireland, physical replication.")

        async def refused(**kw):
            return "Error: tool disabled in the G0 probe"
        monkeypatch.setattr(C, "TOOL_DISPATCH", {n: (browse if n == "web_browse" else refused)
                                                 for n in C.TOOL_DISPATCH})

    async def job(label: str, final_max: int | None = None, rescue_max: int | None = None,
                  steps: int = 3) -> dict:
        """A real job. final_max shrinks the tool-less last call (forced cut-off)."""
        if rescue_max is not None:
            monkeypatch.setattr(B, "_RESCUE_MAX_TOKENS", rescue_max)
        inner = proxy.chat.completions.create

        async def create(**kw):
            if final_max and "tools" not in kw and kw.get("max_tokens") == 4096 \
                    and not any("cut off" in str(m.get("content")) or "ended without" in str(m.get("content"))
                                for m in kw["messages"][-1:]):
                kw["max_tokens"] = final_max
            return await inner(**kw)
        shrunk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(C, "_get_compat_client", lambda: shrunk if final_max else proxy)
        monkeypatch.setattr(B, "_MAX_BG_ITERATIONS", steps)
        before = _SPEND["usd"]
        try:
            r = await B._run_compat_loop(
                "Compare the TER, domicile and replication of CSPX, VUAA and SPXS, from one "
                "web search, in a short paragraph.")
            out = {"probe": "background_job", "case": label, "status": r.status,
                   "ending": r.ending, "finish_reason": r.finish_reason, "calls": r.calls,
                   "output_tokens": r.output_tokens, "chars": len(r.text),
                   "text": r.text[:400]}
        except Exception as e:
            out = {"probe": "background_job", "case": label, "error": str(e)[:200]}
        out["cost"] = float(_SPEND["usd"] - before)
        monkeypatch.setattr(B, "_RESCUE_MAX_TOKENS", 4096)
        return out

    async def go():
        for host in ("Together", "BaseTen", "Novita"):
            for choice in ("auto", "required"):
                row = await finish_step(host, choice)
                rows.append(row)
                print(json.dumps(row), flush=True)
        stub_tools()
        monkeypatch.setattr(B, "_with_skills", lambda s, t: (s, t))
        for label, fm, rm, steps in (("ordinary", None, None, 3),
                                     ("cut_then_continued", 25, None, 2),
                                     ("cut_twice", 25, 12, 2)):
            row = await job(label, fm, rm, steps)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        await providers.close_all()
    try:
        asyncio.run(go())
    finally:
        _write(DOCS / "goals-g0-protocol-2026-10-05.jsonl", rows)
        print(f"\nSPEND ${_SPEND['usd']:.5f} requests={_SPEND['requests']}"
              f" stopped={_SPEND['stopped']}", flush=True)


# --------------------------------------------------- Appendix A baseline (not in G0's envelope)

SETUP = {
    "schedule_task": "Confirmed written to jarvis-vault/schedule.md (tasks): task #1 scheduled.",
    "schedule_reminder": "Confirmed written to jarvis-vault/schedule.md (reminders): reminder set.",
    "schedule_night_task": "Confirmed written to jarvis-vault/schedule.md (night_tasks): queued for tonight.",
    "watch_action": "Watch w1 set: checked every 6h for 30 days.",
    "run_in_background": "Background task #1 started. It runs while the conversation continues. Tell Sam it's underway.",
    "web_research": "Research job #1 started (standard). Tell Sam it is underway.",
    "research_queue": "Queued for research.",
    "forecast": "Logged forecast f1.",
    "week_plan": "Proposal drawn; nothing written.",
    "vault_write": "Confirmed written to jarvis-vault/drafts/note.md (verified by read-back).",
    "standing_instruction": "Saved rule r1.",
    "notion_action": "Created task in Notion.",
    "check_background_tasks": "Nothing running, nothing scheduled, nothing waiting.",
    "portfolio": "NVDA: 1.14339682 shares, cost 228.68 USD. Thesis: none recorded.",
    "gmail_action": "Inbox: 3 messages; none from Maitre Durand since 22 September.",
    "calendar_action": "Mon 2026-10-05: nothing scheduled.",
}
READ = "Search results:\n1. Example page -- figures vary; no single authoritative answer here.\n"


@pytest.mark.skipif(os.getenv("JARVIS_MISSION_BASELINE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2])
@pytest.mark.parametrize("case", list(PROMPTS))
def test_baseline(monkeypatch, tmp_path, case, trial):
    from backend.brain.portfolio_live_probe import _scratch
    from backend.storage import llm_calls, operations
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=14, cap="0.04")
    llm_calls._ensure()
    calls = []

    def recorder(name):
        async def fn(**kw):
            calls.append({"tool": name, "args": {k: str(v)[:160] for k, v in kw.items()}})
            return SETUP.get(name, READ)
        return fn
    monkeypatch.setattr(C, "TOOL_DISPATCH", {n: recorder(n) for n in C.TOOL_DISPATCH})

    async def run():
        events = []

        async def send(e):
            events.append(e)
        operations.current_run_id.set(f"baseline-{case}-{trial}")
        return await C.chat(PROMPTS[case], [], send, channel="discord")
    reply = asyncio.run(run())
    print("\nRESULT " + json.dumps({"case": case, "trial": trial, "tools": calls, "reply": reply,
                                     "requests": state["requests"]}, ensure_ascii=False), flush=True)


# ------------------------------------------------------------------- G1a: one live goal

@pytest.mark.skipif(os.getenv("JARVIS_GOALS_G1A_LIVE") != "1",
                    reason="explicit paid probe only (JARVIS_GOALS_G1A_LIVE=1)")
def test_goal_end_to_end(monkeypatch, tmp_path):
    """docs/archive/GOALS-ROADMAP.md G1a live gate: one small sourced read goal, started on his words
    through the real gateway, planned, worked in real sessions with real web reads, cut off
    once mid-session (as a restart would) and resumed, checked and delivered. Aggregate
    ceiling CAP_G1A_USD; the goal's own budget is $0.50. Mail, calendar, memory, portfolio,
    vault and code-graph reads are disabled: only the web is real."""
    import backend.brain.goals_live_probe as me
    monkeypatch.setattr(me, "CAP_USD", Decimal("0.20"))
    _SPEND.update(usd=Decimal(0), requests=0, jev=0, stopped=[])
    C, router, providers, proxy = _world(monkeypatch, tmp_path)
    from backend.brain import background as B, goal_runner as R
    from backend.brain.engine import _to_openai_tools
    from backend.storage import db
    from backend.storage import goals as S
    from backend.tools import gateway, policy
    monkeypatch.setenv("JARVIS_GOALS", "1")
    assert R.acquire_lock(tmp_path / "goals.lock")
    sent = []

    async def notify(text, key):
        sent.append({"key": key, "text": text})
        return True
    R.set_notifier(notify)
    monkeypatch.setattr(R, "_client", lambda: proxy)
    monkeypatch.setattr(R, "_task", None)

    async def disabled(**kw):
        return "Error: this tool is disabled in the live goal probe"
    for name in ("gmail_action", "calendar_action", "memory_search", "memory_timeline",
                 "portfolio", "vault_read", "vault_list", "code_graph", "skill"):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, disabled)

    words = ("Keep at it until it's solid: find the total expense ratio, domicile and replication "
             "method of CSPX and VUAA, with sources.")
    policy.start_scope(words=[words])
    schema_chars = len(json.dumps(_to_openai_tools(R.SESSION_TOOLS + R._read_tool_schemas())))
    timeline = []
    t0 = time.time()

    async def go():
        res = await gateway.execute("goal", {
            "action": "start", "title": "CSPX vs VUAA: fees, domicile, replication",
            "criteria": ["the total expense ratio of CSPX and of VUAA, each with a source",
                         "the domicile and replication method of each, with a source"],
            "budget_usd": 0.5, "quote": "Keep at it until it's solid"},
            dispatch=C.TOOL_DISPATCH, caller=policy.LIVE)
        timeline.append({"t": 0, "start": str(res.result)[:200], "ok": res.ok})
        gid = re.search(r"Goal (g[0-9a-f]+)", str(res.result)).group(1)
        interrupted = False
        for i in range(20):
            await R._cycle()
            if R._task is not None:
                if not interrupted and S.plan(gid) and i >= 1:
                    await asyncio.sleep(12)            # let the first session get going
                    if not R._task.done():
                        await R.cancel_running(gid)     # cut off, as a restart would
                        R._task = None
                        recovered = S.recover()
                        interrupted = True
                        timeline.append({"t": round(time.time() - t0), "cut_off": True,
                                         "recovered": len(recovered)})
                if R._task is not None:
                    await R._task
                    R._task = None
            g = S.get(gid)
            timeline.append({"t": round(time.time() - t0), "cycle": i, "state": g["state"]})
            if g["state"] in S.ENDED or _SPEND["stopped"]:
                break
        await providers.close_all()
        return gid

    import re
    gid = asyncio.run(go())
    conn = db.connect()
    g = S.get(gid)
    sessions = [dict(r) for r in conn.execute(
        "SELECT session_id, milestone_id, state, started_at, ended_at, ending_json, output_tokens"
        " FROM goal_sessions WHERE goal_id=? ORDER BY started_at", (gid,)).fetchall()]
    calls = [dict(r) for r in conn.execute(
        "SELECT purpose, input_tokens, cached_tokens, output_tokens, finish_reason, outcome,"
        " latency_ms, cost, host, run_id FROM llm_calls ORDER BY started_at").fetchall()]
    result = S.read_file(gid, "result.md")
    rows = [{"probe": "goal", "goal_id": gid, "state": g["state"], "outcome": g["outcome"],
             "seconds": round(time.time() - t0), "schema_chars": schema_chars,
             "plan": [{k: m[k] for k in ("milestone_id", "title", "state")} for m in S.plan(gid)],
             "charges": [{k: c[k] for k in ("purpose", "reserved_microusd", "settled_microusd",
                                             "state")} for c in S.charges(gid)],
             "evidence": len(S.evidence_index(gid)), "notes": len(S.notes(gid)),
             "files": S.files(gid), "messages": sent, "timeline": timeline,
             "result_md": (result or {}).get("body", "")}]
    rows += [{"probe": "session", **s} for s in sessions]
    rows += [{"probe": "call", **c} for c in calls]
    _write(DOCS / "goals-g1a-live-2026-10-05.jsonl", rows)
    print(f"\nGOAL {gid} {g['state']} {g['outcome']}\nSPEND ${_SPEND['usd']:.5f} "
          f"requests={_SPEND['requests']} stopped={_SPEND['stopped']}", flush=True)
    R.set_notifier(None)
    R.release_lock()


# ------------------------------------------------------- G1b: the card and the mirror, live

@pytest.mark.skipif(os.getenv("JARVIS_GOALS_G1B_LIVE") != "1",
                    reason="explicit probe only (JARVIS_GOALS_G1B_LIVE=1): posts one real card")
def test_goal_card_and_mirror_live(monkeypatch, tmp_path):
    """docs/archive/GOALS-ROADMAP.md G1b live gate, approved by Sam on 2026-10-06: one real goal card
    in his Discord DMs from the JARVIS bot, posted once and edited in place as a small read
    goal runs, plus its vault mirror in a scratch vault. Discord is reached through the REST
    API only (`Client.login`, no gateway), so the server's bot connection is untouched. The
    result message is NOT sent (recorded instead). Aggregate ceiling $0.10."""
    import discord
    import backend.brain.goals_live_probe as me
    monkeypatch.setattr(me, "CAP_USD", Decimal("0.10"))
    _SPEND.update(usd=Decimal(0), requests=0, jev=0, stopped=[])
    C, router, providers, proxy = _world(monkeypatch, tmp_path)
    from backend.brain import background as B, goal_runner as R
    from backend.storage import goals as S
    from backend.tools import gateway, policy
    monkeypatch.setenv("JARVIS_GOALS", "1")
    assert R.acquire_lock(tmp_path / "goals.lock")
    messages = []

    async def notify(text, key):
        messages.append(key)
        return True
    R.set_notifier(notify)
    monkeypatch.setattr(R, "_client", lambda: proxy)
    monkeypatch.setattr(R, "_task", None)
    monkeypatch.setattr(R, "_card_shown", {})

    async def disabled(**kw):
        return "Error: this tool is disabled in the live goal probe"
    for name in ("gmail_action", "calendar_action", "memory_search", "memory_timeline",
                 "portfolio", "vault_read", "vault_list", "code_graph", "skill"):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, disabled)

    card_log = []

    async def go():
        client = discord.Client(intents=discord.Intents.none())
        await client.login(os.environ["DISCORD_BOT_TOKEN"])          # REST only, no gateway
        user = await client.fetch_user(int(os.environ["DISCORD_ALLOWED_USER_ID"]))
        dm = await user.create_dm()

        async def post(text):
            m = await dm.send(text)
            card_log.append({"op": "post", "id": str(m.id), "text": text})
            return "sent", str(m.id)

        async def edit(mid, text):
            try:
                m = await dm.fetch_message(int(mid))
            except discord.NotFound:
                return "gone"
            await m.edit(content=text)
            card_log.append({"op": "edit", "id": mid, "text": text})
            return "ok"
        R.set_card_functions(post, edit)
        words = "Keep going until it's solid: what is the total expense ratio of SPXS, with a source?"
        policy.start_scope(words=[words])
        res = await gateway.execute("goal", {
            "action": "start", "title": "SPXS fee, sourced",
            "criteria": ["the total expense ratio of Invesco S&P 500 UCITS ETF (SPXS), with a source"],
            "budget_usd": 0.3, "quote": "Keep going until it's solid"},
            dispatch=C.TOOL_DISPATCH, caller=policy.LIVE)
        gid = re.search(r"Goal (g[0-9a-f]+)", str(res.result)).group(1)
        for i in range(14):
            await R._cycle()
            if R._task is not None:
                await R._task
                R._task = None
            if S.get(gid)["state"] in S.ENDED or _SPEND["stopped"]:
                break
        await providers.close_all()
        await client.close()
        return gid

    import re
    gid = asyncio.run(go())
    g = S.get(gid)
    from backend.tools import vault
    folder = Path(vault._VAULT) / "goals" / g["folder"]
    mirror = {p.name: p.read_text(encoding="utf-8") for p in folder.iterdir() if p.is_file()}
    rows = [{"probe": "g1b", "goal_id": gid, "state": g["state"], "outcome": g["outcome"],
             "card_message_id": g["card_message_id"],
             "card_ops": [{"op": c["op"], "id": c["id"], "text": c["text"]} for c in card_log],
             "mirror_files": {k: len(v) for k, v in mirror.items() if not k.startswith(".")},
             "mirror_plan": mirror.get("plan.md", ""), "messages_not_sent": messages,
             "spend_usd": float(_SPEND["usd"]), "requests": _SPEND["requests"]}]
    _write(DOCS / "goals-g1b-live-2026-10-06.jsonl", rows)
    print(f"\nGOAL {gid} {g['state']} card={g['card_message_id']} posts="
          f"{sum(c['op'] == 'post' for c in card_log)} edits={sum(c['op'] == 'edit' for c in card_log)}"
          f" mirror={sorted(rows[0]['mirror_files'])}\nSPEND ${_SPEND['usd']:.5f}", flush=True)
    R.set_card_functions(None, None)
    R.set_notifier(None)
    R.release_lock()


# ------------------------------------------- G2: tool selection, a fake critical action, hosts

G2_SCENARIOS = {
    "email": ("Write to the agency about the Oberkampf flat",
              "Sam's agency contact is contact@agence-oberkampf.example (he gave it). The "
              "milestone needs that agency asked by email whether the 2-room flat on rue "
              "Oberkampf is still available, and when it can be visited.",
              "an email to the agency, proposed for his approval"),
    "ask": ("Settle his maximum rent",
            "The shortlist depends on the most Sam will pay per month, which nobody but him "
            "knows; nothing in the evidence says it.",
            "his maximum monthly rent, from him"),
    "job": ("Survey the rental agencies of Paris 11e",
            "This needs a thorough, cited survey of the rental agencies active in Paris 11e "
            "(names, fees, specialties) -- longer than a few searches: start a research job "
            "and wait for it.",
            "a cited list of agencies from a research job"),
    "event": ("Catch a new listing",
              "Nothing suitable is listed yet. Wait until a 2-room flat under 1,400 euros a "
              "month is listed on rue Oberkampf, checked on the web every few hours.",
              "the listing, once it appears"),
}
G2_EXPECT = {"email": "goal_action", "ask": "ask", "job": "goal_job", "event": "wait_for_event"}


@pytest.mark.skipif(os.getenv("JARVIS_GOALS_G2_LIVE") != "1",
                    reason="explicit probe only (JARVIS_GOALS_G2_LIVE=1)")
def test_g2_live(monkeypatch, tmp_path):
    """docs/archive/GOALS-ROADMAP.md G2 live gate (ceiling $0.10): (a) real sessions on four seeded
    milestones -- does the model pick goal_action, ask, goal_job and wait_for_event, with
    well-formed arguments -- with the email tool and the research launch faked (nothing is
    sent, nothing is launched); (b) the new session schemas (nested condition, a free-form
    arguments object) accepted by each preferred host."""
    import backend.brain.goals_live_probe as me
    # A re-run after a fix can name its scenarios and skip the hosts, under a smaller ceiling,
    # so the slice's total stays inside its envelope.
    monkeypatch.setattr(me, "CAP_USD", Decimal(os.getenv("JARVIS_G2_CAP_USD", "0.10")))
    only = [s for s in os.getenv("JARVIS_G2_ONLY", "").split(",") if s] or list(G2_SCENARIOS)
    hosts = [] if os.getenv("JARVIS_G2_HOSTS") == "0" else ["Together", "BaseTen", "Novita"]
    _SPEND.update(usd=Decimal(0), requests=0, jev=0, stopped=[])
    C, router, providers, proxy = _world(monkeypatch, tmp_path)
    from backend.brain import background as B, goal_budget as budget, goal_runner as R
    from backend.brain.engine import _to_openai_tools
    from backend.storage import approvals as A, db, jobs
    from backend.storage import goals as S
    from backend.tools import approval_flow as AF
    monkeypatch.setenv("JARVIS_GOALS", "1")
    assert R.acquire_lock(tmp_path / "goals.lock")
    R.set_notifier(None)
    AF.set_notifier(None)
    monkeypatch.setattr(R, "_client", lambda: proxy)
    monkeypatch.setattr(R, "_task", None)
    sent_mail, launched = [], []

    async def never(**kw):
        sent_mail.append({"action": kw.get("action"), "to": kw.get("to")})
        return "Error: the live probe never sends email"

    async def disabled(**kw):
        return "Error: this tool is disabled in the live probe"

    async def browse(**kw):
        return ("Search results:\n1. SeLoger, 6 Oct 2026: no 2-room flat under 1,400 euros on "
                "rue Oberkampf this week.\n2. Agence Oberkampf -- contact page.")
    for name in list(C.TOOL_DISPATCH):
        monkeypatch.setitem(C.TOOL_DISPATCH, name,
                            browse if name in ("web_browse", "fetch_page") else disabled)
    monkeypatch.setitem(C.TOOL_DISPATCH, "gmail_action", never)
    monkeypatch.setitem(C.TOOL_DISPATCH, "calendar_action", never)

    async def launch(question, depth, goal_id):
        jid = jobs.enqueue({"kind": "web_research", "description": question, "goal_id": goal_id})
        jobs.claim(jid)
        launched.append({"job": jid, "question": question[:300]})
        return SimpleNamespace(job_id=jid, _handle=None), ""
    monkeypatch.setattr(R._bg, "start_goal_research", launch)
    rows: list[dict] = []

    async def scenario(name: str, trial: int) -> dict:
        title, detail, expected = G2_SCENARIOS[name]
        g = S.create(title=f"Flat hunt: {title}", words="find me a flat in Paris 11e and keep at it",
                     conversation_id="probe", criteria=["a shortlist of flats he can visit"],
                     state=S.ACTIVE, budget_microusd=budget.micro(0.30),
                     deadline_at=time.time() + 14 * 86400, creation_key=f"g2-{name}-{trial}")
        S.save_plan(g["goal_id"], [
            {"id": "M1", "title": f"{title}. {detail}", "expected": expected,
             "checks": [], "criteria": ["C1"], "depends": []},
            {"id": "M2", "title": "Write the shortlist", "expected": "result.md",
             "checks": [], "criteria": ["C1"], "depends": ["M1"]}], actor="jarvis", reason="probe")
        before = _SPEND["usd"]
        await R._cycle()
        if R._task is not None:
            await R._task
            R._task = None
        gid = g["goal_id"]
        sessions = [dict(r) for r in db.connect().execute(
            "SELECT ending_json, state FROM goal_sessions WHERE goal_id=?", (gid,)).fetchall()]
        ending = json.loads((sessions[0]["ending_json"] if sessions else None) or "{}")
        waits = S.waits(gid, state=None)
        pend = [r for r in A.pending()
                if (S.binding("approval", str(r["id"])) or {}).get("goal_id") == gid]
        chosen = {"goal_action": bool(pend), "ask": any(w["kind"] == "question" for w in waits),
                  "goal_job": any(e["kind"] == "job" for e in S.effects(gid)),
                  "wait_for_event": any(w["kind"] == "watch" for w in waits)}
        return {"probe": "g2_session", "scenario": name, "trial": trial,
                "expected": G2_EXPECT[name], "picked_expected": chosen[G2_EXPECT[name]],
                "chosen": chosen, "ending": ending.get("ending"),
                "session_state": sessions[0]["state"] if sessions else None,
                "waits": [{"kind": w["kind"], "payload": w["payload"]} for w in waits],
                "approvals": [{"id": r["id"], "args": r["arguments"], "why": r["why"][:300]}
                              for r in pend],
                "milestone_states": {m["milestone_id"]: m["state"] for m in S.plan(gid)},
                "goal_state": S.get(gid)["state"], "cost": float(_SPEND["usd"] - before)}

    async def host(h: str) -> dict:
        provider = router.OPENROUTER
        body = dict(provider.no_thinking_body)
        body["provider"] = {"only": [h], "allow_fallbacks": False}
        tools = _to_openai_tools([R.FINISH_TOOL, R.GOAL_ACTION_TOOL, R.GOAL_JOB_TOOL])
        messages = [{"role": "system", "content": R.SESSION_SYSTEM},
                    {"role": "user", "content": (
                        "Milestone M1: nothing is listed yet. End this session waiting for a "
                        "2-room flat under 1,400 euros a month to be listed on rue Oberkampf "
                        "(web, checked every 6 hours).")}]
        try:
            r = await _request(model=provider.standard, messages=messages, tools=tools,
                               tool_choice="required", max_tokens=600, stream=False,
                               extra_body=body)
            calls = r.choices[0].message.tool_calls or []
            args = json.loads(calls[0].function.arguments or "{}") if calls else {}
            cond = args.get("condition")
            return {"probe": "g2_host", "host": h, "accepted": True,
                    "served_by": getattr(r, "provider", None),
                    "called": [c.function.name for c in calls], "ending": args.get("ending"),
                    "condition_ok": isinstance(cond, dict)
                    and {"method", "query", "predicate"} <= set(cond),
                    "cost": getattr(r.usage, "cost", None)}
        except Exception as e:
            return {"probe": "g2_host", "host": h, "accepted": False, "error": str(e)[:200]}

    async def go():
        for trial in (1, 2):
            for name in only:
                if _SPEND["stopped"]:
                    break
                rows.append(await scenario(name, trial))
                print(json.dumps(rows[-1])[:500], flush=True)
        for h in hosts:
            rows.append(await host(h))
            print(json.dumps(rows[-1]), flush=True)
        await providers.close_all()

    asyncio.run(go())
    rows.append({"probe": "g2_summary", "spend_usd": float(_SPEND["usd"]),
                 "requests": _SPEND["requests"], "stopped": _SPEND["stopped"],
                 "mail_and_calendar_calls": sent_mail, "launches": launched})
    _write(DOCS / os.getenv("JARVIS_G2_OUT", "goals-g2-live-2026-10-06.jsonl"), rows)
    print(f"\nSPEND ${_SPEND['usd']:.5f} requests={_SPEND['requests']} mail/calendar={sent_mail} "
          f"stopped={_SPEND['stopped']}", flush=True)
    R.release_lock()


# ------------------------------------------------------------- G3: routing and steering

G3_CASES = [
    # (name, message, expected): offer = a goal offer and nothing started; turn = an ordinary
    # turn, no goal; start = he asked directly, one goal started.
    ("flat", PROMPTS["flat"], "offer"),
    ("notary", PROMPTS["notary"], "offer"),
    ("nvda", PROMPTS["nvda"], "offer"),
    ("etf_once", "Compare the three main Europe-domiciled S&P 500 ETFs on fees, tracking "
                 "difference, domicile and replication.", "turn"),
    ("price", "What's NVDA trading at?", "turn"),
    ("reminder", "Remind me Friday at 9 to email my notary.", "turn"),
    ("start", "Start a goal for this: keep looking for a 2-bedroom flat in Lyon under 1,000 "
              "euros a month over the next two weeks and send me anything good.", "start"),
]
G3_CORRECTION = "Actually, only accumulating share classes please."


@pytest.mark.skipif(os.getenv("JARVIS_GOALS_G3_LIVE") != "1",
                    reason="explicit paid probe only (JARVIS_GOALS_G3_LIVE=1)")
@pytest.mark.parametrize("case", [c[0] for c in G3_CASES] + ["correction"])
def test_g3_live(monkeypatch, tmp_path, case):
    """docs/archive/GOALS-ROADMAP.md G3 live: through the real chat() with Jev on and goals enabled, does a
    goal-worthy request get an offer and nothing started, an ordinary one stay a turn, a
    direct request start one goal, and a late correction reach the running turn? Every tool
    but `goal` records its call and answers with a canned line; the goal tool is real over
    the scratch database. Ceiling: JARVIS_G3_CAP_USD per case (default $0.03)."""
    from backend.brain.portfolio_live_probe import _scratch
    from backend.brain import goal_runner as R, steering, turn_signals as ts
    from backend.brain import goals as GT
    from backend.storage import goals as S, llm_calls, operations
    cap = os.getenv("JARVIS_G3_CAP_USD", "0.03")
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=10, cap=cap)
    llm_calls._ensure()
    monkeypatch.setenv("JARVIS_GOALS", "1")
    assert R.acquire_lock(tmp_path / "goals.lock")
    monkeypatch.setattr(R, "touched", lambda gid: asyncio.sleep(0))      # no card, no runner
    calls, seen = [], {"planned": 0, "signals": None}
    run_id = f"g3-{case}"

    def recorder(name):
        async def fn(**kw):
            calls.append({"tool": name, "args": {k: str(v)[:160] for k, v in kw.items()}})
            if case == "correction" and len(calls) == 1:     # the first tool call, any tool
                steering.push("discord", G3_CORRECTION, key="m-late", conversation="discord:dm",
                              target_run=run_id)
            return SETUP.get(name, READ)
        return fn
    dispatch = {n: recorder(n) for n in C.TOOL_DISPATCH}
    dispatch["goal"] = GT.goal
    monkeypatch.setattr(C, "TOOL_DISPATCH", dispatch)
    real_plan, real_resolve = C._plan_task, ts.resolve

    async def plan(*a, **k):
        seen["planned"] += 1
        return await real_plan(*a, **k)

    async def resolve(*a, **k):
        seen["signals"] = await real_resolve(*a, **k)
        return seen["signals"]
    monkeypatch.setattr(C, "_plan_task", plan)
    monkeypatch.setattr(ts, "resolve", resolve)
    text = dict((n, m) for n, m, _ in G3_CASES).get(case) or G3_CASES[3][1]
    expected = dict((n, e) for n, _, e in G3_CASES).get(case, "correction")

    async def run():
        async def send(e):
            pass
        operations.current_run_id.set(run_id)
        # Open the pooled connection in this loop first: each case is a fresh process, and a
        # cold first Jev call (~0.96 s) always misses the 0.8 s deadline.
        await ts.warm()
        with steering.turn("discord:dm", run_id, f"m-{case}"):
            return await C.chat(text, [], send, channel="discord")
    t0 = time.perf_counter()
    reply = asyncio.run(run())
    sig = seen["signals"]
    from backend.storage import db
    goals = {g["state"] for g in S.list_goals((S.PROPOSED, S.ACTIVE), limit=10)}
    row = {"probe": "g3", "case": case, "expected": expected,
           "jev": sig is not None,
           "jev_rows": [dict(r) for r in db.connect().execute(
               "SELECT finish_reason, latency_ms FROM llm_calls"
               " WHERE purpose='jev'").fetchall()],
           "p_goal": sig.probabilities.get("goal") if sig else None,
           "p_complex": sig.probabilities.get("complex") if sig else None,
           "goal_flag": sig.get("goal") if sig else None,
           "jev_latency": round(sig.latency_s, 3) if sig else None,
           "planned": seen["planned"], "tools": [c["tool"] for c in calls],
           "goal_states": sorted(goals),
           "absorbed": [r["state"] for r in steering._all_rows() if r["message_key"] == "m-late"],
           "mentions_accumulating": "accumulat" in (reply or "").lower(),
           "requests": state["requests"], "seconds": round(time.perf_counter() - t0, 1),
           "reply": (reply or "")[:600]}
    from backend.storage import db
    row["cost"] = float(db.connect().execute("SELECT coalesce(sum(cost),0) FROM llm_calls").fetchone()[0])
    print("\nRESULT " + json.dumps(row, ensure_ascii=False), flush=True)
    with open(DOCS / os.getenv("JARVIS_G3_OUT", "goals-g3-live-2026-10-06.jsonl"), "a",
              encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    R.release_lock()

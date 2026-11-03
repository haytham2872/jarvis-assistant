"""Thinking-headroom benchmark (H0): reasoning tokens by task shape and effort.

    .venv\\Scripts\\python.exe scripts\\bench_thinking.py fixtures   # real tools, once
    .venv\\Scripts\\python.exe scripts\\bench_thinking.py run        # 20 shapes x efforts x trials
    .venv\\Scripts\\python.exe scripts\\bench_thinking.py grade      # blind grade by a strong model
    .venv\\Scripts\\python.exe scripts\\bench_thinking.py report     # the table for CLAUDE.md

Every shape is one model call with the request JARVIS would actually send: the real
stable system prompt, the real tool schemas, and -- for shapes that follow tool calls --
real tool results fetched once by `fixtures` and replayed identically for every trial,
so effort is the only thing that varies. `compile_conclude` rebuilds the 2026-09-28
incident: a research request followed by nine real web searches, where the model must
now write the conclusion.

`max_tokens` is set far above anything observed (default 128,000) so the limit never
shapes the measurement, and the host order skips BaseTen, which caps output at 32,768.

Output lives in .jarvis_bench/ (gitignored): the fixtures carry the real Sam
portrait and vault notes. Runs resume: an existing (shape, effort, trial) is skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ.setdefault("JARVIS_JEV", "0")
os.environ.setdefault("JARVIS_DB_PATH", str(ROOT / ".jarvis_bench" / "bench.db"))

OUT = ROOT / ".jarvis_bench"
FIXTURES = OUT / "fixtures.json"
RESULTS = OUT / "results.jsonl"
GRADES = OUT / "grades.json"
EFFORTS = ("low", "high", "max")
HOSTS = ["Together", "Novita"]           # BaseTen is excluded on purpose (32,768 cap)
GRADER = "claude-opus-5-5"

# --- the shapes -----------------------------------------------------------------------------
# (id, kind, user_text, tool calls to run for context). kind "answer" is graded blind;
# kind "tool" is scored on whether the expected tool was chosen.

NVDA_ASK = ("Dig into whether my NVDA thesis still holds -- datacenter demand, custom-chip "
            "competition, export controls, and valuation -- then compile everything into a "
            "conclusion for me.")
NVDA_SEARCHES = [
    "NVIDIA datacenter revenue latest quarter 2026",
    "NVIDIA Blackwell Rubin demand hyperscaler capex 2026",
    "Google TPU Amazon Trainium custom AI chips competition NVIDIA 2026",
    "AMD MI400 vs NVIDIA market share 2026",
    "US export controls NVIDIA China chips 2026",
    "NVIDIA stock valuation forward P/E September 2026",
    "NVIDIA gross margin guidance 2026",
    "AI capex slowdown risk 2026 analysts",
    "NVIDIA stock price September 2026",
]
BATTERY_ASK = "Research the current state of solid-state batteries for EVs and tell me where it's really at."
BATTERY_SEARCHES = [
    "solid-state battery EV production 2026",
    "Toyota solid-state battery timeline 2026",
    "QuantumScape 2026 progress",
    "Samsung SDI solid-state battery 2026",
]

SHAPES = [
    ("chat_hello", "answer", "hey jarvis, long day. how are you doing?", []),
    ("chat_opinion", "answer",
     "What do you honestly think about stoicism as a way of handling stress?", []),
    ("lookup_choose", "tool", "What's the weather in Paris tomorrow?", []),
    ("lookup_answer", "answer", "What's the weather in Paris tomorrow?",
     [("web_browse", {"task": "weather forecast Paris tomorrow"})]),
    ("research_choose_next", "tool", BATTERY_ASK,
     [("web_browse", {"task": q}) for q in BATTERY_SEARCHES[:2]]),
    ("research_answer", "answer", BATTERY_ASK,
     [("web_browse", {"task": q}) for q in BATTERY_SEARCHES]),
    ("compile_conclude", "answer", NVDA_ASK,
     [("web_browse", {"task": q}) for q in NVDA_SEARCHES]),
    ("deep_analysis", "answer",
     "Analyse in depth, using my note and what's new, whether the NVDA decline thesis in my "
     "note still holds. Give me your conclusion.",
     [("vault_read", {"filename": "nvidia-stock-decline-analysis-2026-06.md", "folder": "topics", "section": "*"}),
      ("web_browse", {"task": NVDA_SEARCHES[0]}),
      ("web_browse", {"task": NVDA_SEARCHES[8]})]),
    ("planning", "answer",
     "Help me plan the next 4 weeks to prepare for a machine learning engineer interview. "
     "I have about an hour on weekdays and four hours on weekends.", []),
    ("essay", "answer",
     "Write me a long essay, around 1500 words, on whether AI assistants should remember "
     "their users.", []),
    ("math", "answer",
     "A tank fills through pipe A in 6 hours and through pipe B in 9 hours, and drains "
     "through pipe C in 12 hours. All three start at 8am on an empty tank; at 10am pipe B "
     "breaks. When is the tank full?", []),
    ("design", "answer",
     "How would you design a rate limiter for a public API doing 10k requests a second "
     "across five regions? Walk me through it.", []),
    ("memory_question", "answer", "What did I tell you about my NVDA position?", []),
    ("email_draft", "answer",
     "Draft an email to my landlord asking him to fix the heating, which has been broken "
     "for a week. Firm but polite. Don't send it.", []),
    ("compare", "answer",
     "Compare Kimi k2.6 and DeepSeek V4.1 Flash for running you: cost, speed and quality.", []),
    ("summarise_note", "answer", "Summarise this note for me in five points.",
     [("vault_read", {"filename": "anthropic-emotion-vectors-research.md", "folder": "topics", "section": "*"})]),
    ("forecast", "tool", "What are the odds the ECB cuts rates at its October meeting?", []),
    ("standing_rule", "tool",
     "Stop using bullet points when you answer me on Discord. Ever.", []),
    ("reminder", "tool", "Remind me tomorrow at 9 to call the bank.", []),
    ("self_arch", "answer",
     "Why do you sometimes go quiet on long tasks? Explain what actually happens inside you.", []),
]
EXPECTED_TOOL = {
    "lookup_choose": {"web_browse"}, "research_choose_next": {"web_browse", "fetch_page"},
    "forecast": {"forecast"}, "standing_rule": {"standing_instruction"},
    "reminder": {"schedule_reminder"},
}
MEMORY_EXCHANGE = (
    "[Earlier exchange, 2026-09-21]\nSam: I'm holding 40 NVDA shares I bought at $118 in "
    "March. If it drops under 160 I want to trim half, but I'm not selling before earnings.\n"
    "JARVIS: Noted -- 40 shares at 118, trim half under 160, nothing before earnings."
)


# --- fixtures -------------------------------------------------------------------------------

async def build_fixtures() -> None:
    from backend.brain import engine, personality
    from backend.tools.registry import TOOLS
    from backend.tools.browser import web_browse
    from backend.tools.vault import vault_read

    runners = {"web_browse": lambda a: web_browse(**a), "vault_read": lambda a: vault_read(**a)}
    sam = (ROOT / "jarvis-vault" / "people" / "owner.md").read_text(encoding="utf-8")
    cache: dict[str, str] = {}
    shapes = []
    for sid, kind, user_text, calls in SHAPES:
        stable, volatile = personality.build_system_prompt(
            user_text, owner_md=sam,
            exchange=MEMORY_EXCHANGE if sid == "memory_question" else "")
        system, messages = engine._compose_compat_request(stable, volatile, [], user_text, "discord")
        messages = [{"role": "system", "content": system}] + messages
        for i, (tool, args) in enumerate(calls):
            key = json.dumps([tool, args], sort_keys=True)
            if key not in cache:
                print(f"  {sid}: {tool} {args}", flush=True)
                cache[key] = str(await runners[tool](args))
            call_id = f"call_{sid}_{i}"
            messages.append({"role": "assistant", "content": "", "reasoning_content": "",
                             "tool_calls": [{"id": call_id, "type": "function", "function": {
                                 "name": tool, "arguments": json.dumps(args)}}]})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": cache[key]})
        shapes.append({"id": sid, "kind": kind, "user_text": user_text, "messages": messages,
                       "input_chars": sum(len(m.get("content") or "") for m in messages)})
    OUT.mkdir(exist_ok=True)
    FIXTURES.write_text(json.dumps({"tools": engine._to_openai_tools(TOOLS), "shapes": shapes},
                                   ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(shapes)} shapes to {FIXTURES}")


# --- run ------------------------------------------------------------------------------------
# One trial = one whole turn through the real engine._chat_compat: planner, tool loop,
# escalation, leak recovery. Read-only tools run for real (results cached per argument set,
# so every trial that asks the same thing sees the same page); tools with side effects are
# stubbed. Every model call lands in .jarvis_bench/bench.db via H0's llm_calls record,
# tagged with the turn's run id, which is how per-iteration reasoning is read back.

_READ_ONLY = {"web_browse", "fetch_page", "vault_read", "vault_list", "memory_search",
              "memory_timeline", "get_self_model", "code_graph", "check_background_tasks"}
_STUB_REPLY = {
    "ask_clarifying_question": "No reply arrived within the wait. Proceed on your best "
                               "assumption and say what you assumed.",
    "gmail_action": "Error: Gmail is not available in this environment.",
    "calendar_action": "Error: Calendar is not available in this environment.",
    "spotify_action": "Error: Spotify is not available in this environment.",
}


def _install_harness(max_tokens: int):
    """Patch the live loop for measurement. Returns the per-turn ContextVars."""
    import contextvars
    from backend.brain import engine

    effort_var = contextvars.ContextVar("bench_effort", default="high")
    tools_var = contextvars.ContextVar("bench_tools", default=None)
    cache: dict[str, str] = {}
    if max_tokens:
        # Unbounded measurement: every effort gets the same generous ceiling.
        engine._OUTPUT_LIMITS = {k: max_tokens for k in engine._OUTPUT_LIMITS}
        engine._TURN_OUTPUT_BUDGET = 10 ** 9
    orig_cfg = engine._reasoning_config

    def cfg(provider, use_thinking):
        temperature, body = orig_cfg(provider, use_thinking)
        if use_thinking and body is not None:
            body = {**body, "reasoning": {"effort": effort_var.get()}}
        return temperature, body
    engine._reasoning_config = cfg

    def wrap(name, fn):
        async def call(**kwargs):
            used = tools_var.get()
            if used is not None:
                used.append(name)
            if name in _READ_ONLY:
                key = json.dumps([name, kwargs], sort_keys=True)
                if key not in cache:
                    cache[key] = str(await fn(**kwargs))
                return cache[key]
            return _STUB_REPLY.get(name, f"Confirmed (benchmark stub): {name} done with "
                                         f"{json.dumps(kwargs)[:200]}")
        return call
    for name, fn in list(engine.TOOL_DISPATCH.items()):
        engine.TOOL_DISPATCH[name] = wrap(name, fn)
    return effort_var, tools_var


async def one_turn(shape, effort, trial, effort_var, tools_var) -> dict:
    from backend.brain import engine, router
    from backend.storage import llm_calls, operations
    from backend.tools.registry import TOOLS

    async def send(_m):
        pass

    run_id = f"bench:{shape['id']}:{effort}:{trial}:{int(time.time())}"
    tokens = [operations.current_run_id.set(run_id), effort_var.set(effort),
              tools_var.set([])]
    fresh = not any(m["role"] == "tool" for m in shape["messages"])
    started = time.time()
    try:
        text = await engine._chat_compat(
            router.OPENROUTER_THINKING, shape["messages"][0]["content"], shape["messages"][1:],
            send, None, user_text=shape["user_text"] if fresh else "", tools=TOOLS,
            channel="local", use_thinking=True)
        tools_used = list(tools_var.get())
    finally:
        tools_var.reset(tokens[2]); effort_var.reset(tokens[1])
        operations.current_run_id.reset(tokens[0])
    calls = [r for r in llm_calls.recent(since_s=10 ** 9) if r["run_id"] == run_id]
    chat = [r for r in calls if r["purpose"] in ("chat", llm_calls.RECOVERY)]
    last = chat[-1] if chat else {}
    return {
        "shape": shape["id"], "effort": effort, "trial": trial,
        "latency_s": round(time.time() - started, 1),
        "iterations": len(chat), "planned": any(r["purpose"] == "planner" for r in calls),
        "recoveries": sum(1 for r in calls if r["purpose"] == llm_calls.RECOVERY),
        "cut_off": sum(1 for r in chat if r["finish_reason"] == "length"),
        "reasoning_per_call": [r["reasoning_tokens"] or 0 for r in chat],
        "effort_per_call": [r["effort"] for r in chat],
        "synthesis_reasoning": last.get("reasoning_tokens") or 0,
        "turn_reasoning": sum(r["reasoning_tokens"] or 0 for r in chat),
        "turn_output": sum(r["output_tokens"] or 0 for r in calls),
        "max_input": max((r["input_tokens"] or 0 for r in chat), default=0),
        "hosts": sorted({r["host"] for r in calls if r["host"]}),
        "cost": round(sum(r["cost"] or 0 for r in calls), 5),
        "tools": tools_used, "text": text,
    }


async def run(trials: int, concurrency: int, max_tokens: int, only: list[str] | None) -> None:
    fx = json.loads(FIXTURES.read_text(encoding="utf-8"))
    done = set()
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["shape"], r["effort"], r["trial"]))
    effort_var, tools_var = _install_harness(max_tokens)
    jobs = [(s, e, t) for s in fx["shapes"] for e in EFFORTS for t in range(trials)
            if (s["id"], e, t) not in done and (not only or s["id"] in only)]
    random.shuffle(jobs)                      # spread host load and time-of-day effects
    print(f"{len(jobs)} turns to run ({len(done)} already done)", flush=True)
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def go(shape, effort, trial):
        async with sem:
            for attempt in range(3):
                try:
                    r = await one_turn(shape, effort, trial, effort_var, tools_var)
                    break
                except Exception as exc:
                    print(f"  {shape['id']}/{effort}/{trial}: {exc!r} (attempt {attempt + 1})")
                    await asyncio.sleep(5)
            else:
                return
            async with lock:
                with RESULTS.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"  {shape['id']:22s} {effort:4s} t{trial} calls={r['iterations']} "
                  f"reasoning={r['reasoning_per_call']} {r['latency_s']}s cut={r['cut_off']} "
                  f"rec={r['recoveries']} {','.join(r['tools'])}", flush=True)

    await asyncio.gather(*(go(*j) for j in jobs))


# --- grade ----------------------------------------------------------------------------------

_GRADE_PROMPT = """You are grading answers from a personal assistant called JARVIS to its user, Sam.
JARVIS's replies are read on Discord or spoken aloud, so plain prose is expected.

The request and all the context JARVIS had (tool results included) are below, then {n} candidate
answers labelled by letter. The candidates were produced independently; judge each on its own
merits and against the others:
- correct and grounded in the context given (no invented facts or figures),
- actually answers what was asked, with the depth the request calls for,
- clear and no longer than it needs to be.

Score each from 1 (useless or wrong) to 10 (excellent). Use the full range. Reply with JSON only:
{{"scores": {{"A": 7, "B": 5, ...}}, "notes": "one or two sentences on what separated the best from the worst"}}

=== REQUEST ===
{request}

=== CONTEXT JARVIS HAD (tool results, oldest first) ===
{context}

=== CANDIDATES ===
{candidates}"""


def _context_of(shape) -> str:
    parts = []
    for m in shape["messages"]:
        if m["role"] == "tool":
            parts.append(m["content"][:30000])
    if shape["id"] == "memory_question":
        parts.append(MEMORY_EXCHANGE)
    return "\n\n---\n\n".join(parts) or "(none -- answered from its own knowledge)"


async def grade() -> None:
    # The blind grader is a separate model from the one being measured (Claude, through
    # Anthropic's API). It needs `pip install anthropic` and ANTHROPIC_API_KEY; nothing
    # else in the project uses either.
    import anthropic

    fx = {s["id"]: s for s in json.loads(FIXTURES.read_text(encoding="utf-8"))["shapes"]}
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines()]
    grades = json.loads(GRADES.read_text(encoding="utf-8")) if GRADES.exists() else {}
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    rng = random.Random(7)
    for sid, shape in fx.items():
        if shape["kind"] != "answer" or sid in grades:
            continue
        cands = [r for r in rows if r["shape"] == sid and r["text"].strip()]
        if not cands:
            continue
        rng.shuffle(cands)
        letters = [chr(ord("A") + i) for i in range(len(cands))]
        body = "\n\n".join(f"--- {L} ---\n{r['text']}" for L, r in zip(letters, cands))
        prompt = _GRADE_PROMPT.format(n=len(cands), request=shape["user_text"],
                                      context=_context_of(shape), candidates=body)
        msg = await client.messages.create(model=GRADER, max_tokens=2000,
                                           messages=[{"role": "user", "content": prompt}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        start, end = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[start:end + 1])
        grades[sid] = {"notes": parsed.get("notes", ""), "scores": [
            {"effort": r["effort"], "trial": r["trial"], "score": parsed["scores"].get(L)}
            for L, r in zip(letters, cands)]}
        GRADES.write_text(json.dumps(grades, indent=1), encoding="utf-8")
        print(f"  graded {sid}: {parsed.get('notes', '')[:140]}", flush=True)


# --- report ---------------------------------------------------------------------------------

def _p(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(q * (len(v) - 1))))] if v else None


def report() -> None:
    fx = {s["id"]: s for s in json.loads(FIXTURES.read_text(encoding="utf-8"))["shapes"]}
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines()]
    grades = json.loads(GRADES.read_text(encoding="utf-8")) if GRADES.exists() else {}
    print("| shape | " + " | ".join(
        f"{e}: turn reasoning (median, max) · synthesis · calls · time · done · grade"
        for e in EFFORTS) + " |")
    print("|---|" + "---|" * len(EFFORTS))
    per_call = {e: [] for e in EFFORTS}
    for sid, shape in fx.items():
        sr = [r for r in rows if r["shape"] == sid]
        if not sr:
            continue
        cells = []
        for e in EFFORTS:
            er = [r for r in sr if r["effort"] == e]
            if not er:
                cells.append("—")
                continue
            for r in er:
                per_call[e] += [n for n, f in zip(r["reasoning_per_call"], r["effort_per_call"])
                                if f == e]
            turn = [r["turn_reasoning"] for r in er]
            synth = statistics.median(r["synthesis_reasoning"] for r in er)
            calls = statistics.median(r["iterations"] for r in er)
            lat = statistics.median(r["latency_s"] for r in er)
            if shape["kind"] == "tool":
                ok = sum(1 for r in er if set(r["tools"]) & EXPECTED_TOOL.get(sid, set()))
                done, g = f"tool {ok}/{len(er)}", ""
            else:
                ok = sum(1 for r in er if r["text"].strip() and not r["recoveries"])
                done = f"{ok}/{len(er)}"
                sc = [x["score"] for x in grades.get(sid, {}).get("scores", [])
                      if x["effort"] == e and x["score"] is not None]
                g = f" · {statistics.mean(sc):.1f}" if sc else ""
            cells.append(f"{int(statistics.median(turn)):,} ({max(turn):,}) · {int(synth):,} · "
                         f"{calls:g} · {lat:.0f}s · {done}{g}")
        print(f"| {sid} | " + " | ".join(cells) + " |")
    print()
    print("Reasoning tokens per call, by effort:")
    for e in EFFORTS:
        v = per_call[e]
        if v:
            print(f"  {e}: n={len(v)} p50={_p(v, .5):,} p90={_p(v, .9):,} p95={_p(v, .95):,} "
                  f"max={max(v):,}")
    print("Whole-turn output tokens, by effort:")
    for e in EFFORTS:
        v = [r["turn_output"] for r in rows if r["effort"] == e]
        if v:
            print(f"  {e}: n={len(v)} p50={_p(v, .5):,} p95={_p(v, .95):,} max={max(v):,} "
                  f"time p50={_p([r['latency_s'] for r in rows if r['effort'] == e], .5)}s "
                  f"max={max(r['latency_s'] for r in rows if r['effort'] == e)}s "
                  f"cost p50=${_p([r['cost'] for r in rows if r['effort'] == e], .5)} "
                  f"max=${max(r['cost'] for r in rows if r['effort'] == e)}")
    print(f"cut off at the limit: {sum(r['cut_off'] for r in rows)}; recoveries: "
          f"{sum(r['recoveries'] for r in rows)}; hosts: "
          + ", ".join(sorted({h for r in rows for h in r['hosts']})))
    print(f"total cost ${sum(r['cost'] for r in rows):.3f} over {len(rows)} turns")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["fixtures", "run", "grade", "report"])
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="0 = the shipped limits and turn budget; N = every call gets N, no turn budget")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--tag", default="", help="write results-<tag>.jsonl / grades-<tag>.json")
    a = ap.parse_args()
    if a.tag:
        global RESULTS, GRADES
        RESULTS = OUT / f"results-{a.tag}.jsonl"
        GRADES = OUT / f"grades-{a.tag}.json"
    if a.step == "fixtures":
        asyncio.run(build_fixtures())
    elif a.step == "run":
        asyncio.run(run(a.trials, a.concurrency, a.max_tokens, a.only))
    elif a.step == "grade":
        asyncio.run(grade())
    else:
        report()


if __name__ == "__main__":
    main()

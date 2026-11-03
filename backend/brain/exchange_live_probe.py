"""Optional real-model probe: the Discord exchange of 5 October 2026, 00:13-00:17. NEVER part of
normal pytest discovery.

JARVIS_EXCHANGE_LIVE=1 python -m pytest backend/brain/exchange_live_probe.py -s -q
Entry points: test_exchange (three variants of the stored notes, three trials each),
test_goodnight_draft_is_checked_against_the_ledger (last night's reply as the first draft, three
trials) and test_goodnight_with_the_old_goodnight_recalled (the goodnight as it ran on the
server: a deep turn with the 3 October goodnight recalled, five trials).

Sam sent the skills-update paragraph, then "ok cancel and re add each one against the skill
this time", then "good night". Live, three things went wrong:
  1. the update paragraph got a one-step plan DM ("Working on: ... Acknowledge the new skill
     system update ... I'll message you when it's done");
  2. the scheduling reply was marked "followed: stock-research" and gained a bull and a bear
     case on NVDA and SPCX -- the skill's Checks applied to a reply that set up a routine;
  3. "good night" was answered with "the ledger is empty ... say the five lines again", one turn
     after the same conversation quoted his SPCX position from the ledger.
Replayed here in a scratch world: the four plain scheduled checks, the migrated skills and his
five positions (real skill, schedule_task, portfolio and activity tools; web pages fixed text).
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from backend.brain.skills_live_probe import PAGE, WEB, _migrated

UPDATE = ("i added a new update here's what's new: your skills work differently now. The list of them is in "
          "your prompt, and you open one with the skill tool when a request matches, not before. When I tell "
          "you how I want a kind of task done, save it; when I say \"remember how you did that\", \"do it like "
          "last Tuesday\", or send a checklist and say \"make this a skill\", use learn, which builds it from "
          "what you actually did rather than from memory. Every reply that followed a skill ends with a line "
          "naming it, so when I correct how you did it (\"skip the analyst part next time\"), patch that skill "
          "instead of making a standing rule, and when I say to stop doing something, remove the step and add "
          "a pitfall. I can take any change back with /skill undo. A skill can run on a timer: \"every Monday "
          "at 9, do the stock research on TTWO\" is a routine, and each run follows the skill as it is that "
          "morning. Background jobs and routines can open your skills but never change them. Before a reply "
          "that followed a skill goes out, it's checked against the skill's own checks. And each night you "
          "look after them: something I asked for three times on different days becomes a skill, duplicates "
          "merge, a correction you missed gets folded in, unused ones retire after 90 days, and the morning "
          "brief tells me what changed.")
REPLY_1 = ("Confirmed from the schedule, not from memory: the NVDA and SPCX runs are there as plain timed checks "
           "-- open 15:30 and close 23:30 weekdays through 30 November -- and neither names a skill. So they sit "
           "on the old side of this update. If you want them inheriting stock-research instead, say so and I'll "
           "cancel and re-add each one against the skill; from then on a patch to the procedure reaches the next "
           "morning's run.\n\nWhile I was in there: the Sunday 15:00 interview reminder did fire yesterday, so "
           "that open item is closed. The Diametral prep is still the live one -- interview at 10:30 today, Teams."
           "\n\nAnd the five portfolio closes are queued for 22:20 tonight, which will be the first marks this "
           "book has had.")
RE_ADD = ("ok cancel and re add each one against the skill this time, and i will work on the interview prep "
          "tomorrow morning before it don't worry")
QUEUE = ("[open] 2026-10-01 -- Diametral interview Monday 10:30 (Teams): prep the company, the role and two "
         "questions to ask.")
# What the 3 October session plausibly left behind (its consolidation read the false "nothing is
# in the ledger" reply). Used only by the variant that tests whether such a note alone causes it.
STALE = ("\n[open] 2026-10-03 -- Portfolio ledger is empty: his holdings message never reached the tool in a "
         "form it would take, so no positions were recorded. Ask him to send the five lines again in one "
         "sentence and record them.")
PORTRAIT = ("## Situation\nPreparing for the Diametral interview (Monday 10:30, Teams). Tracks a small "
            "stock portfolio. His five opening positions never reached the ledger on 3 October: the write "
            "failed and nothing was recorded, so he needs to send the five lines again in one sentence.\n\n"
            "## Recurring themes\nInvesting, AI.\n\n## Observations\nPrefers short answers.")
HOLDINGS = (("NVDA", "1.14339682", "228.68"), ("TTWO", "0.24335794", "238.13"), ("TSLA", "0.02822711", "426.89"),
            ("QNT", "2.2683417", "49.75"), ("SPCX", "1.68563049", "148.52"))
# An actual case ("Bull case, as the runs will argue it: NVDA closed at ..."), not the skill's
# steps listed ("dated figures, the week's news, bull and bear case, what it means ...").
BULL_BEAR = re.compile(r"(?<!\band )\b(bull|bear) case\s*[:,]|\bthe (bull|bear) case (on|for)\b", re.I)
EMPTY_LEDGER = re.compile(r"ledger is empty|nothing (is )?in the ledger|no positions (were|are) recorded|"
                          r"(say|send) the five lines again", re.I)


def _seed(chronos, P, S):
    from backend.storage import lifelog
    for sym, open_or_close, when, task in (
            ("NVDA", "open", "2026-10-06 15:30", "Report NVDA's price at the market open from a live quote page, never a delayed tick, with the time it was read."),
            ("SPCX", "open", "2026-10-06 15:30", "Report SPCX's price at the market open from a live quote page, never a delayed tick, with the time it was read."),
            ("NVDA", "close", "2026-10-06 23:30", "Report NVDA's official closing price from the historical price table, with the day's change."),
            ("SPCX", "close", "2026-10-06 23:30", "Report SPCX's official closing price from the historical price table, with the day's change.")):
        out = asyncio.run(chronos.schedule_task("add", when=when, task=task, repeat="weekdays",
                                                until="2026-11-30", timezone="America/New_York"))
        assert out.startswith("Confirmed"), out
    extra = {"QNT": "Quantinuum Inc.", "SPCX": "Space Exploration Technologies Corp."}
    for sym, qty, price in HOLDINGS:
        i = ({"symbol": sym, "exchange": "NASDAQ", "currency": "USD", "price_key": sym, "name": extra[sym]}
             if sym in extra else P.resolve_instrument(sym))
        S.record_trades([{"instrument": i, "side": "opening", "shares": qty, "price": price, "currency": "USD",
                          "fees": "0", "native_price": price, "native_fees": "0", "traded_on": "2026-10-03",
                          "quote": "probe fixture", "dedup_key": f"{sym}:opening"}], run_id="fixture",
                        now=datetime(2026, 10, 3, 23, 0, tzinfo=lifelog.LOCAL_TZ).timestamp())


@pytest.mark.skipif(os.getenv("JARVIS_EXCHANGE_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("variant", ["plain", "stale_queue", "stale_portrait"])
@pytest.mark.parametrize("trial", [1, 2, 3])
def test_exchange(monkeypatch, tmp_path, caplog, trial, variant):
    from backend.brain import background, chronos, skills
    from backend.brain.portfolio_live_probe import _scratch
    from backend.storage import db, llm_calls, operations
    from backend.storage import portfolio as S
    from backend.tools import vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=90, cap="0.45")
    llm_calls._ensure()
    operations._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    _seed(chronos, P, S)
    monkeypatch.setattr(C, "_read_queue", AsyncMock(return_value=QUEUE + (STALE if variant == "stale_queue" else "")))
    if variant == "stale_portrait":       # his portrait sits in the stable block of every turn
        monkeypatch.setattr(C, "_read_owner", AsyncMock(return_value=PORTRAIT))
    for name, fn in (("skill", skills.skill), ("schedule_task", chronos.schedule_task),
                     ("check_background_tasks", background.check_background_tasks),
                     ("vault_read", vault.vault_read), ("vault_list", vault.vault_list)):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, fn)

    def fixed(text):
        async def tool(**kw):
            return text
        return tool
    for name, text in (("web_browse", WEB), ("fetch_page", PAGE)):
        monkeypatch.setitem(C.TOOL_DISPATCH, name, fixed(text))
    plans = []

    async def notify(text):
        plans.append(text)
        return True
    monkeypatch.setattr(C, "_plan_notifier", notify)
    caplog.set_level(logging.INFO)
    out = {"trial": trial, "variant": variant}

    async def exercise():
        history = []

        async def turn(key, text):
            async def send(event): pass
            run = f"ex-{trial}-{variant}-{key}"
            operations.current_run_id.set(run)
            caplog.clear()
            reply = await C.chat(text, history, send, channel="discord")
            footer = skills.pop_footer("discord")
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
            ops = db.connect().execute("SELECT tool, outcome, args_preview FROM operations WHERE run_id=? "
                                       "ORDER BY started_at", (run,)).fetchall()
            logs = [r.getMessage() for r in caplog.records
                    if "skill checks" in r.getMessage() or "unbacked" in r.getMessage()]
            out[key] = {"reply": reply, "footer": footer, "logs": logs,
                        "ops": [(o[0], o[1], o[2][:110]) for o in ops]}
            print(json.dumps({"trial": trial, "variant": variant, "turn": key, **out[key]}, ensure_ascii=False),
                  flush=True)

        await turn("update", UPDATE)
        out["update"]["plan_dms"] = list(plans)
        # The rest replays what he actually saw, so the next turns start from the same history.
        history[-1] = {"role": "assistant", "content": REPLY_1}
        await turn("readd", RE_ADD)
        await turn("goodnight", "good night")
        await providers.close_all()

    asyncio.run(exercise())
    lines = [t for t in (chronos._parse_task_line(l) for l in chronos._read_schedule()) if t]
    readd = out["readd"]["reply"]
    tool_less = [d for d in out["update"]["plan_dms"] if d.startswith("Working on") and "(" not in d.split("\n", 2)[-1]]
    verdict = {
        "update_no_tool_less_plan_dm": not tool_less,
        "readd_tied_all_four": sum(1 for t in lines if t["skill"] == "stock-research") == 4 and len(lines) == 4,
        # an actual case ("Bull case, as the runs will argue it:"), not a mention of the skill's steps
        "readd_no_bull_bear": not BULL_BEAR.search(readd),
        "readd_no_checks_fix": not any("draft misses" in l for l in out["readd"]["logs"]),
        "readd_footer_not_followed": "followed:" not in out["readd"]["footer"],
        "goodnight_no_empty_ledger": not EMPTY_LEDGER.search(out["goodnight"]["reply"]),
        "positions_intact": len(S.positions()) == 5,
    }
    print(json.dumps({"trial": trial, "variant": variant, "verdict": verdict,
                      "plan_dms": out["update"]["plan_dms"],
                      "lines": [(t["skill"], t["repeat"], str(t["due"]), t["tz"], t["task"][:50]) for t in lines],
                      "requests": state["requests"]}, ensure_ascii=False), flush=True)


SAID_LIVE = ("Good night, Sam.\n\nInterview at 10:30. The prep is where you left it, and it will still be there "
             "in the morning.\n\nOne thing, held for when you wake: the ledger is empty. Your message before last "
             "apparently never reached me in a form the tool would take, so no positions were recorded. Say the "
             "five lines again in one sentence when you are up and I will write them straight from that.")


@pytest.mark.skipif(os.getenv("JARVIS_EXCHANGE_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3])
def test_goodnight_draft_is_checked_against_the_ledger(monkeypatch, tmp_path, caplog, trial):
    """The step the replay could not reach (the server's stale note is not in the scratch world):
    his real goodnight reply as JARVIS's first draft, then the real claim check and correction
    pass, with the five positions in the ledger."""
    from backend.brain import chronos
    from backend.brain.portfolio_live_probe import _scratch
    from backend.storage import llm_calls, operations
    from backend.storage import portfolio as S
    from backend.tools import vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=20, cap="0.15")
    llm_calls._ensure()
    operations._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    _seed(chronos, P, S)
    real = C._chat_compat
    first = {"on": True}

    async def compat(*a, **k):
        if first["on"]:
            first["on"] = False
            return SAID_LIVE
        return await real(*a, **k)
    monkeypatch.setattr(C, "_chat_compat", compat)
    caplog.set_level(logging.WARNING, logger="backend.brain.engine")
    out = {}

    async def exercise():
        async def send(event): pass
        operations.current_run_id.set(f"gn-{trial}")
        history = [{"role": "user", "content": RE_ADD}, {"role": "assistant", "content": "Done: all four re-added."}]
        out["reply"] = await C.chat("good night", history, send, channel="discord")
        await providers.close_all()

    asyncio.run(exercise())
    warnings = [r.getMessage() for r in caplog.records if "unbacked" in r.getMessage()]
    verdict = {"flagged": any("portfolio ledger holds 5" in w for w in warnings),
               "corrected": not EMPTY_LEDGER.search(out["reply"]),
               "no_note": C._LEDGER_NOTE.strip() not in out["reply"],
               "positions_intact": len(S.positions()) == 5}
    print(json.dumps({"trial": trial, "verdict": verdict, "reply": out["reply"], "warnings": warnings},
                     ensure_ascii=False), flush=True)


# What recall brought back on the server (5 October): "good night" scores 0.847 against the 3 October
# goodnight exchange (cross-session floor 0.75) and the reranker keeps it at 4.46 (cut-off -3). That
# turn was deep, because the update paragraph's length made the next two turns deep.
OCT3_GOODNIGHT = ("i'm still really tired so i'm going to sleep now, good night\n"
                  "Good night. One thing before you sleep, and it matters. I told you the five positions were "
                  "recorded. They were not. Neither call succeeded, and nothing is in the ledger. That was exactly "
                  "the failure shape I keep flagging -- the sentence written from intent rather than from a tool "
                  "result. Send the five lines again in one sentence when you're up and I'll record them straight "
                  "from that.")


@pytest.mark.skipif(os.getenv("JARVIS_EXCHANGE_LIVE") != "1", reason="explicit paid probe only")
@pytest.mark.parametrize("trial", [1, 2, 3, 4, 5])
def test_goodnight_with_the_old_goodnight_recalled(monkeypatch, tmp_path, caplog, trial):
    """The server's goodnight as it actually ran: a deep turn whose recall held the 3 October
    goodnight exchange. Judged by whether the DRAFT said the ledger was empty (the claim check's
    warning) and whether the reply he would get says so."""
    from backend.brain import chronos
    from backend.brain.portfolio_live_probe import _scratch
    from backend.storage import llm_calls, operations
    from backend.storage import portfolio as S
    from backend.tools import vault
    C, P, providers, state = _scratch(monkeypatch, tmp_path, limit=20, cap="0.15")
    llm_calls._ensure()
    operations._ensure()
    _migrated(vault._VAULT)
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    _seed(chronos, P, S)
    monkeypatch.setattr(C, "_resolve_deep", lambda *a: True)
    monkeypatch.setattr(C, "recall", AsyncMock(return_value=C._RecallResult(exchange=OCT3_GOODNIGHT)))
    caplog.set_level(logging.WARNING, logger="backend.brain.engine")
    out = {}

    async def exercise():
        async def send(event): pass
        operations.current_run_id.set(f"gnr-{trial}")
        history = [{"role": "user", "content": UPDATE}, {"role": "assistant", "content": REPLY_1},
                   {"role": "user", "content": RE_ADD},
                   {"role": "assistant", "content": "Done: all four re-added against stock-research, same times."}]
        out["reply"] = await C.chat("good night", history, send, channel="discord")
        await providers.close_all()

    asyncio.run(exercise())
    warnings = [r.getMessage() for r in caplog.records if "unbacked" in r.getMessage()]
    dropped = [r.getMessage()[:200] for r in caplog.records if "left out and suppressed" in r.getMessage()]
    from backend.storage import fact_corrections
    from backend.brain.memory import USER_ID
    verdict = {"memory_dropped": bool(dropped),
               "suppressed_for_later": bool(fact_corrections.list_for_user(USER_ID)),
               "draft_said_empty": any("portfolio ledger holds 5" in w for w in warnings),
               "reply_says_empty": bool(EMPTY_LEDGER.search(out["reply"])),
               "reply_mentions_a_correction": bool(re.search(r"\bcorrect(ion|ing)\b|I was wrong", out["reply"], re.I))}
    print(json.dumps({"trial": trial, "verdict": verdict, "reply": out["reply"], "warnings": warnings},
                     ensure_ascii=False), flush=True)

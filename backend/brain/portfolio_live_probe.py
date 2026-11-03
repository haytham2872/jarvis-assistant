"""Optional small real-model probe. NEVER part of normal pytest discovery.

JARVIS_PORTFOLIO_LIVE=1 python -m pytest backend/brain/portfolio_live_probe.py -s -q
Explicit entry points: test_three_live_portfolio_turns, and test_review_fix_turns (3 October
2026: record, correct, confirm a reported split, read back; three trials), and the night of
3 October: test_goodnight_after_recording (his three real messages replayed) and
test_goodnight_correction_pass (a true goodnight draft, then the real correction pass).
Three turns, at most eight completion requests, $0.10 preflight ceiling. The real
chat, prompt, schemas, policy, claim check and Jev run against a scratch world.
No real holdings, vault, mail, calendar, memories, or notifiers can be changed.
"""
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.skipif(os.getenv("JARVIS_PORTFOLIO_LIVE")!="1",reason="explicit paid probe only")
def test_three_live_portfolio_turns(monkeypatch,tmp_path):
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2]/".env")
    from backend.brain import engine as C, router, portfolio as P, personality, threads, turn_signals
    from backend.providers import gateway as providers
    from backend.storage import portfolio as S, llm_calls, operations
    from backend.tools import vault

    assert router.OPENROUTER.configured, "OpenRouter key is not configured"
    monkeypatch.setenv("JARVIS_JEV","1")
    scratch=tmp_path/"vault"
    scratch.mkdir()
    monkeypatch.setattr(vault,"_VAULT",scratch)
    monkeypatch.setattr(C.intent,"classify",AsyncMock(return_value=None))
    monkeypatch.setattr(C,"assign_session",AsyncMock(return_value=threads.SessionDecision("portfolio-probe",None)))
    monkeypatch.setattr(C,"recall",AsyncMock(return_value=C._RecallResult()))
    for name in ("_read_owner","_read_latest_letter","_read_queue","_read_relevant_topic"):
        monkeypatch.setattr(C,name,AsyncMock(return_value=""))
    monkeypatch.setattr(C,"after_turn",lambda *a:None)
    monkeypatch.setattr(C,"get_mood",lambda:None)
    monkeypatch.setattr(personality,"_mood_nudge",lambda:"")
    for name in ("_ingest_mem","persist_mood","_record_thread_task"):
        monkeypatch.setattr(C,name,AsyncMock())
    monkeypatch.setattr(C,"_resolve_deep",lambda *a:False)
    monkeypatch.setattr(P,"_rates",AsyncMock())
    monkeypatch.setattr(C,"TOOL_DISPATCH",{
        name:P.portfolio if name=="portfolio" else AsyncMock(return_value="Error: outside tool disabled in scratch probe")
        for name in C.TOOL_DISPATCH})
    monkeypatch.setattr(C,"_OUTPUT_LIMITS",dict.fromkeys(C._OUTPUT_LIMITS,4096))
    monkeypatch.setattr(C,"_DEFAULT_OUTPUT_LIMIT",4096)

    report_path=Path(__file__).resolve().parents[2]/"docs/portfolio-source-measurement.json"
    measured=json.loads(report_path.read_text(encoding="utf-8"))
    feed=next(r for r in measured["results"] if r["symbol"]=="NVDA" and r["source"]=="nasdaq")
    close=feed["closes"][-1]
    fx=next(r for r in measured["results"] if r["source"]=="ecb")
    S.save_fx([{"pair":"USD/EUR","day":r["date"],"rate":r["close"],"source":"ECB reference","url":fx["url"]} for r in fx["closes"]])

    async def exercise():
        http=providers.get_http_client()
        models=(await http.get(router.OPENROUTER.base_url.rstrip("/")+"/models",timeout=15)).json()["data"]
        prices={m["id"]:m.get("pricing",{}) for m in models}
        real=providers.get_async_client(router.OPENROUTER)
        real.max_retries=0
        requests=0
        reserved=Decimal(0)
        llm_calls._ensure()
        async def create(**kwargs):
            nonlocal requests,reserved
            if requests>=8:
                raise RuntimeError("live completion request limit reached")
            kwargs["max_tokens"]=min(int(kwargs.get("max_tokens",4096)),4096)
            p=prices.get(kwargs["model"])
            if not p or not p.get("prompt") or not p.get("completion"):
                raise RuntimeError("could not verify model pricing; no paid request made")
            # Conservative ASCII estimate and maximum output, without assuming cache hits.
            input_bound=len(json.dumps([kwargs.get("messages",[]),kwargs.get("tools",[])],ensure_ascii=True))//2+2000
            bound=Decimal(p["prompt"])*input_bound+Decimal(p["completion"])*kwargs["max_tokens"]
            meter=S.connection().execute("SELECT sum(cost),count(*),count(cost) FROM llm_calls").fetchone()
            spent=Decimal(str(meter[0] or 0)) if meter[1]==requests and meter[2]==requests else reserved
            if spent+bound>Decimal("0.10"):
                raise RuntimeError("live probe spending ceiling reached")
            reserved+=bound
            requests+=1
            print(f"Paid completion {requests}/8; spent-or-reserved plus this request <= ${spent+bound:.4f} (cap $0.10)",flush=True)
            return await real.chat.completions.create(**kwargs)
        proxy=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(C,"_get_compat_client",lambda:proxy)
        history=[]
        turns=["I have 200 NVDA at 118. Record my position.",
               "You previously guessed I hold 50 TTWO at 160. Do not record that guess as my holding.",
               "How is my NVDA position doing? Give the ledger's USD value and USD P&L as of the recorded close."]
        replies=[]
        for n,text in enumerate(turns):
            events=[]
            async def send(event): events.append(event)
            operations.current_run_id.set(f"probe-{n}")
            reply=await C.chat(text,history,send,channel="discord")
            await asyncio.sleep(0)
            history.extend([{"role":"user","content":text},{"role":"assistant","content":reply}])
            print(json.dumps({"turn":n+1,"reply":reply,"tools":[e for e in events if e.get("type")=="tool_call"]},ensure_ascii=False),flush=True)
            replies.append(reply)
            assert not __import__("re").search(r"not financial advice|not (?:a |your )?financial advis[eo]r|consult.{0,30}financial",reply,__import__("re").I)
            if n==0:
                assert S.positions("NVDA")[0]["shares"]=="200"
                S.save_mark("NVDA",{"day":close["date"],"close":close["close"],"currency":"USD","source":"nasdaq","url":feed["url"]})
            if n==1:
                assert not S.history("TTWO")["trades"]
            if n==2:
                # A fresh, code-computed portfolio context also legitimately answers
                # this read. Requiring an extra tool call would waste model budget.
                assert P.context_text(text)
                plain=reply.replace(",","")
                assert P.money(Decimal(close["close"])*200).replace(",","") in plain
                assert P.money((Decimal(close["close"])-118)*200).replace(",","") in plain
        conn=S.connection()
        cost=conn.execute("SELECT sum(cost),count(*) FROM llm_calls").fetchone()
        report={"synthetic_scratch_world":True,"completion_requests":requests,"reported_llm_cost":cost[0],
                "recorded_llm_calls":cost[1],"per_run_preflight_cap_usd":"0.10","source_close":close,
                "checks":["quoted holding recorded","assistant guess not imported","dated USD value/P&L exact","no canned disclaimer"]}
        (report_path.parent/'portfolio-live-probe.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report),flush=True)
        await providers.close_all()
    asyncio.run(exercise())


def _scratch(monkeypatch, tmp_path, *, limit, cap):
    """The same scratch world as above: real chat, prompt, schemas, policy, claim check and
    Jev; only the portfolio tool is live, everything else returns an error."""
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2]/".env")
    from backend.brain import engine as C, router, portfolio as P, personality, threads
    from backend.providers import gateway as providers
    from backend.tools import vault
    assert router.OPENROUTER.configured, "OpenRouter key is not configured"
    monkeypatch.setenv("JARVIS_JEV","1")
    scratch=tmp_path/"vault"
    scratch.mkdir()
    monkeypatch.setattr(vault,"_VAULT",scratch)
    monkeypatch.setattr(C.intent,"classify",AsyncMock(return_value=None))
    monkeypatch.setattr(C,"assign_session",AsyncMock(return_value=threads.SessionDecision("portfolio-probe",None)))
    monkeypatch.setattr(C,"recall",AsyncMock(return_value=C._RecallResult()))
    for name in ("_read_owner","_read_latest_letter","_read_queue","_read_relevant_topic"):
        monkeypatch.setattr(C,name,AsyncMock(return_value=""))
    monkeypatch.setattr(C,"after_turn",lambda *a:None)
    monkeypatch.setattr(C,"get_mood",lambda:None)
    monkeypatch.setattr(personality,"_mood_nudge",lambda:"")
    for name in ("_ingest_mem","persist_mood","_record_thread_task"):
        monkeypatch.setattr(C,name,AsyncMock())
    monkeypatch.setattr(C,"_resolve_deep",lambda *a:False)
    monkeypatch.setattr(P,"_rates",AsyncMock())
    monkeypatch.setattr(C,"TOOL_DISPATCH",{
        name:P.portfolio if name=="portfolio" else AsyncMock(return_value="Error: outside tool disabled in scratch probe")
        for name in C.TOOL_DISPATCH})
    monkeypatch.setattr(C,"_OUTPUT_LIMITS",dict.fromkeys(C._OUTPUT_LIMITS,4096))
    monkeypatch.setattr(C,"_DEFAULT_OUTPUT_LIMIT",4096)
    state={"requests":0,"cost":Decimal(0)}
    real=providers.get_async_client(router.OPENROUTER)
    real.max_retries=0
    async def create(**kwargs):
        if state["requests"]>=limit:
            raise RuntimeError("live completion request limit reached")
        kwargs["max_tokens"]=min(int(kwargs.get("max_tokens",4096)),4096)
        # What the recorded calls actually cost so far, plus a conservative bound on this one
        # ($1.20/Mtok output at full length, $0.30/Mtok on a generous input estimate).
        from backend.storage import db
        spent=Decimal(str(db.connect().execute("SELECT coalesce(sum(cost),0) FROM llm_calls").fetchone()[0]))
        bound=Decimal("0.0000012")*kwargs["max_tokens"]+Decimal("0.0000003")*(len(json.dumps([kwargs.get("messages",[]),kwargs.get("tools",[])]))//2+2000)
        if spent+bound>Decimal(cap):
            raise RuntimeError("live probe spending ceiling reached")
        state["cost"]=spent+bound
        state["requests"]+=1
        return await real.chat.completions.create(**kwargs)
    proxy=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(C,"_get_compat_client",lambda:proxy)
    return C,P,providers,state


@pytest.mark.skipif(os.getenv("JARVIS_PORTFOLIO_LIVE")!="1",reason="explicit paid probe only")
@pytest.mark.parametrize("trial",[1,2,3])
def test_review_fix_turns(monkeypatch,tmp_path,trial):
    """3 October 2026: a phrasing the first version refused, a correction, a reported split
    confirmed in words, then a read. Judged from the ledger, not from the replies."""
    from datetime import date, timedelta
    from backend.storage import portfolio as S, lifelog, operations
    C,P,providers,state=_scratch(monkeypatch,tmp_path,limit=20,cap="0.10")
    from backend.storage import llm_calls
    llm_calls._ensure()
    import re as _re
    disclaimer=_re.compile(r"not financial advice|not (?:a |your )?financial advis[eo]r|consult.{0,30}financial",_re.I)
    async def exercise():
        history=[]
        async def turn(n,text):
            events=[]
            async def send(event): events.append(event)
            operations.current_run_id.set(f"fix-probe-{trial}-{n}")
            reply=await C.chat(text,history,send,channel="discord")
            history.extend([{"role":"user","content":text},{"role":"assistant","content":reply}])
            tools=[e.get("input") for e in events if e.get("type")=="tool_call"]
            print(json.dumps({"trial":trial,"turn":n,"reply":reply,"tools":tools},ensure_ascii=False),flush=True)
            assert not disclaimer.search(reply)
            return reply
        await turn(1,"my NVDA position is 200 shares at 118")
        assert S.positions("NVDA")[0]["shares"]=="200"
        await turn(2,"that's wrong, it's 20 shares not 200, same price")
        assert S.positions("NVDA")[0]["shares"]=="20"
        assert sum(t["voided"] for t in S.trade_rows("NVDA"))==1
        tomorrow=(date.fromisoformat(lifelog.local_date())+timedelta(days=1)).isoformat()
        S.note_split("NVDA",{"day":tomorrow,"numerator":"10","denominator":"1","source":"yahoo",
                             "url":"https://query1.finance.yahoo.com/v8/finance/chart/NVDA"})
        await turn(3,"yes, record the NVDA split")
        assert S.positions("NVDA")[0]["shares"]=="200"
        assert S.splits("NVDA")[0]["state"]=="confirmed"
        reply=await turn(4,"how many NVDA shares do I have now?")
        assert "200" in reply or "two hundred" in reply.lower()     # live: "Two hundred."
        spent=S.connection().execute("SELECT sum(cost),count(*) FROM llm_calls").fetchone()
        print(json.dumps({"trial":trial,"completion_requests":state["requests"],"reported_cost_usd":spent[0],
                          "recorded_calls":spent[1]}),flush=True)
        await providers.close_all()
    asyncio.run(exercise())


_HOLDINGS_MSG = ("the fingers good thank you for asking, now Here are my current holdings from my broker app. "
                 "Record them in the portfolio ledger as opening positions and treat the ledger as the reference "
                 "for my portfolio from now on. I hold 1.14339682 NVDA at an average price of $228.68; 0.24335794 "
                 "TTWO at an average price of $238.13; 0.02822711 TSLA at an average price of $426.89; 2.2683417 "
                 "QNT at an average price of $49.75; 1.68563049 SPCX at an average price of $148.52. Any older "
                 "entry for these in the ledger is wrong, so correct it to match instead of adding to it.")


@pytest.mark.skipif(os.getenv("JARVIS_PORTFOLIO_LIVE")!="1",reason="explicit paid probe only")
@pytest.mark.parametrize("trial",[1,2,3])
def test_goodnight_after_recording(monkeypatch,tmp_path,caplog,trial):
    """3 October 2026, live on Discord: the holdings were recorded, then the reply to "good
    night" retracted them ("They were not. Neither call succeeded"), and the next turn drew the
    system-check note again. Replays those three messages; judged from the ledger, the replies
    and the claim-check warnings."""
    import logging, re as _re
    from backend.storage import portfolio as S, operations, db
    C,P,providers,state=_scratch(monkeypatch,tmp_path,limit=30,cap="0.15")
    from backend.storage import llm_calls
    llm_calls._ensure()
    caplog.set_level(logging.WARNING, logger="backend.brain.engine")
    async def identify(symbol):         # QNT/SPCX without a network lookup (verified live 3 October)
        extra={"QNT":"Quantinuum Inc.","SPCX":"Space Exploration Technologies Corp."}
        if str(symbol).upper() in extra:
            s=str(symbol).upper()
            return {"symbol":s,"exchange":"NASDAQ","currency":"USD","price_key":s,"name":extra[s]}
        return P.resolve_instrument(symbol)
    monkeypatch.setattr(P,"identify_instrument",identify)
    out={"trial":trial,"turns":[]}
    async def exercise():
        history=[]
        async def turn(n,text):
            events=[]
            async def send(event): events.append(event)
            run=f"goodnight-{trial}-{n}"
            operations.current_run_id.set(run)
            caplog.clear()
            reply=await C.chat(text,history,send,channel="discord")
            history.extend([{"role":"user","content":text},{"role":"assistant","content":reply}])
            ops=db.connect().execute("SELECT tool,outcome,args_preview,result_preview FROM operations "
                                     "WHERE run_id=? ORDER BY started_at",(run,)).fetchall()
            warnings=[r.getMessage() for r in caplog.records if "unbacked" in r.getMessage() or "confabulated" in r.getMessage()]
            out["turns"].append({"turn":n,"reply":reply,"warnings":warnings,
                                 "ops":[(o[0],o[1],o[2][:160],o[3][:160]) for o in ops],
                                 "positions":len(S.positions())})
            print(json.dumps(out["turns"][-1],ensure_ascii=False),flush=True)
            return reply,warnings
        await turn(1,_HOLDINGS_MSG)
        assert len(S.positions())==5
        night,w2=await turn(2,"i'm still really tired so i'm going to sleep now, good night")
        third,w3=await turn(3,"I hold 1.14339682 NVDA at 228.68, 0.24335794 TTWO at 238.13, 0.02822711 TSLA at 426.89, "
                              "2.2683417 QNT at 49.75, 1.68563049 SPCX at 148.52")
        assert len(S.positions())==5 and all(Decimal(p["shares"])>0 for p in S.positions())
        assert sum(1 for t in S.trade_rows("NVDA") if not t["voided"])==1      # never double-counted
        assert C._UNBACKED_NOTE.strip() not in night and C._UNBACKED_NOTE.strip() not in third
        assert not _re.search(r"\b(were|was|are) not\b[^.]{0,30}\b(recorded|in the ledger)|nothing is in the ledger",night,_re.I)
        assert not w2 and not w3, (w2,w3)
        print(json.dumps({"trial":trial,"completion_requests":state["requests"]}),flush=True)
        await providers.close_all()
    asyncio.run(exercise())


@pytest.mark.skipif(os.getenv("JARVIS_PORTFOLIO_LIVE")!="1",reason="explicit paid probe only")
@pytest.mark.parametrize("trial",[1,2,3])
@pytest.mark.parametrize("new_session",[False,True],ids=["same-session","after-new"])
def test_goodnight_correction_pass(monkeypatch,tmp_path,caplog,trial,new_session):
    """The step the live run could not show: a TRUE goodnight line about the positions recorded
    a turn earlier ("The five positions are recorded"), as the first draft of turn 2, then the
    real correction pass. Live, that pass ended in "They were not ... nothing is in the ledger".
    Same session: the line is backed by the earlier recording and no pass runs. After /new it
    is not, the pass runs, and must check the ledger instead of retracting it."""
    import logging, re as _re
    from backend.storage import portfolio as S, operations, db
    C,P,providers,state=_scratch(monkeypatch,tmp_path,limit=30,cap="0.15")
    from backend.storage import llm_calls
    llm_calls._ensure()
    caplog.set_level(logging.WARNING, logger="backend.brain.engine")
    async def identify(symbol):
        extra={"QNT":"Quantinuum Inc.","SPCX":"Space Exploration Technologies Corp."}
        s=str(symbol).upper()
        if s in extra:
            return {"symbol":s,"exchange":"NASDAQ","currency":"USD","price_key":s,"name":extra[s]}
        return P.resolve_instrument(symbol)
    monkeypatch.setattr(P,"identify_instrument",identify)
    real_compat=C._chat_compat
    forced={"on":False}
    seen_warnings=[]
    from backend.brain import threads
    async def compat(*a,**k):
        if forced["on"]:
            forced["on"]=False
            return "Good night. The five positions are recorded and the ledger is the reference from here. Sleep well."
        return await real_compat(*a,**k)
    monkeypatch.setattr(C,"_chat_compat",compat)
    async def exercise():
        history=[]
        async def turn(n,text):
            async def send(event): pass
            run=f"gn-pass-{trial}-{n}"
            operations.current_run_id.set(run)
            caplog.clear()
            reply=await C.chat(text,history,send,channel="discord")
            history.extend([{"role":"user","content":text},{"role":"assistant","content":reply}])
            ops=db.connect().execute("SELECT tool,outcome,args_preview,result_preview FROM operations "
                                     "WHERE run_id=? ORDER BY started_at",(run,)).fetchall()
            warnings=[r.getMessage() for r in caplog.records if "unbacked" in r.getMessage()]
            seen_warnings[:]=warnings
            print(json.dumps({"trial":trial,"new_session":new_session,"turn":n,"reply":reply,"warnings":warnings,
                              "ops":[(o[0],o[1],o[2][:120],o[3][:140]) for o in ops]},ensure_ascii=False),flush=True)
            return reply
        await turn(1,_HOLDINGS_MSG)
        assert len(S.positions())==5
        forced["on"]=True
        if new_session:
            monkeypatch.setattr(C,"assign_session",AsyncMock(return_value=threads.SessionDecision("portfolio-probe-2",None)))
        night=await turn(2,"i'm still really tired so i'm going to sleep now, good night")
        assert len(S.positions())==5
        assert bool(seen_warnings)==new_session, seen_warnings
        assert C._UNBACKED_NOTE.strip() not in night
        assert not _re.search(r"\b(were|was|are) not\b[^.]{0,30}\b(recorded|in the ledger)|nothing is in the ledger|not recorded",night,_re.I)
        await providers.close_all()
    asyncio.run(exercise())

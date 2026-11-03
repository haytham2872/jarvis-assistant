"""Exactness and provenance checks with the model/world faked; zero API spend."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.brain import portfolio as P
from backend.market import data
from backend.storage import db, portfolio as S, watches as W, forecasts as F
from backend.tools import policy, gateway


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def rates(day):
        return None
    async def no_splits(i, since):
        return []
    monkeypatch.setattr(P, "_rates", rates)
    monkeypatch.setattr(data, "get_splits", no_splits)
    monkeypatch.setattr(P, "_split_next_try", {})
    monkeypatch.setattr(P, "_notifier", None)
    monkeypatch.setattr(P, "_cycle_lock", asyncio.Lock())
    policy.start_scope([])


def trade(**kw):
    return {"symbol": "NVDA", "side": "opening", "shares": "200", "price": "118",
            "quote": "I have 200 NVDA at 118", **kw}


def call_trade(message, **kw):
    policy.start_scope([message])
    return run(P.portfolio("record_trade", trades=[trade(quote=message, **kw)]))


def seed(symbol="NVDA", side="buy", qty="10", price="100", fees="0", day="2026-09-01", key=None):
    i = P.resolve_instrument(symbol)
    return S.record_trades([{"instrument": i, "side": side, "shares": qty, "price": price,
                            "currency": i["currency"], "fees": fees, "native_price": price,
                            "native_fees": fees, "traded_on": day, "quote": "synthetic user fixture",
                            "dedup_key": key or f"{symbol}:{side}:{qty}:{price}:{day}"}], run_id="fixture",
                           now=datetime(2026,9,1,tzinfo=data.LOCAL_TZ).timestamp())


def mark(close="150", day="2026-09-02", now=None):
    return S.save_mark("NVDA", {"day": day, "close": close, "currency": "USD",
                               "source": "fixture", "url": "https://example.com/close"}, now=now)


def test_verified_holding_and_no_assistant_import():
    result = call_trade("I have 200 NVDA at 118")
    assert result.startswith("Confirmed:")
    row = S.history("NVDA")["trades"][0]
    assert row["quote"] == "I have 200 NVDA at 118"
    assert row["shares"] == "200"
    policy.start_scope(["how much do I hold?"])
    # The same figures again, from words not in this message: reported as already there
    # (3 October 2026), never recorded twice.
    again = run(P.portfolio("record_trade", trades=[trade()]))
    assert again.startswith("Already in the ledger") and "#1" in again
    rejected = run(P.portfolio("record_trade", trades=[trade(shares="300", quote="I have 300 NVDA at 118")]))
    assert rejected.startswith("Error:")
    assert len(S.history("NVDA")["trades"]) == 1


@pytest.mark.parametrize("message,changes", [
    ("I have 200 NVDA at 118", {"shares": "300"}),
    ("I have 200 NVDA at 118", {"price": "119"}),
    ("If I have 200 NVDA at 118", {}),
    ("You said I have 200 NVDA at 118", {}),
    ("I have 200 NVDA at 118?", {}),
    ("I have 200 NVDA at 118 and 50 TTWO at 160", {"shares": "50", "price": "160"}),
    ("I have 200 NVDA at 118", {"side": "sell"}),
    ("I have 200 NVDA at €118", {"currency": "USD"}),
    ("I have 200 NVDA at 118", {"currency": "EUR"}),
    ("I have 200 NVDA at 118", {"fees": "10"}),
    ("I have 200 NVDA at 118", {"traded_on": "2020-01-01"}),
])
def test_figures_side_currency_and_date_must_be_his(message, changes):
    assert call_trade(message, **changes).startswith("Error:")
    assert not S.history("NVDA")["trades"]


def test_padding_a_real_quote_does_not_verify_invented_numbers():
    policy.start_scope(["yes thanks"])
    assert run(P.portfolio("record_trade", trades=[trade(quote="yes thanks ... I have 200 NVDA at 118")])).startswith("Error:")


def test_invented_quote_cannot_create_the_first_holding():
    policy.start_scope(['How much do I hold?'])
    assert run(P.portfolio('record_trade',trades=[trade()])).startswith('Error:')
    assert not S.history('NVDA')['trades']


def test_sentence_punctuation_in_a_real_quote_is_accepted():
    assert call_trade("I have 200 NVDA at 118. Record my position.").startswith("Confirmed:")


def test_two_holdings_and_atomic_rejection():
    message = "I have 200 NVDA at 118 and 50 Take-Two at 160"
    policy.start_scope([message])
    rows = [trade(quote=message), trade(symbol="Take-Two", shares="50", price="160", quote=message)]
    assert run(P.portfolio("record_trade", trades=rows)).startswith("Confirmed:")
    assert [(p["symbol"],p["shares"]) for p in S.positions()] == [("NVDA","200"),("TTWO","50")]
    policy.start_scope(["I sold 10 NVDA at 150 and 100 TTWO at 170"])
    rows = [trade(side="sell",shares="10",price="150",quote=policy.owner_words()[0]),
            trade(symbol="TTWO",side="sell",shares="100",price="170",quote=policy.owner_words()[0])]
    assert run(P.portfolio("record_trade", trades=rows)).startswith("Error:")
    assert len(S.history("NVDA")["trades"])==1


def test_native_trade_survives_fx_outage_but_eur_value_is_unavailable(monkeypatch):
    async def fail(day):
        raise ValueError("FX down")
    monkeypatch.setattr(P, "_rates", fail)
    assert call_trade("I have 200 NVDA at 118").startswith("Confirmed:")
    assert S.positions()[0]["cost_base"] is None
    assert "EUR value unavailable" in P.show_text()


def test_opening_balance_repetition_cannot_double_holdings():
    assert call_trade("I have 200 NVDA at 118").startswith("Confirmed:")
    assert call_trade("I have 200 NVDA at 118").startswith("Already in the ledger")
    assert call_trade("I hold 200 NVDA at 118").startswith("Already in the ledger")   # restated
    assert call_trade("I hold 300 NVDA at 118", shares="300").startswith("Error:")     # a second opening
    assert S.positions()[0]["shares"]=="200" and len(S.history("NVDA")["trades"]) == 1


def test_partial_sale_split_fees_and_decimal_views():
    seed(fees="10")
    seed(qty="10",price="200",day="2026-09-02")
    seed(side="sell",qty="5",price="250",fees="5",day="2026-09-03")
    seed(side="split",qty="2",price="0",day="2026-09-04")
    S.save_fx([{"pair":"USD/EUR","day":f"2026-09-0{d}","rate":"0.9","source":"fixture","url":"https://example.com/fx"} for d in range(1,6)])
    mark("90", "2026-09-05")
    p = S.positions()[0]
    assert p["shares"]=="30"
    assert Decimal(p["average_cost"]) == Decimal("75.25")
    assert Decimal(p["cost_basis"]) == Decimal("2257.5")
    assert Decimal(p["realised"]) == Decimal("492.5")
    assert Decimal(p["unrealised"]) == Decimal("442.5")
    assert Decimal(p["value_base"]) == Decimal("2430")
    assert Decimal(p["unrealised_base"]) == Decimal("398.25")


def test_backdated_sell_validates_the_entire_history_and_rolls_back():
    seed()
    seed(side="sell",qty="5",day="2026-09-10")
    with pytest.raises(ValueError):
        seed(side="sell",qty="6",day="2026-09-05")
    assert len(S.history("NVDA")["trades"])==2


@pytest.mark.parametrize("table", ["trades","marks","fx","theses","thesis_events"])
def test_facts_cannot_be_updated_or_deleted(table):
    conn = S.connection()
    if table in ("theses","thesis_events"):
        S.set_thesis(P.resolve_instrument("NVDA"),"His actual reason","owner","His actual reason","")
    for verb in ("DELETE FROM", "UPDATE"):
        sql = f"{verb} {table}" + (" SET ts=0" if verb=="UPDATE" else "")
        # SQLite fires triggers per row, so put a real fact in each target.
        if table=="trades": seed()
        if table=="marks": seed(); mark()
        if table=="fx": S.save_fx([{"pair":"USD/EUR","day":"2026-09-01","rate":"0.9","source":"x","url":"https://example.com"}])
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with conn:
                conn.execute(sql)


def test_no_fuzzy_injection_on_unrelated_turn_and_bounded_context():
    seed()
    assert P.context_text("hey how are you") == ""
    assert "NVDA" in P.context_text("how is my NVDA position doing")
    assert len(P.context_text("my portfolio")) <= 1200


def thesis():
    q = "My NVDA thesis is datacenter demand keeps growing"
    policy.start_scope([q])
    result = run(P.portfolio("set_thesis",symbol="NVDA",why="datacenter demand keeps growing",quote=q))
    assert result.startswith("Confirmed:")
    return S.current_theses()[0]["id"]


def test_his_reasons_and_model_analysis_stay_separate():
    tid = thesis()
    assert run(P.portfolio("set_thesis",symbol="NVDA",why="margins will double",quote="My NVDA thesis is datacenter demand keeps growing")).startswith("Error:")
    assert run(P.portfolio("set_thesis",symbol="NVDA",why="Margins may face pressure",source="my analysis")).startswith("Confirmed:")
    assert {t["source"] for t in S.current_theses()} == {"owner","my analysis"}
    assert tid


def test_price_condition_records_before_delivery_and_uses_watch_recovery():
    tid = thesis()
    q = "wrong if NVDA closes below 180"
    policy.start_scope([q])
    response = run(P.portfolio("add_condition",thesis_id=tid,kind="price_below",level="180",quote=q))
    assert response.startswith("Confirmed:")
    c = S.conditions()[0]
    assert W.get(c["watch_id"])["method"]=="portfolio_close"
    assert not W.due(time_now(),10)
    mark("170", lifelog_today(), now=time_now()+1)
    run(P.check_conditions(time_now()+2))
    assert W.get(c["watch_id"])["alert_state"]=="parked"
    assert len(S.history("NVDA")["events"])==3
    run(P.check_conditions(time_now()+3))
    assert len(W.pop_parked())==1
    assert W.pop_parked()==[]
    assert len(S.history("NVDA")["events"])==3


def time_now():
    import time
    return time.time()


def lifelog_today():
    from backend.storage import lifelog
    return lifelog.local_date()


def test_simulated_fortnight_handles_dst_once_per_session(monkeypatch):
    from backend.brain import pause
    monkeypatch.setattr(pause,"is_paused",lambda *a,**kw:False)
    start=datetime(2026,10,19,12,tzinfo=data.LOCAL_TZ)
    with S.connection() as conn:
        S.ensure_instrument(P.resolve_instrument("NVDA"),now=start.timestamp())
    async def close(i,day):
        return {"day":day,"close":"200","currency":"USD","source":"fixture","url":"https://example.com"}
    monkeypatch.setattr(data,"get_closes",close)
    async def no_reviews(now): pass
    monkeypatch.setattr(P,"reviews_cycle",no_reviews)
    i=S.instruments()[0]
    assert data.fetch_at(i,"2026-10-23").strftime("%H:%M")=="22:20"
    assert data.fetch_at(i,"2026-10-26").strftime("%H:%M")=="21:20"
    assert data.fetch_at(i,"2026-11-02").strftime("%H:%M")=="22:20"
    for d in range(15):
        current=start+timedelta(days=d)
        run(P.cycle(current.replace(hour=20,minute=30)))
        assert all(m["local_date"]<current.date().isoformat() for m in S.history("NVDA")["marks"])
        for _ in range(2): run(P.cycle(current.replace(hour=23,minute=0)))
    marks=S.history("NVDA")["marks"]
    assert len(marks)==len(data.sessions(i,"2026-10-19","2026-11-02"))==11


def test_market_holidays_and_early_closes():
    i=P.resolve_instrument("NVDA")
    assert data.fetch_at(i,"2026-12-25") is None
    assert data.fetch_at(i,"2026-11-27").strftime("%H:%M")=="19:20"  # 13:00 NY


def test_total_source_failure_leaves_no_invented_mark(monkeypatch):
    from backend.brain import pause
    monkeypatch.setattr(pause,"is_paused",lambda *a,**kw:False)
    seed()
    async def fail(i,day): raise ValueError("all sources failed")
    monkeypatch.setattr(data,"get_closes",fail)
    async def no_reviews(now): pass
    monkeypatch.setattr(P,"reviews_cycle",no_reviews)
    run(P.cycle(datetime(2026,9,2,23,tzinfo=data.LOCAL_TZ)))
    assert not S.history("NVDA")["marks"]
    assert S.connection().execute("SELECT count(*) FROM portfolio_fetches WHERE state='missing'").fetchone()[0]==1
    assert "missing mark" in str(P.scheduled_items(datetime(2026,9,3,tzinfo=data.LOCAL_TZ).timestamp()))


def test_gateway_read_cannot_back_trade_claim():
    from backend.brain import engine as C
    C._turn_tools_ok.clear()
    C._turn_tools_ok.add("portfolio:show")
    # Persistence request: a read cannot count as recording a requested trade.
    problems=C._unbacked_claims("Recorded your position.",C._turn_tools_ok,1,asked_to_save=True)
    assert problems


def test_thesis_links_are_idempotent_and_atomic():
    tid=thesis()
    q="wrong if datacenter revenue growth falls below 40% on 2026-11-19"
    policy.start_scope([q])
    args=dict(thesis_id=tid,kind="metric",criterion="datacenter revenue growth falls below 40%",due_date="2026-11-19",quote=q)
    assert run(P.portfolio("add_condition",**args)).startswith("Confirmed:")
    assert run(P.portfolio("add_condition",**args)).startswith("Confirmed:")
    assert len(S.conditions())==1
    assert len(W.list_active())==1
    assert len(S.history("NVDA")["events"])==2


def test_condition_and_alert_rollback_together(monkeypatch):
    tid=thesis()
    q="wrong if NVDA closes below 180"
    policy.start_scope([q])
    monkeypatch.setattr(W,"add",lambda **kw:None)
    assert run(P.portfolio("add_condition",thesis_id=tid,kind="price_below",level="180",quote=q)).startswith("Error:")
    assert not S.conditions()


def test_trade_tool_not_available_to_unattended_workers():
    """Since 2026-10-04 a job may READ the ledger (a stock-research routine starts there),
    never write it: the job is offered show/history/record only, and both the job's executor
    and the portfolio itself refuse a recording."""
    from backend.brain import background
    assert background._BG_ACTIONS["portfolio"] == ("show", "history", "record")
    assert run(background._bg_exec("portfolio", {"action": "record_trade", "trades": [trade()]})).startswith("Error:")
    with policy.job_scope("background"):
        assert run(P.portfolio("record_trade",trades=[trade()])).startswith("Error:")


def test_daily_profit_change_excludes_deposits_and_partial_sells():
    seed()
    S.save_fx([{"pair":"USD/EUR","day":f"2026-09-0{d}","rate":"1","source":"fixture","url":"https://example.com"} for d in range(1,5)])
    mark("100","2026-09-01")
    seed(qty="10",price="100",day="2026-09-02")
    mark("100","2026-09-02")
    seed(side="sell",qty="5",price="120",day="2026-09-03")
    mark("120","2026-09-03")
    rows=S.daily()
    assert Decimal(rows[1]["change_base"])==0
    assert Decimal(rows[2]["change_base"])==400
    views = [dict(r) for r in S.connection().execute("SELECT * FROM v_portfolio_daily ORDER BY local_date")]
    assert Decimal(views[2]["change_base"])==400


def test_us_cent_values_have_no_binary_rounding():
    seed(qty="3",price="0.1")
    assert S.positions()[0]["cost_basis"]=="0.3"


@pytest.mark.parametrize('message', ['I do not own 200 NVDA at 118','I never bought 200 NVDA at 118','If I bought 200 NVDA at 118'])
def test_denied_or_hypothetical_ownership_is_rejected(message):
    assert call_trade(message).startswith('Error:')


def test_retry_with_a_shorter_quote_and_decimal_format_is_the_same_trade():
    from backend.storage import operations
    seed()
    q='I bought 5 NVDA at 120. Please record it.'
    policy.start_scope([q])
    token=operations.current_run_id.set('same-retried-turn')
    try:
        assert run(P.portfolio('record_trade',trades=[trade(side='buy',shares='5',price='120',quote=q)])).startswith('Confirmed:')
        assert run(P.portfolio('record_trade',trades=[trade(side='buy',shares='5.0',price='120.00',quote='I bought 5 NVDA at 120.')])).startswith('Confirmed:')
    finally:
        operations.current_run_id.reset(token)
    assert S.positions()[0]['shares']=='15'


def test_revision_retains_conditions_and_closure_cancels_the_watch():
    tid=thesis()
    q='wrong if NVDA closes below 180'
    policy.start_scope([q])
    assert run(P.portfolio('add_condition',thesis_id=tid,kind='price_below',level='180',quote=q)).startswith('Confirmed:')
    policy.start_scope(['My NVDA reason now is recurring demand'])
    assert run(P.portfolio('revise_thesis',symbol='NVDA',thesis_id=tid,why='recurring demand',quote='My NVDA reason now is recurring demand')).startswith('Confirmed:')
    revised=S.current_theses()[0]
    assert len(S.active_conditions())==1 and revised['previous_id']==tid
    policy.start_scope(['Close my NVDA thesis'])
    assert run(P.portfolio('close_thesis',symbol='NVDA',thesis_id=revised['id'],quote='Close my NVDA thesis')).startswith('Confirmed:')
    assert not S.active_conditions()
    assert W.get(S.conditions()[0]['watch_id'])['state']==W.CANCELLED
    assert len(S.history('NVDA')['theses'])==3


def dated_condition(monkeypatch):
    stamp=datetime(2026,10,1,12,tzinfo=data.LOCAL_TZ).timestamp()
    monkeypatch.setattr(P.time,'time',lambda:stamp)
    tid=thesis()
    q='wrong if revenue falls below 40% on 2026-10-02'
    policy.start_scope([q])
    assert run(P.portfolio('add_condition',thesis_id=tid,kind='metric',criterion='revenue falls below 40%',due_date='2026-10-02',quote=q)).startswith('Confirmed:')
    return S.conditions()[0]


def test_review_runs_once_and_only_records_a_new_cited_note(monkeypatch,tmp_path):
    from backend.brain import background,pause
    from backend.storage import jobs
    from backend.tools import vault
    c=dated_condition(monkeypatch)
    monkeypatch.setattr(pause,'is_paused',lambda *a,**kw:False)
    monkeypatch.setattr(vault,'_VAULT',tmp_path/'vault')
    admitted=[]
    async def start(*args,**kw):
        admitted.append(args)
        return SimpleNamespace(job_id='one-review-job'),''
    monkeypatch.setattr(background,'_start',start)
    when=datetime(2026,10,3,12,tzinfo=data.LOCAL_TZ)
    run(P.reviews_cycle(when))
    draft=vault._resolve('drafts','research.md')
    draft.parent.mkdir(parents=True)
    draft.write_text('His reasons: datacenter demand keeps growing\nMy analysis: Evidence changed.\nSource: https://investor.nvidia.com/results',encoding='utf-8')
    import os
    os.utime(draft,(when.timestamp()+1,when.timestamp()+1))
    monkeypatch.setattr(jobs,'get',lambda _: {'state':'done','payload':json.dumps({'summary':'Evidence changed','note_file':'drafts/research.md'})})
    async def write(filename,content,folder):
        path=vault._resolve(folder,filename)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(content,encoding='utf-8')
        return 'Saved'
    monkeypatch.setattr(vault,'vault_write',write)
    run(P.reviews_cycle(when+timedelta(minutes=1)))
    run(P.reviews_cycle(when+timedelta(minutes=2)))
    assert len(admitted)==1
    assert vault._resolve('topics','nvda-thesis.md').read_text(encoding='utf-8')==draft.read_text(encoding='utf-8')
    assert len([e for e in S.history('NVDA')['events'] if e['kind']=='reviewed'])==1
    assert not S.history('NVDA')['trades']


def test_uncited_review_is_incomplete_and_not_repeated(monkeypatch):
    from backend.brain import background,pause
    from backend.storage import jobs
    c=dated_condition(monkeypatch)
    monkeypatch.setattr(pause,'is_paused',lambda *a,**kw:False)
    async def start(*a,**kw): return SimpleNamespace(job_id='uncited'),''
    monkeypatch.setattr(background,'_start',start)
    when=datetime(2026,10,3,12,tzinfo=data.LOCAL_TZ)
    run(P.reviews_cycle(when))
    monkeypatch.setattr(jobs,'get',lambda _: {'state':'done','payload':'{"summary":"unverified"}'})
    run(P.reviews_cycle(when+timedelta(minutes=1)))
    assert S.connection().execute('SELECT state FROM portfolio_reviews').fetchone()[0]=='incomplete'
    assert [e['kind'] for e in S.history('NVDA')['events']].count('reviewed')==0


def test_full_research_slot_retries_forward_without_deleting_request(monkeypatch):
    from backend.brain import background,pause
    c=dated_condition(monkeypatch)
    monkeypatch.setattr(pause,'is_paused',lambda *a,**kw:False)
    calls=[]
    async def start(*a,**kw):
        calls.append(a)
        return (None,'slots full') if len(calls)==1 else (SimpleNamespace(job_id='later'), '')
    monkeypatch.setattr(background,'_start',start)
    when=datetime(2026,10,3,12,tzinfo=data.LOCAL_TZ)
    run(P.reviews_cycle(when))
    run(P.reviews_cycle(when+timedelta(minutes=5)))
    assert len(calls)==1
    run(P.reviews_cycle(when+timedelta(minutes=31)))
    assert len(calls)==2
    assert S.connection().execute('SELECT state,job_id FROM portfolio_reviews').fetchone()[:]==('submitted','later')


def test_pause_skips_close_and_stops_review_until_resume(monkeypatch):
    from backend.brain import pause
    seed()
    paused={'yes':True}
    monkeypatch.setattr(pause,'is_paused',lambda *a,**kw:paused['yes'])
    skipped=[]
    monkeypatch.setattr(pause,'record_skip',lambda *a:skipped.append(a))
    async def no_call(*a): raise AssertionError('network or review ran during pause')
    monkeypatch.setattr(data,'get_closes',no_call)
    monkeypatch.setattr(P,'reviews_cycle',no_call)
    when=datetime(2026,9,1,23,tzinfo=data.LOCAL_TZ)
    run(P.cycle(when))
    run(P.cycle(when))
    assert len(skipped)==1
    assert S.connection().execute('SELECT state FROM portfolio_fetches').fetchone()[0]=='paused'
    paused['yes']=False
    async def review(*a): pass
    monkeypatch.setattr(P,'reviews_cycle',review)
    run(P.cycle(when))
    assert not S.history('NVDA')['marks']


def test_forecast_link_flows_back_and_uses_existing_brier_score(monkeypatch):
    tid=thesis()
    q='wrong if revenue falls below 40% on 2026-11-19 with probability 60%'
    policy.start_scope([q])
    assert run(P.portfolio('add_condition',thesis_id=tid,kind='metric',criterion='revenue falls below 40%',due_date='2026-11-19',probability='60%',quote=q)).startswith('Confirmed:')
    c=S.conditions()[0]
    assert c['forecast_id'] and not c['watch_id']
    F.resolve(c['forecast_id'],outcome=True,resolver='fixture',quote='official results: growth 30%',notice='')
    run(P.check_conditions(time_now()))
    run(P.check_conditions(time_now()))
    assert [e['kind'] for e in S.history('NVDA')['events']].count('condition_hit')==1
    assert 'Linked forecasts: 1 resolved, Brier 0.160' in P.record_text()


def test_native_position_month_history_remains_exact_after_a_sale():
    seed(qty='10',price='100')
    mark('110','2026-09-02')
    seed(side='sell',qty='10',price='120',day='2026-10-01')
    rows=S.position_daily('NVDA')
    assert rows[1]['realised']=='0' and rows[1]['unrealised']=='100'
    assert rows[-1]['realised']=='200' and rows[-1]['unrealised']=='0'
    assert 'NVDA 2026-10, last recorded 2026-10-01: cumulative realised 200.00' in P.record_text()


def test_missing_session_cannot_be_reported_as_a_daily_gain():
    seed()
    S.save_fx([{'pair':'USD/EUR','day':'2026-09-01','rate':'1','source':'fixture','url':'https://example.com'}])
    mark('100','2026-09-01')
    mark('120','2026-09-03')  # 2 September's close is absent
    rows=S.daily()
    assert rows[-1]['profit_base']=='200'
    assert rows[-1]['change_base'] is None
    assert 'daily change unavailable' in P.brief_line()


def test_new_thesis_does_not_revive_research_for_an_ended_thesis(monkeypatch):
    from backend.brain import background,pause
    c=dated_condition(monkeypatch)
    policy.start_scope(['Close my NVDA thesis'])
    assert run(P.portfolio('close_thesis',symbol='NVDA',thesis_id=c['thesis_id'],quote='Close my NVDA thesis')).startswith('Confirmed:')
    policy.start_scope(['My NVDA reason is replacement demand'])
    assert run(P.portfolio('set_thesis',symbol='NVDA',why='replacement demand',quote='My NVDA reason is replacement demand')).startswith('Confirmed:')
    monkeypatch.setattr(pause,'is_paused',lambda *a,**kw:False)
    async def forbidden(*a,**kw): raise AssertionError('closed thesis admitted a new review')
    monkeypatch.setattr(background,'_start',forbidden)
    run(P.reviews_cycle(datetime(2026,10,3,12,tzinfo=data.LOCAL_TZ)))
    assert not S.connection().execute('SELECT 1 FROM portfolio_reviews').fetchone()


def test_split_cannot_reuse_a_pre_split_close():
    seed(qty='10',price='100')
    mark('200','2026-09-02')
    seed(side='split',qty='2',price='0',day='2026-09-03')
    row=S.positions('NVDA')[0]
    assert row['shares']=='20'
    assert row['last_mark'] is None and row['value'] is None and row['unrealised'] is None
    assert 'Total unavailable' in P.show_text()
    mark('100','2026-09-03')
    row=S.positions('NVDA')[0]
    assert Decimal(row['value'])==2000 and Decimal(row['unrealised'])==1000


def test_old_positions_view_upgrades_without_changing_facts():
    seed()
    conn=S.connection()
    old_sql=conn.execute("SELECT sql FROM sqlite_master WHERE name='v_positions'").fetchone()[0]
    old_sql=old_sql.replace("AND local_date>=COALESCE((SELECT max(traded_on) FROM trades WHERE instrument=i.symbol AND side='split'),'0001-01-01')",'')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DROP VIEW v_positions')
        conn.execute(old_sql)
    assert "side='split'" in S.connection().execute("SELECT sql FROM sqlite_master WHERE name='v_positions'").fetchone()[0]
    assert S.positions('NVDA')[0]['shares']=='10'
    assert len(S.history('NVDA')['trades'])==1


def test_opening_after_latest_mark_does_not_turn_the_brief_into_zero():
    with S.connection() as conn:
        S.ensure_instrument(P.resolve_instrument('NVDA'),now=datetime(2026,9,1,tzinfo=data.LOCAL_TZ).timestamp())
    S.save_fx([{'pair':'USD/EUR','day':'2026-09-01','rate':'1','source':'fixture','url':'https://example.com'}])
    mark('200','2026-09-01')
    seed(side='opening',day='2026-09-02')
    brief=P.brief_line()
    assert 'predates recorded trades/opening balances' in brief
    assert 'EUR 0.00' not in brief

"""The tool record: what the tools did in earlier turns, put in front of the next one.

Live, 28 October 2026 (Discord): Sam said he had sold his SpaceX shares, then sent a broker
screenshot ("Heres the details"). The ledger refused the sale: the figures were only in the
picture, and the quote check takes his words, not an image. JARVIS told him it would not take
it, but invented the cause ("the check is not receiving this turn's text at all. It is a fault
on my side"). Two messages later, once the sale went in, he told him the earlier refusals "were
my assertions, not the tool's return" -- a false confession: the receipts show they were real.

Both come from one gap: the live history keeps his words and the final replies, never what a
tool returned. These tests reproduce the exchange through the real portfolio tool and the real
tool gateway, and pin what JARVIS now sees. Model faked; zero API spend.
"""
import asyncio
import inspect
import time

import pytest

from backend.brain import engine, consolidation, portfolio as P, tool_record
from backend.brain.memory import RecallResult
from backend.brain.threads import SessionDecision
from backend.market import data
from backend.storage import operations, runs
from backend.storage import portfolio as S
from backend.tools import gateway, policy

VOICE = "I just sold all my uh SpaceX stocks. So uh Make sure the ledger and my portfolio is updated."
IMAGE_MSG = "Heres the details  [Sent image: attachments/2026-10-28-2308-img-2775.md]"
COMMA = "Sell all my spcx at 146,68"
FINAL = "sold 1.68563049 SPCX at 146.68."
SALE = {"symbol": "SPCX", "side": "sell", "shares": "1.68563049", "price": "146.68", "currency": "USD"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    async def rates(day):
        return None

    async def no_splits(i, since):
        return []
    monkeypatch.setattr(P, "_rates", rates)
    monkeypatch.setattr(data, "get_splits", no_splits)
    monkeypatch.setattr(P, "_notifier", None)
    policy.start_scope([])
    # Turns here set the run id the way main.py does; it must not outlive the test (a later
    # test's trades would share one run, and the ledger's duplicate check keys on it).
    token = operations.current_run_id.set(None)
    yield
    operations.current_run_id.reset(token)


def _spcx():
    from datetime import datetime
    i = {"symbol": "SPCX", "exchange": "NASDAQ", "currency": "USD", "price_key": "SPCX",
         "name": "Space Exploration Technologies Corp."}
    S.record_trades([{"instrument": i, "side": "opening", "shares": "1.68563049", "price": "148.52",
                      "currency": "USD", "fees": "0", "native_price": "148.52", "native_fees": "0",
                      "traded_on": "2026-10-03", "quote": "probe fixture", "dedup_key": "SPCX:opening"}],
                    run_id="fixture", now=datetime(2026, 10, 3, 23, 0, tzinfo=data.LOCAL_TZ).timestamp())


def _turn(message: str, calls: list[dict], channel: str = "discord") -> str:
    """One turn as main.py runs it: a run opened with his words, the calls made through the real
    gateway (so the receipts are the ones production writes), the run closed."""
    run_id = runs.begin_run(channel, message)
    token = operations.current_run_id.set(run_id)
    policy.start_scope([message])
    try:
        for args in calls:
            run(gateway.execute("portfolio", args, dispatch={"portfolio": P.portfolio},
                                caller=policy.LIVE))
    finally:
        operations.current_run_id.reset(token)
    runs.finish_run(run_id)
    return run_id


def _record_trade(quote, **row):
    return {"action": "record_trade", "trades": [dict(SALE, quote=quote, **row)]}


# --- the refusals now say why ---------------------------------------------------------------

def test_reproduced_a_quote_from_his_previous_message_is_refused_with_his_actual_message():
    _spcx()
    policy.start_scope([IMAGE_MSG])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote="I just sold all my uh SpaceX stocks")]))
    assert out.startswith("Error: quote is not in Sam's current message")
    assert "Heres the details" in out                      # what his message actually is
    assert "not an earlier one" in out
    assert "image or document he sent are not his words" in out
    assert "not a fault" in out                            # nothing left to invent a cause for


def test_without_an_attachment_the_refusal_does_not_talk_about_pictures():
    _spcx()
    policy.start_scope(["ok thanks"])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote="sold all my SpaceX")]))
    assert "ok thanks" in out and "image" not in out


def test_a_refusal_names_the_figure_that_is_missing():
    _spcx()
    message = "sold some spcx at 146,68"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=message)]))
    # "146,68" is read as 146.68, so only the share count is missing.
    assert "share count 1.68563049" in out and "price 146.68" not in out


def test_reproduced_sell_all_at_a_price_is_refused_only_for_the_verb_and_points_at_the_card():
    """"Sell all my spcx at 146,68": "all" now gives the count (the ledger's whole holding) and
    the comma the price; what is left is that "sell" is an order, not a sale made."""
    _spcx()
    policy.start_scope([COMMA])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=COMMA)]))
    assert "his words say 'sell'" in out and "propose_trade" in out
    assert "share count" not in out and S.positions("SPCX")[0]["shares"] == "1.68563049"


@pytest.mark.parametrize("text, figures", [
    ("Sell all my spcx at 146,68", ["146.68"]),
    ("sold 1,68563049 SPCX at 146,68", ["1.68563049", "146.68"]),
    ("bought 1,000 NVDA at 118", ["1000", "118"]),
    ("paid 1,234.50 each", ["1234.50"]),
    ("I have 200 NVDA, 50 TTWO at 160", ["200", "50", "160"]),
])
def test_a_decimal_comma_is_one_figure_and_a_thousands_comma_still_is_too(text, figures):
    assert [str(f[2]) for f in P._figures(text)] == figures


def test_a_sale_said_with_decimal_commas_is_recorded():
    _spcx()
    message = "sold 1,68563049 SPCX at 146,68"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=message)]))
    assert out.startswith("Confirmed"), out


# --- the record ------------------------------------------------------------------------------

def test_reproduced_the_next_turn_sees_what_the_tools_really_returned():
    """The exchange of 28 October, through the real tool and gateway. On the fourth turn the
    record shows both earlier refusals with their reasons: the reports were true."""
    _spcx()
    since = time.time() - 1
    _turn(VOICE, [])                                                      # asked for the price
    _turn(IMAGE_MSG, [_record_trade("I just sold all my uh SpaceX stocks")] * 3
          + [{"action": "show"}])
    _turn(COMMA, [_record_trade(COMMA)])
    current = runs.begin_run("discord", FINAL)
    text = tool_record.text("discord", since, current, engine._READ_ACTIONS)
    assert "Heres the details" in text and "Sell all my spcx at 146,68" in text
    # three refusals, written once with their count
    assert "portfolio record_trade, 3 calls, each refused: quote is not in Sam's current message" in text
    assert "portfolio show: ok" in text
    # "all" now gives the count; what was left is that "sell" is an order, not a sale made
    assert "his words say 'sell', not that he sold it" in text
    assert "do not take it back" in text
    assert "uh SpaceX" not in text                       # the voice turn used no tool: not listed
    assert FINAL not in text                                  # the turn being answered is not in it


def test_the_record_keeps_to_this_conversation():
    since = time.time()
    before = runs.begin_run("discord", "an old conversation")             # ended before `since`
    operations.record_operation(tool="vault_read", outcome="failed", started_at=since - 100,
                                args={"action": "x"}, error="Not found", run_id=before)
    runs.finish_run(before)
    conn = runs._ensure()
    conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE run_id = ?",
                 (since - 100, since - 90, before))
    conn.commit()
    other = runs.begin_run("mobile", "from the phone")
    operations.record_operation(tool="web_browse", outcome="ok", started_at=since, run_id=other)
    runs.finish_run(other)
    here = runs.begin_run("discord", "this one")
    operations.record_operation(tool="web_browse", outcome="ok", started_at=since + 1, run_id=here)
    runs.finish_run(here)
    text = tool_record.text("discord", since)
    assert "this one" in text
    assert "an old conversation" not in text and "from the phone" not in text
    assert tool_record.text("discord", None) == ""                 # empty history, no record


def test_a_refilled_history_still_shows_its_oldest_turn():
    """After a restart the history is dated when each reply was stored, a moment after its run
    ended; the oldest turn's receipts must not fall out of the window."""
    run_id = runs.begin_run("discord", "check NVDA")
    operations.record_operation(tool="web_browse", outcome="ok", started_at=time.time(), run_id=run_id)
    runs.finish_run(run_id)
    stored_at = time.time() + 2                          # the exchange row is written after the run
    assert "check NVDA" in tool_record.text("discord", stored_at)


def test_render_groups_repeats_and_words_each_outcome():
    rows = [{"run_id": "a", "run_at": 0, "user_preview": "email Sarah", "tool": "gmail_action",
             "outcome": "pending", "args_preview": "{'action': 'send_email'}", "result_preview": "", "error": None},
            {"run_id": "a", "run_at": 0, "user_preview": "email Sarah", "tool": "web_browse",
             "outcome": "unknown", "args_preview": "{}", "result_preview": "", "error": "cancelled"},
            {"run_id": "b", "run_at": 0, "user_preview": "remind me", "tool": "schedule_reminder",
             "outcome": "ok", "args_preview": "{'action': 'add'}",
             "result_preview": "Confirmed written to jarvis-vault/schedule.md (reminders): 2026-10-29 09:00",
             "error": None},
            {"run_id": "b", "run_at": 0, "user_preview": "remind me", "tool": "schedule_reminder",
             "outcome": "ok", "args_preview": "{'action': 'list'}", "result_preview": "a long list",
             "error": None}]
    text = tool_record.render(rows, frozenset({"list"}))
    assert "gmail_action send_email: became an approval request, not done" in text
    assert "web_browse: cut off mid-call, outcome unknown" in text
    assert "schedule_reminder add: ok (Confirmed written to jarvis-vault/schedule.md" in text
    assert "schedule_reminder list: ok" in text and "a long list" not in text      # a read: no head
    assert tool_record.render([]) == ""


def test_render_keeps_the_newest_turns_within_its_size():
    rows = [{"run_id": str(n), "run_at": n, "user_preview": f"message {n}", "tool": "web_browse",
             "outcome": "failed", "args_preview": "{}", "result_preview": "", "error": "SEARCH FAILED " + "x" * 300}
            for n in range(20)]
    text = tool_record.render(rows)
    assert "message 19" in text and "message 0\"" not in text
    assert "(older turns not shown)" in text
    assert len(text) < tool_record._MAX_CHARS + 900


# --- the turn ------------------------------------------------------------------------------

def _fake_turn(monkeypatch, captured, reply="Recorded."):
    async def classify(_t):
        return None

    async def session(*a, **k):
        return SessionDecision(session_id="s-record", ended_session_id=None)

    async def empty(*a, **k):
        return ""

    async def recall(*a, **k):
        return RecallResult()

    async def compat(model, system, messages, *a, **k):
        captured.setdefault("messages", messages)
        return reply

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(engine.intent, "classify", classify)
    monkeypatch.setattr(engine, "recall", recall)
    monkeypatch.setattr(engine, "assign_session", session)
    for name in ("_read_owner", "_read_latest_letter", "_read_queue", "_read_relevant_topic"):
        monkeypatch.setattr(engine, name, empty)
    monkeypatch.setattr(engine.predictions, "read_pending_block", empty)
    monkeypatch.setattr(engine.turn_signals, "start", lambda _t: None)
    monkeypatch.setattr(engine, "_chat_compat", compat)
    for name in ("_ingest_mem", "persist_mood", "_record_thread_task"):
        monkeypatch.setattr(engine, name, noop)


def test_the_record_rides_on_the_turn_and_the_turn_being_answered_is_left_out(monkeypatch):
    _spcx()
    since = time.time() - 1
    _turn(IMAGE_MSG, [_record_trade("I just sold all my uh SpaceX stocks")])
    captured = {}
    _fake_turn(monkeypatch, captured)
    current = runs.begin_run("discord", FINAL)
    operations.current_run_id.set(current)
    operations.record_operation(tool="portfolio", outcome="ok", started_at=time.time(),
                                args={"action": "record_trade"}, run_id=current)

    async def send(_m):
        pass
    history = [{"role": "user", "content": IMAGE_MSG}, {"role": "assistant", "content": "It would not take it."}]
    run(engine.chat(FINAL, history, send, None, channel="discord", history_since=since))
    outgoing = captured["messages"][-1]["content"]
    assert "[Tool record of the earlier turns" in outgoing
    assert "quote is not in Sam's current message" in outgoing
    assert outgoing.count("portfolio record_trade") == 1                      # only the earlier turn
    # history itself is unchanged: the record is not stored as anything he or JARVIS said
    assert all("Tool record" not in m["content"] for m in history)


def test_a_described_earlier_result_is_not_called_invented_when_the_record_shows_tools_ran():
    reply = "The tool came back with a refusal: the price was only in your screenshot."
    assert engine._unbacked_claims(reply, set(), 0)                         # no record: flagged
    assert not engine._unbacked_claims(reply, set(), 0, earlier_tools=True)


def test_the_turn_tells_the_claim_check_a_record_was_shown(monkeypatch):
    since = time.time() - 1
    _turn(IMAGE_MSG, [_record_trade("I just sold all my uh SpaceX stocks")])
    seen = {}
    _fake_turn(monkeypatch, {}, reply="The tool came back with a refusal earlier.")
    real = engine._enforce_backed_claims

    async def spy(*a, **k):
        seen.update(k)
        return await real(*a, **k)
    monkeypatch.setattr(engine, "_enforce_backed_claims", spy)
    operations.current_run_id.set(runs.begin_run("discord", "why did it refuse?"))

    async def send(_m):
        pass
    out = run(engine.chat("why did it refuse?", [], send, None, channel="discord", history_since=since))
    assert seen.get("earlier_tools") is True
    assert out == "The tool came back with a refusal earlier."               # not "corrected"


def test_the_correction_pass_is_told_the_record_stands():
    assert "do not take it back" in engine._CLAIM_CHECK_FRAME
    assert "never describe a tool result you did not receive" not in engine._CLAIM_CHECK_FRAME


# --- consolidation -------------------------------------------------------------------------

def test_consolidation_is_given_the_sessions_receipts():
    _spcx()
    run_id = _turn(IMAGE_MSG, [_record_trade("I just sold all my uh SpaceX stocks")])
    started = runs.get_run(run_id)["started_at"]
    threads = [{"timestamp": started + 1, "channel": "discord"}, {"timestamp": started + 2, "channel": "discord"}]
    block = consolidation._session_tool_record(threads)
    assert "quote is not in Sam's current message" in block
    assert consolidation._session_tool_record([{"timestamp": started + 1}]) == ""     # no channel
    later = [{"timestamp": started + 999, "channel": "discord"}]
    assert consolidation._session_tool_record(later) == ""          # another session's window


def test_the_consolidation_prompt_carries_the_record_and_the_rule(monkeypatch):
    captured = {}

    async def fake_call(prompt):
        captured["prompt"] = prompt
        return ""

    async def none():
        return None
    monkeypatch.setattr(consolidation, "_call_compat", fake_call)
    monkeypatch.setattr(consolidation, "_facts_sample_sync", lambda: "")
    monkeypatch.setattr(consolidation, "_listening_log_tail", lambda: "")
    run(consolidation._generate_consolidation([], "", "", "", None, tool_record_block="RECORD-LINE"))
    assert "WHAT YOUR TOOLS DID THIS SESSION" in captured["prompt"] and "RECORD-LINE" in captured["prompt"]
    prompt = " ".join(captured["prompt"].split())
    assert "Never write that you invented, misreported or confabulated something the record shows" in prompt


def test_chat_passes_its_history_window_to_the_record():
    src = inspect.getsource(engine.chat)
    assert "_tool_record.text(channel, history_since" in src


# --- a reply that takes an earlier tool report back is checked against the record ------------

@pytest.mark.parametrize("reply", [
    # the server, 28 October
    "Recorded. One thing to put right: I told you twice earlier that the tool had refused your words, "
    "and that was not true.",
    "I told you so before. Both were my assertions, not the tool's return.",
    # the replay, with and without the record
    "And to close the loop: my earlier attempts to record this did not actually run.",
    "I told you the ledger demanded a share count and a price. That was wrong, twice over.",
])
def test_a_reply_that_calls_an_earlier_tool_report_unreal_is_caught(reply):
    assert engine._retraction(reply)


@pytest.mark.parametrize("reply", [
    "Recorded. Trade #5: sell 1.68563049 SPCX at 146.68 USD.",
    "The backdate did not go through: the date was only in the image.",          # this turn's refusal
    "I was wrong about the earnings date, it is the 19th.",                       # nothing about a tool
    "I told you earlier the earnings were on the 19th; that was wrong.",          # earlier, but no tool
    "The ledger refused the backdate. The record did not go through.",            # a tool, but this turn
    'He wrote "I told you the tool refused and that was not true" in his note.',  # quoted
    "The refusals were real: the price was only in your screenshot.",
])
def test_ordinary_and_true_sentences_are_left_alone(reply):
    assert engine._retraction(reply) == ""


def test_the_record_check_reruns_once_with_the_sentence_and_keeps_a_true_correction():
    calls = []

    async def rerun(msgs):
        calls.append(msgs)
        return "Recorded. Both refusals were real; the cause I gave was a guess, and wrong."
    draft = "Recorded. I told you twice earlier that the tool had refused your words, and that was not true."
    out = run(engine._enforce_record(draft, [{"role": "user", "content": FINAL}], rerun, "discord"))
    assert out.startswith("Recorded. Both refusals were real")
    assert len(calls) == 1
    frame = calls[0][-1]["content"]
    assert "that was not true" in frame and "tool record of earlier turns" in frame
    assert calls[0][-2] == {"role": "assistant", "content": draft}


def test_the_record_check_keeps_the_draft_when_the_pass_fails_or_says_nothing():
    async def broken(msgs):
        raise RuntimeError("provider down")

    async def silent(msgs):
        return ""
    draft = "I told you twice earlier that the tool had refused your words, and that was not true."
    assert run(engine._enforce_record(draft, [], broken, "discord")) == draft
    assert run(engine._enforce_record(draft, [], silent, "discord")) == draft

    async def never(msgs):
        raise AssertionError("no pass for a reply that takes nothing back")
    assert run(engine._enforce_record("Recorded.", [], never, "discord")) == "Recorded."


def test_the_turn_runs_the_record_check_only_when_a_record_was_shown(monkeypatch):
    seen = []
    real = engine._enforce_record

    async def spy(reply, *a, **k):
        seen.append(reply)
        return await real(reply, *a, **k)
    monkeypatch.setattr(engine, "_enforce_record", spy)
    _fake_turn(monkeypatch, {}, reply="Recorded.")

    async def send(_m):
        pass
    operations.current_run_id.set(runs.begin_run("discord", "hello"))
    run(engine.chat("hello", [], send, None, channel="discord", history_since=time.time() - 1))
    assert seen == []                                                   # no earlier tools: no record
    since = time.time() - 1
    _turn(IMAGE_MSG, [_record_trade("I just sold all my uh SpaceX stocks")])
    operations.current_run_id.set(runs.begin_run("discord", FINAL))
    run(engine.chat(FINAL, [], send, None, channel="discord", history_since=since))
    assert seen == ["Recorded."]


def test_a_record_of_many_calls_keeps_within_its_size():
    rows = [{"run_id": str(n), "run_at": n, "user_preview": f"message {n}", "tool": f"tool_{k}",
             "outcome": "failed", "args_preview": "{}", "result_preview": "", "error": "y" * 300}
            for n in range(8) for k in range(6)]
    text = tool_record.render(rows)
    body = text.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert len(body) <= tool_record._MAX_CHARS
    assert "message 7" in text and "(older turns not shown)" in text


def test_consolidation_passes_the_record_to_the_model(monkeypatch, tmp_path):
    from backend.tools import vault as vault_mod
    v = tmp_path / "jarvis-vault"
    for folder in vault_mod.FOLDERS:
        (v / folder).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vault_mod, "_VAULT", v)
    monkeypatch.setattr(consolidation, "_VAULT", v)
    monkeypatch.setattr(consolidation, "_snapshot_unconsolidated_ids_sync", lambda *a, **kw: [])
    monkeypatch.setattr(consolidation, "_mark_facts_consolidated_sync", lambda *a, **kw: None)

    async def none():
        return None
    monkeypatch.setattr(consolidation, "latest_sleep", none)
    threads = [{"id": "t1", "session_id": "sess-record", "timestamp": 1000.0, "significance": 0.2,
                "high": False, "user": "x", "assistant": "y", "summary": "s", "channel": "discord"}]

    async def fake_threads(sid):
        return threads
    monkeypatch.setattr(consolidation, "threads_for_session", fake_threads)
    monkeypatch.setattr(consolidation, "_session_tool_record", lambda t: "THE-RECORD" if t is threads else "")
    captured = {}

    async def fake_generate(*args, **kwargs):
        captured.update(kwargs)
        return None
    monkeypatch.setattr(consolidation, "_generate_consolidation", fake_generate)
    run(consolidation.consolidate_session("sess-record"))
    assert captured.get("tool_record_block") == "THE-RECORD"

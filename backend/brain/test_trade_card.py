"""After the SPCX sale of 28 October 2026: a trade from a screenshot in one tap, "sold all", and
/trace.

He sold his SpaceX shares and sent the broker confirmation as a screenshot. The ledger rightly
refused figures that were only in the picture, and it took three more messages to get the sale in:
"Sell all my spcx at 146,68" was refused for the share count, which the ledger already knew.

  - propose_trade: the figures JARVIS read are put to him as one Approve / Deny card; his tap
    records exactly those, through the approval flow (Discord and the phone share it).
  - "sold all my X at P": the share count is the ledger's whole holding.
  - /trace (and the phone's Activity tab): what the tools did, from the receipts, for him to read.

Real portfolio tool, gateway, approval store and flow; nothing over the network.
"""
import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest

from backend.brain import attachments, discord_bridge as db, portfolio as P, tool_record
from backend.brain import test_activity as A
from backend.brain.test_activity import bridge  # noqa: F401  (the Discord client fixture)
from backend.brain import test_tool_record as T
from backend.market import data
from backend.storage import approvals, exchanges, operations, runs
from backend.storage import portfolio as S
from backend.tools import approval_flow, gateway, policy

SALE = T.SALE
CARD = {"action": "propose_trade",
        "trades": [dict(SALE, traded_on="2026-10-26", quote="his broker screenshot")]}


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
    monkeypatch.setattr(approval_flow, "_notifier", None)
    monkeypatch.setattr(approval_flow, "_resolution_notifier", None, raising=False)
    monkeypatch.setattr(approval_flow, "_undelivered", [])
    monkeypatch.setattr(attachments, "_in_view", {})
    policy.start_scope([])
    token = operations.current_run_id.set(None)
    yield
    operations.current_run_id.reset(token)


def _call(args, message, caller=policy.LIVE):
    """One tool call in a turn where he said `message`, through the real gateway."""
    run_id = runs.begin_run("discord", message)
    token = operations.current_run_id.set(run_id)
    policy.start_scope([message])
    try:
        return run(gateway.execute("portfolio", args, dispatch={"portfolio": P.portfolio}, caller=caller))
    finally:
        operations.current_run_id.reset(token)
        runs.finish_run(run_id)


def _held():
    return S.positions("SPCX")[0]["shares"]


def _sold():
    return [t for t in S.trade_rows("SPCX") if t["side"] == "sell" and not t["voided"]]


# --- "sold all" -------------------------------------------------------------------------------

def test_sold_all_takes_the_share_count_from_the_ledger():
    T._spcx()
    message = "I sold all my SPCX at 146.68"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=message)]))
    assert out.startswith("Confirmed"), out
    assert float(_held()) == 0.0


def test_sold_all_with_a_different_count_is_refused_and_names_the_holding():
    T._spcx()
    message = "sold all my SPCX at 146.68"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, shares="1", quote=message)]))
    assert "the ledger holds 1.68563049 shares" in out and not _sold()


def test_sold_all_without_a_price_names_only_the_price():
    T._spcx()
    message = "I just sold all my SPCX"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=message)]))
    assert "price 146.68" in out and "share count 1" not in out and "all\" gives" in out and not _sold()


@pytest.mark.parametrize("message", [
    "if I sold all my SPCX at 146.68 what would I make",
    "should I sell all my SPCX at 146.68",
    "my brother sold all his SPCX at 146.68",
])
def test_all_does_not_make_a_hypothetical_or_someone_elses_sale_his(message):
    T._spcx()
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=message)]))
    assert out.startswith("Error") and not _sold()


def test_all_never_supplies_the_count_of_a_buy():
    T._spcx()
    message = "bought all the SPCX I could at 146.68"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, side="buy", quote=message)]))
    assert "share count" in out


def test_all_of_a_stock_he_does_not_hold_is_refused():
    message = "sold all my SPCX at 146.68"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=message)]))
    assert out.startswith("Error")


# --- the card ---------------------------------------------------------------------------------

def test_reproduced_the_screenshot_turn_puts_the_sale_to_him_as_one_card_and_records_nothing():
    T._spcx()
    result = _call(CARD, T.IMAGE_MSG)
    assert result.outcome == "ok", result.result
    assert "Not recorded yet" in result.result and "Approve" in result.result
    assert _held() == "1.68563049" and not _sold()
    [row] = approvals.pending()
    assert row["preview"] == ("Record in your portfolio: sold 1.68563049 SPCX at 146.68 USD on "
                              "2026-10-26.")
    assert "your tap records exactly these" in row["why"]


def test_approving_the_card_records_exactly_the_figures_shown():
    T._spcx()
    _call(CARD, T.IMAGE_MSG)
    [row] = approvals.pending()
    said = run(approval_flow.approve(row["id"], "discord button"))
    assert said.startswith(f"Approved #{row['id']}. Confirmed"), said
    [sale] = _sold()
    assert (sale["shares"], sale["price"], sale["traded_on"]) == ("1.68563049", "146.68", "2026-10-26")
    assert sale["quote"].startswith(P._CARD_QUOTE)
    assert float(_held()) == 0.0
    # A second tap (or "yes N" racing it) cannot record it twice.
    run(approval_flow.approve(row["id"], "yes reply"))
    assert len(_sold()) == 1


def test_denying_the_card_records_nothing():
    T._spcx()
    _call(CARD, T.IMAGE_MSG)
    [row] = approvals.pending()
    run(approval_flow.deny(row["id"], "discord button"))
    assert _held() == "1.68563049" and not _sold()


def test_the_model_cannot_record_with_the_card_quote_itself():
    """Only the approval flow carries a card out: the same quote from a live turn is checked
    against his words like any other, and refused."""
    T._spcx()
    out = _call({"action": "record_trade", "trades": [dict(SALE, quote=P._CARD_QUOTE + "sold it")]},
                T.IMAGE_MSG)
    assert out.outcome == "failed" and not _sold()


def test_an_approved_record_trade_without_the_card_quote_is_refused():
    T._spcx()
    out = _call({"action": "record_trade", "trades": [dict(SALE, quote="whatever")]}, "x",
                caller=policy.APPROVED)
    assert out.outcome == "failed" and not _sold()


@pytest.mark.parametrize("caller", ["unattended", "background", "scheduled", "goal"])
def test_no_unattended_caller_can_put_a_card(caller):
    T._spcx()
    out = _call(CARD, T.IMAGE_MSG, caller=caller)
    assert out.outcome == "failed" and not approvals.pending()


def test_a_card_needs_a_file_he_sent_or_his_message_naming_the_stock():
    T._spcx()
    out = _call(CARD, "ok thanks")
    assert out.outcome == "failed" and "image or document he sent" in out.result
    assert not approvals.pending()


def test_his_message_naming_the_stock_is_enough_for_a_card():
    T._spcx()
    assert _call(CARD, T.COMMA).outcome == "ok"
    assert len(approvals.pending()) == 1


def test_an_image_still_in_view_counts_for_the_next_turn():
    T._spcx()
    attachments._in_view["discord"] = [attachments._InView("x.png", "data:", time.time())]
    assert _call(CARD, "that's the one").outcome == "ok"


def test_a_sale_bigger_than_the_holding_is_not_put_to_him():
    T._spcx()
    out = _call({**CARD, "trades": [dict(CARD["trades"][0], shares="5")]}, T.IMAGE_MSG)
    assert out.outcome == "failed" and "holds 1.68563049" in out.result and not approvals.pending()


def test_a_trade_already_in_the_ledger_gets_no_card():
    T._spcx()
    _call(CARD, T.IMAGE_MSG)
    run(approval_flow.approve(approvals.pending()[0]["id"], "tap"))
    out = _call(CARD, T.IMAGE_MSG)
    assert "Already in the ledger" in out.result and not approvals.pending()


def test_a_split_or_correction_is_never_carded():
    T._spcx()
    out = _call({**CARD, "trades": [dict(CARD["trades"][0], side="split", shares="2", price="0")]},
                T.IMAGE_MSG)
    assert out.outcome == "failed" and not approvals.pending()


def test_the_screenshot_refusal_now_points_at_the_card():
    T._spcx()
    policy.start_scope([T.IMAGE_MSG])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote="I just sold all my uh SpaceX stocks")]))
    assert "propose_trade" in out and "Approve/Deny card" in out


def test_the_card_is_offered_to_the_model_and_only_in_conversation():
    from backend.brain import background
    from backend.tools import registry
    schema = next(t for t in registry.TOOLS if t["name"] == "portfolio")["input_schema"]
    assert "propose_trade" in schema["properties"]["action"]["enum"]
    assert "propose_trade" not in background._BG_ACTIONS["portfolio"]
    assert policy.RISK["portfolio"]["propose_trade"] == policy.OWN


# --- /trace -----------------------------------------------------------------------------------

def _exchange(channel, user, ts):
    exchanges.record_exchange(exchange_id=str(uuid.uuid4()), session_id="s", channel=channel,
                              user_text=user, assistant_text="reply", significance=0.1, high=False,
                              timestamp=ts)


def test_trace_shows_him_what_the_tools_did_in_our_conversation():
    T._spcx()
    start = time.time() - 30
    _exchange("discord", T.VOICE, start)
    T._turn(T.IMAGE_MSG, [T._record_trade("I just sold all my uh SpaceX stocks")])
    _exchange("discord", T.IMAGE_MSG, time.time())
    text = tool_record.trace_text("discord")
    assert text.startswith("What my tools did in our conversation since")
    assert 'after you said "Heres the details' in text
    assert "portfolio record_trade: refused: quote is not in Sam's current message" in text
    assert "This record is for you" not in text            # his view, not JARVIS's instructions


def test_trace_with_no_conversation_says_so():
    assert tool_record.trace_text("discord") == "There's no conversation of ours to trace yet."


def test_trace_of_a_conversation_without_tool_calls():
    _exchange("discord", "hi", time.time())
    assert tool_record.trace_text("discord").startswith("No tool calls in our conversation so far")


def test_trace_is_per_channel():
    T._spcx()
    _exchange("mobile", "hi", time.time() - 10)
    T._turn(T.IMAGE_MSG, [T._record_trade("x")], channel="discord")
    since, lines, _ = tool_record.trace("mobile")
    assert since is not None and lines == []


def test_slash_trace_is_a_command(bridge, monkeypatch):
    monkeypatch.setattr(db, "_trace_handler", None)
    db.configure(trace_handler=lambda: "the trace")
    assert A.say(bridge, "/trace") == ["the trace"]
    assert "/trace" in [cmd for cmd, _ in db._COMMAND_HELP]


def test_the_phone_activity_view_carries_the_trace_section(monkeypatch):
    from backend.brain import activity
    from backend.mobile import routes
    monkeypatch.setattr(activity, "activity_view", lambda now=None: {"as_of": 1.0, "paused": None, "sections": []})
    monkeypatch.setattr(tool_record, "trace", lambda channel: (time.time(), [f"line on {channel}"], False))
    view = routes._activity_with_trace()
    [section] = view["sections"]
    assert section["title"].startswith("What my tools did in our chat since")
    assert section["items"] == ["line on mobile"]


def test_a_card_with_no_date_says_today_so_a_wrong_date_shows():
    T._spcx()
    trade = {k: v for k, v in CARD["trades"][0].items() if k != "traded_on"}
    assert _call({**CARD, "trades": [trade]}, T.IMAGE_MSG).outcome == "ok"
    [row] = approvals.pending()
    assert " today (" in row["preview"]


def test_a_card_approved_after_he_typed_the_same_sale_records_nothing_twice():
    T._spcx()
    _call(CARD, T.IMAGE_MSG)
    words = "sold 1.68563049 SPCX at 146.68 on 26 October"
    assert _call({"action": "record_trade", "trades": [dict(SALE, traded_on="2026-10-26", quote=words)]},
                 words).outcome == "ok"
    said = run(approval_flow.approve(approvals.pending()[0]["id"], "tap"))
    assert "Already in the ledger" in said and len(_sold()) == 1


def test_an_approved_card_never_records_a_split():
    T._spcx()
    split = dict(SALE, side="split", shares="2", price="0", quote=P._CARD_QUOTE + "split")
    out = _call({"action": "record_trade", "trades": [split]}, "x", caller=policy.APPROVED)
    assert out.outcome == "failed" and not [t for t in S.trade_rows("SPCX") if t["side"] == "split"]


def test_trace_starts_at_the_first_message_of_the_conversation():
    T._spcx()
    start = time.time() - 600
    _exchange("discord", T.VOICE, start)
    T._turn(T.IMAGE_MSG, [T._record_trade("x")])
    _exchange("discord", "and later", time.time() + 5)
    since, lines, _ = tool_record.trace("discord")
    assert abs(since - start) < 1 and len(lines) == 1


def test_all_of_a_stock_he_does_not_hold_says_nothing_about_a_sale_of_zero():
    message = "sold all my SPCX at 146.68"
    policy.start_scope([message])
    out = run(P.portfolio("record_trade", trades=[dict(SALE, quote=message)]))
    assert out.startswith("Error") and "sale of 0" not in out and "holds 0" not in out


def test_a_card_with_an_unknown_side_is_refused_in_words():
    T._spcx()
    out = _call({**CARD, "trades": [dict(CARD["trades"][0], side="short")]}, T.IMAGE_MSG)
    assert out.outcome == "failed" and "buy, sale or holding" in out.result and not approvals.pending()


def test_a_card_that_cannot_be_stored_is_not_reported_as_waiting(monkeypatch):
    T._spcx()

    async def nothing(**kw):
        return None
    monkeypatch.setattr(approval_flow, "request", nothing)
    out = _call(CARD, T.IMAGE_MSG)
    assert out.outcome == "failed" and "could not be stored" in out.result

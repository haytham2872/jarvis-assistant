"""The approvals store (P1): one row is one exact action, decided once, never run twice."""
import threading
from datetime import datetime, timezone

import pytest

from backend.storage import approvals, db


def _make(now=1000.0, **over):
    args = dict(context="background", tool="gmail_action", action="send_email",
                arguments={"action": "send_email", "to": "x@example.com",
                           "subject": "Hi", "body": "Bonjour — café"},
                preview="Email to x@example.com: Hi", risk="outward",
                why="a background job cannot send email", run_id="run-1", now=now)
    args.update(over)
    return approvals.create(**args)


def test_create_stores_the_exact_action_and_the_life_log_fields():
    ts = datetime(2026, 9, 29, 22, 30, tzinfo=timezone.utc).timestamp()      # 00:30 on the 30th, Paris
    approval_id = _make(now=ts)
    row = approvals.get(approval_id)
    assert row["state"] == approvals.PENDING
    assert row["arguments"] == {"action": "send_email", "to": "x@example.com",
                                "subject": "Hi", "body": "Bonjour — café"}
    assert (row["created_at"], row["local_date"]) == (ts, "2026-09-30")
    assert row["expires_at"] == ts + approvals.EXPIRY_S
    assert (row["tool"], row["action"], row["risk"], row["context"], row["run_id"]) == \
        ("gmail_action", "send_email", "outward", "background", "run-1")
    assert row["decision"] is None and row["decided_at"] is None and row["executed_at"] is None


def test_ids_count_up_and_are_never_reused():
    first, second = _make(), _make()
    assert second == first + 1
    approvals.deny(second, "text", now=1001.0)
    assert _make() == second + 1


def test_claim_moves_a_pending_request_to_executing_exactly_once():
    approval_id = _make()
    row = approvals.claim(approval_id, "button", now=1010.0)
    assert row["state"] == approvals.EXECUTING and row["decision"] == "approved"
    assert (row["decided_at"], row["decided_how"]) == (1010.0, "button")
    assert row["arguments"]["to"] == "x@example.com"
    assert approvals.claim(approval_id, "text", now=1011.0) is None            # second tap: nothing


def test_two_simultaneous_approvals_run_the_action_once():
    """A button tap racing a text reply: the conditional UPDATE lets exactly one through."""
    approval_id = _make()
    gate = threading.Barrier(8)
    winners = []

    def press(i):
        gate.wait()
        if approvals.claim(approval_id, "button", now=1010.0) is not None:
            winners.append(i)

    threads = [threading.Thread(target=press, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_an_expired_request_cannot_be_approved_even_before_the_sweep_runs():
    approval_id = _make(now=1000.0)
    late = 1000.0 + approvals.EXPIRY_S + 1
    assert approvals.claim(approval_id, "button", now=late) is None
    assert approvals.get(approval_id)["state"] == approvals.PENDING            # not swept yet
    assert approvals.pending(now=late) == []                                   # but not offered


def test_expiry_sweep_expires_each_request_once():
    stale, fresh = _make(now=1000.0), _make(now=1000.0 + approvals.EXPIRY_S)
    sweep = approvals.expire_due(now=1000.0 + approvals.EXPIRY_S + 5)
    assert [r["id"] for r in sweep] == [stale]
    assert sweep[0]["state"] == approvals.EXPIRED and sweep[0]["decided_how"] == "expiry"
    assert approvals.get(fresh)["state"] == approvals.PENDING
    assert approvals.expire_due(now=1000.0 + approvals.EXPIRY_S + 6) == []


def test_a_denied_request_can_never_be_approved_and_the_reverse():
    a, b = _make(), _make()
    assert approvals.deny(a, "button", now=1005.0)["state"] == approvals.DENIED
    assert approvals.claim(a, "text", now=1006.0) is None
    approvals.claim(b, "button", now=1005.0)
    assert approvals.deny(b, "text", now=1006.0) is None
    assert approvals.get(b)["state"] == approvals.EXECUTING


def test_finish_records_the_outcome_once():
    approval_id = _make()
    approvals.claim(approval_id, "button", now=1010.0)
    assert approvals.finish(approval_id, approvals.EXECUTED, "Email sent to x@example.com.", now=1012.0)
    row = approvals.get(approval_id)
    assert (row["state"], row["executed_at"], row["outcome"]) == \
        (approvals.EXECUTED, 1012.0, "Email sent to x@example.com.")
    assert approvals.finish(approval_id, approvals.FAILED, "again", now=1013.0) is False
    assert approvals.get(approval_id)["state"] == approvals.EXECUTED


def test_finish_only_applies_to_a_request_that_is_executing():
    approval_id = _make()
    assert approvals.finish(approval_id, approvals.EXECUTED, "nope") is False
    with pytest.raises(ValueError):
        approvals.finish(approval_id, approvals.DENIED, "not a finishing state")


def test_a_run_cut_off_mid_send_becomes_uncertain_at_startup_and_is_never_retried():
    running, waiting, done = _make(), _make(), _make()
    approvals.claim(running, "button", now=1010.0)
    approvals.claim(done, "button", now=1010.0)
    approvals.finish(done, approvals.EXECUTED, "sent", now=1011.0)
    rows = approvals.reconcile_interrupted()
    assert [r["id"] for r in rows] == [running]
    assert approvals.get(running)["state"] == approvals.UNCERTAIN
    assert "may or may not" in approvals.get(running)["outcome"]
    assert approvals.get(waiting)["state"] == approvals.PENDING
    assert approvals.get(done)["state"] == approvals.EXECUTED
    assert approvals.reconcile_interrupted() == []
    assert approvals.claim(running, "text") is None                             # cannot be re-run


def test_pending_lists_only_open_requests_oldest_first():
    a, b, c = _make(now=1000.0), _make(now=1001.0), _make(now=1002.0)
    approvals.deny(b, "text", now=1003.0)
    assert [r["id"] for r in approvals.pending(now=1004.0)] == [a, c]


def test_the_message_carrying_the_buttons_is_remembered():
    approval_id = _make()
    approvals.attach_message(approval_id, 123456789)
    assert approvals.get(approval_id)["message_id"] == "123456789"


def test_reasons_a_reply_did_not_act_are_plain_words():
    a = _make(now=1000.0)
    assert "no request #999" in approvals.reason_not_pending(999)
    approvals.deny(a, "text", now=1001.0)
    assert "already denied" in approvals.reason_not_pending(a)
    b = _make(now=1000.0)
    approvals.expire_due(now=1000.0 + approvals.EXPIRY_S + 1)
    assert "expired after 24 hours" in approvals.reason_not_pending(b)
    c = _make()
    approvals.claim(c, "button", now=1001.0)
    assert "already approved" in approvals.reason_not_pending(c)


def test_storage_failure_records_nothing_and_never_raises(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert _make() is None
    assert approvals.get(1) is None and approvals.claim(1, "button") is None
    assert approvals.deny(1, "text") is None and approvals.pending() == []
    assert approvals.expire_due() == [] and approvals.reconcile_interrupted() == []
    assert approvals.finish(1, approvals.EXECUTED, "x") is False


def test_long_free_text_is_bounded():
    approval_id = _make(preview="p" * 5000, why="w " * 500)
    row = approvals.get(approval_id)
    assert len(row["preview"]) <= 600 and len(row["why"]) <= 400
    assert len(row["arguments"]["body"]) < 100        # the arguments themselves are never trimmed
    big = _make(arguments={"body": "x" * 20000})
    assert len(approvals.get(big)["arguments"]["body"]) == 20000

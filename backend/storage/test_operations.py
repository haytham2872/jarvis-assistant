"""A02 slice 3: every tool call leaves a durable receipt, with a typed outcome.

The roadmap's contract: "Record operation intent before a tool side effect and its
observed result afterward", so Jarvis "can explain what a job did, what it saved, what
remains uncertain". `unknown` is the load-bearing outcome — an interrupted external write
must stay uncertain rather than be resolved by a blind retry.
"""
import asyncio
import time

import pytest

from backend.brain import engine
from backend.storage import db, operations as ops
from backend.tools import policy


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()
    ops.current_run_id.set(None)
    yield
    db._reset_for_tests()


async def _noop_send(_):
    pass


def _dispatch(monkeypatch, fn, name="demo_tool"):
    monkeypatch.setitem(engine.TOOL_DISPATCH, name, fn)
    if name not in policy.RISK:
        # An unclassified tool needs Sam's approval (P1, fail-closed), which is not what
        # these receipt tests are about: give the stand-in tool a level.
        monkeypatch.setitem(policy.RISK, name, policy.OWN)
    return {"id": "1", "name": name, "input": {"arg": "value"}}


# ------------------------------------------------------------- repository level
def test_receipt_records_outcome_and_timing():
    started = time.time() - 0.05
    oid = ops.record_operation(tool="vault_write", outcome=ops.OK, started_at=started,
                               args={"filename": "x.md"}, result="Confirmed written")
    assert oid
    row = ops.operations_for_run(None)
    # run_id is None here; fetch directly instead
    conn = db.connect()
    got = dict(conn.execute("SELECT * FROM operations WHERE operation_id = ?",
                            (oid,)).fetchone())
    assert got["tool"] == "vault_write"
    assert got["outcome"] == ops.OK
    assert got["duration_ms"] >= 40
    assert "x.md" in got["args_preview"]
    assert "Confirmed written" in got["result_preview"]


def test_previews_are_bounded():
    oid = ops.record_operation(tool="web_browse", outcome=ops.OK,
                               started_at=time.time(), result="y" * 20000)
    conn = db.connect()
    got = dict(conn.execute("SELECT result_preview FROM operations WHERE operation_id = ?",
                            (oid,)).fetchone())
    assert len(got["result_preview"]) == ops._PREVIEW_CHARS


def test_operations_are_grouped_by_run():
    ops.record_operation(tool="a", outcome=ops.OK, started_at=1.0, run_id="run-1")
    ops.record_operation(tool="b", outcome=ops.OK, started_at=2.0, run_id="run-1")
    ops.record_operation(tool="c", outcome=ops.OK, started_at=3.0, run_id="run-2")
    assert [o["tool"] for o in ops.operations_for_run("run-1")] == ["a", "b"]
    assert [o["tool"] for o in ops.operations_for_run("run-2")] == ["c"]


def test_uncertain_operations_are_listable():
    ops.record_operation(tool="gmail_action", outcome=ops.UNKNOWN, started_at=1.0)
    ops.record_operation(tool="vault_read", outcome=ops.OK, started_at=2.0)
    listed = ops.uncertain_operations()
    assert [o["tool"] for o in listed] == ["gmail_action"]


def test_degrades_when_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert ops.record_operation(tool="x", outcome=ops.OK, started_at=1.0) is None
    assert ops.operations_for_run("r") == []
    assert ops.uncertain_operations() == []
    assert ops.counts() == {}


# ------------------------------------------------- wired into the real _exec_tool
def test_successful_tool_call_writes_an_ok_receipt(monkeypatch):
    async def _ok(**kw):
        return "all good"

    tc = _dispatch(monkeypatch, _ok)
    ops.current_run_id.set("run-abc")
    result = run(engine._exec_tool(tc, _noop_send))
    assert result[2] is True

    recs = ops.operations_for_run("run-abc")
    assert len(recs) == 1
    assert recs[0]["tool"] == "demo_tool"
    assert recs[0]["outcome"] == ops.OK
    assert "all good" in recs[0]["result_preview"]


def test_raising_tool_writes_a_failed_receipt_with_the_error(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("disk on fire")

    tc = _dispatch(monkeypatch, _boom)
    ops.current_run_id.set("run-abc")
    run(engine._exec_tool(tc, _noop_send))

    rec = ops.operations_for_run("run-abc")[0]
    assert rec["outcome"] == ops.FAILED
    assert "disk on fire" in rec["error"]


def test_in_band_error_string_is_recorded_as_failed(monkeypatch):
    """The failure shape the roadmap calls out: a tool that returns an error STRING must
    not be recorded as a success."""
    async def _soft_fail(**kw):
        return "Error: mailbox unavailable"

    tc = _dispatch(monkeypatch, _soft_fail)
    ops.current_run_id.set("run-abc")
    result = run(engine._exec_tool(tc, _noop_send))
    assert result[2] is False

    rec = ops.operations_for_run("run-abc")[0]
    assert rec["outcome"] == ops.FAILED
    assert "mailbox unavailable" in rec["error"]


def test_unknown_tool_writes_a_failed_receipt(monkeypatch):
    tc = {"id": "1", "name": "no_such_tool", "input": {}}
    ops.current_run_id.set("run-abc")
    run(engine._exec_tool(tc, _noop_send))
    rec = ops.operations_for_run("run-abc")[0]
    assert rec["outcome"] == ops.FAILED


def test_cancelled_tool_is_recorded_as_unknown_not_failed(monkeypatch):
    """The load-bearing case. A cancelled external write may or may not have landed
    remotely; recording it as `failed` would invite a retry that double-sends."""
    async def _cancelled(**kw):
        raise asyncio.CancelledError()

    tc = _dispatch(monkeypatch, _cancelled, name="gmail_action")
    # A04 validates before dispatch, so the call must be well-formed to reach the
    # cancellation path this test is about.
    tc["input"] = {"action": "send_email", "to": "a@b.test", "subject": "s", "body": "b",
                   "quote": "email a@b.test about it"}
    # An outward send reaches the tool only when Sam asked for it in this turn (P1).
    policy.start_scope(["please email a@b.test about it"])
    ops.current_run_id.set("run-abc")
    with pytest.raises(asyncio.CancelledError):
        run(engine._exec_tool(tc, _noop_send))

    rec = ops.operations_for_run("run-abc")[0]
    assert rec["outcome"] == ops.UNKNOWN
    assert rec["tool"] == "gmail_action"
    assert ops.uncertain_operations()[0]["tool"] == "gmail_action"


def test_receipt_failure_never_breaks_the_tool_call(monkeypatch):
    """Additive means additive."""
    async def _ok(**kw):
        return "fine"

    def _boom(**kw):
        raise RuntimeError("receipt store exploded")

    monkeypatch.setattr(ops, "record_operation", _boom)
    tc = _dispatch(monkeypatch, _ok)
    tc_out, result, ok = run(engine._exec_tool(tc, _noop_send))
    assert ok is True and result == "fine"


def test_run_id_comes_from_the_context_var(monkeypatch):
    """main.py sets it once per turn; tools running concurrently under gather each
    inherit it rather than sharing a mutable global."""
    async def _ok(**kw):
        return "x"

    tc = _dispatch(monkeypatch, _ok)
    ops.current_run_id.set("run-ctx")
    run(engine._exec_tool(tc, _noop_send))
    assert len(ops.operations_for_run("run-ctx")) == 1


def test_job_and_outbox_tables_exist_but_are_unused():
    """Schema only, per the roadmap: "infrastructure for later A06/A07 behavior, not a
    second competing implementation"."""
    assert ops.counts() == {"operations": 0, "derivation_jobs": 0, "delivery_intents": 0}

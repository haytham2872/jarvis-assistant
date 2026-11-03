"""A04 slice 3: one policy, both executors.

The bug class this closes is not any single rule — it is that there were two executors
and every fix landed on one of them. So the load-bearing tests here are the PARITY ones
at the bottom: they run the same call through `engine._exec_tool` and
`background._bg_exec` and assert the two agree. A future fix applied to only one path
fails them.
"""
import asyncio
from types import SimpleNamespace

import pytest

from backend.brain import background, engine
from backend.storage import db, operations as ops
from backend.tools import gateway, outcomes, validation


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


def _spy(result="did the thing"):
    ran = []

    async def fn(**kw):
        ran.append(kw)
        return result

    return ran, fn


# ------------------------------------------------------------------ policy order
def test_denied_capability_never_looks_up_the_tool():
    ran, fn = _spy()
    res = run(gateway.execute("gmail_action", {"action": "list_inbox"},
                              dispatch={"gmail_action": fn},
                              capabilities={"vault_read"}))
    assert ran == []
    assert res.outcome == outcomes.FAILED
    assert res.error == "capability denied"


def test_none_capabilities_means_unrestricted_not_empty():
    """`None` and `set()` must not be conflated -- one allows everything, the other
    denies everything, and the foreground passes None."""
    ran, fn = _spy()
    res = run(gateway.execute("gmail_action", {"action": "list_inbox"},
                              dispatch={"gmail_action": fn}, capabilities=None))
    assert res.ok and ran

    ran2, fn2 = _spy()
    res2 = run(gateway.execute("gmail_action", {"action": "list_inbox"},
                               dispatch={"gmail_action": fn2}, capabilities=set()))
    assert not res2.ok and ran2 == []


def test_unknown_tool_is_a_failure():
    res = run(gateway.execute("no_such_tool", {}, dispatch={}))
    assert res.outcome == outcomes.FAILED
    assert res.result == "Unknown tool: no_such_tool"


def test_malformed_arguments_are_rejected_before_dispatch():
    ran, fn = _spy()
    res = run(gateway.execute("vault_write", {}, dispatch={"vault_write": fn},
                              invalid_args='{"filename": "trunc'))
    assert ran == []
    assert res.outcome == outcomes.FAILED
    assert "not valid JSON" in res.result


def test_schema_violation_is_rejected_before_dispatch():
    ran, fn = _spy()
    res = run(gateway.execute("gmail_action", {"action": "delete_everything"},
                              dispatch={"gmail_action": fn}))
    assert ran == []
    assert "delete_everything" in res.result


def test_raising_tool_is_a_failure_not_a_crash():
    async def boom(**kw):
        raise RuntimeError("disk on fire")

    res = run(gateway.execute("vault_read", {"filename": "x.md", "folder": "topics"},
                              dispatch={"vault_read": boom}))
    assert res.outcome == outcomes.FAILED
    assert "disk on fire" in res.result


def test_cancellation_is_unknown_and_propagates():
    """A02's load-bearing outcome: a cancelled external write may or may not have landed,
    so it must not be recorded as `failed` and invite a double-send on retry."""
    async def cancelled(**kw):
        raise asyncio.CancelledError()

    ops.current_run_id.set("run-c")
    with pytest.raises(asyncio.CancelledError):
        # an approved send: an unapproved one no longer reaches the tool at all (P1)
        run(gateway.execute("gmail_action", {"action": "send_email"},
                            dispatch={"gmail_action": cancelled}, caller="approved"))
    assert ops.operations_for_run("run-c")[0]["outcome"] == ops.UNKNOWN


def test_in_band_error_string_is_classified_as_a_failure():
    _, fn = _spy("Not found: topics/missing.md")
    res = run(gateway.execute("vault_read", {"filename": "m.md", "folder": "topics"},
                              dispatch={"vault_read": fn}))
    assert res.outcome == outcomes.FAILED


def test_success_is_a_success():
    _, fn = _spy("# a real note")
    res = run(gateway.execute("vault_read", {"filename": "x.md", "folder": "topics"},
                              dispatch={"vault_read": fn}))
    assert res.ok and res.result == "# a real note" and res.error is None


# ------------------------------------------------------------------- receipts
@pytest.mark.parametrize("case,kwargs,expected", [
    ("denied",  {"capabilities": set()},                          ops.FAILED),
    ("invalid", {"invalid_args": "{trunc"},                       ops.FAILED),
    ("bad-arg", {},                                               ops.FAILED),
])
def test_every_rejection_leaves_a_receipt(case, kwargs, expected):
    """A rejected call is still an event worth explaining later. Silent rejection would
    reproduce the A02 gap this whole layer exists to close."""
    _, fn = _spy()
    ops.current_run_id.set("run-r")
    args = {} if case == "bad-arg" else {"action": "list_inbox"}
    run(gateway.execute("gmail_action", args, dispatch={"gmail_action": fn}, **kwargs))
    recs = ops.operations_for_run("run-r")
    assert len(recs) == 1 and recs[0]["outcome"] == expected


def test_receipt_failure_never_breaks_the_call(monkeypatch):
    def boom(**kw):
        raise RuntimeError("receipt store exploded")

    monkeypatch.setattr(ops, "record_operation", boom)
    _, fn = _spy("fine")
    res = run(gateway.execute("vault_read", {"filename": "x.md", "folder": "topics"},
                              dispatch={"vault_read": fn}))
    assert res.ok and res.result == "fine"


def test_recording_can_be_disabled():
    _, fn = _spy()
    ops.current_run_id.set("run-off")
    run(gateway.execute("gmail_action", {"action": "list_inbox"},
                        dispatch={"gmail_action": fn}, record=False))
    assert ops.operations_for_run("run-off") == []


# --------------------------------------------------------- foreground/background parity
# The reason this module exists. Each case runs the SAME call through both executors.
_PARITY_CASES = [
    ("malformed arguments", "vault_write", {}, '{"filename": "trunc'),
    ("schema violation",    "gmail_action", {"action": "delete_everything"}, None),
    ("unknown argument",    "vault_read",
     {"filename": "x.md", "folder": "topics", "recursive": True}, None),
]


@pytest.mark.parametrize("label,tool,args,invalid", _PARITY_CASES)
def test_both_executors_reject_the_same_bad_call(monkeypatch, label, tool, args, invalid):
    fg_ran, fg_fn = _spy()
    monkeypatch.setitem(engine.TOOL_DISPATCH, tool, fg_fn)

    tc = {"id": "1", "name": tool, "input": dict(args)}
    if invalid is not None:
        tc[validation.INVALID_ARGS_KEY] = invalid
    _, fg_result, fg_ok = run(engine._exec_tool(tc, _noop_send))

    bg_result = run(background._bg_exec(tool, dict(args), invalid))

    assert fg_ran == [], f"{label}: foreground executed a bad call"
    assert fg_ok is False, f"{label}: foreground accepted a bad call"
    assert fg_result == bg_result, (
        f"{label}: the two executors disagree\n  fg: {fg_result}\n  bg: {bg_result}")


def test_background_records_receipts_like_the_foreground(monkeypatch):
    """Before this slice a background task left no durable evidence at all -- an
    overnight action could not be explained afterward."""
    _, fn = _spy("Confirmed written to jarvis-vault/drafts/x.md (3 chars)")
    monkeypatch.setitem(engine.TOOL_DISPATCH, "vault_write", fn)
    ops.current_run_id.set("run-bg")

    run(background._bg_exec(
        "vault_write", {"filename": "x.md", "content": "abc", "folder": "drafts"}))

    recs = ops.operations_for_run("run-bg")
    assert len(recs) == 1
    assert recs[0]["tool"] == "vault_write" and recs[0]["outcome"] == ops.OK


def test_background_classifies_failures_it_previously_called_success(monkeypatch):
    """`_bg_exec` returned a bare string, so a failed action was indistinguishable from a
    successful one inside an autonomous task."""
    _, fn = _spy("Not found: topics/missing.md")
    monkeypatch.setitem(engine.TOOL_DISPATCH, "vault_read", fn)
    ops.current_run_id.set("run-bg2")

    run(background._bg_exec("vault_read", {"filename": "missing.md", "folder": "topics"}))
    assert ops.operations_for_run("run-bg2")[0]["outcome"] == ops.FAILED


def test_denied_background_tools_stay_denied(monkeypatch):
    """Roadmap acceptance, verified against the REAL allowlist rather than a fixture."""
    denied = sorted(set(engine.TOOL_DISPATCH) - set(background._ALLOWED_TOOLS))
    assert denied, "the allowlist no longer denies anything -- is it still enforced?"

    for name in denied:
        ran, fn = _spy()
        monkeypatch.setitem(engine.TOOL_DISPATCH, name, fn)
        result = run(background._bg_exec(name, {}))
        assert ran == [], f"{name} ran inside a background task despite being denied"
        assert "not available in background tasks" in result


def test_allowed_background_tool_still_runs(monkeypatch):
    """The control: the allowlist must not have become a blanket denial."""
    ran, fn = _spy("results…")
    monkeypatch.setitem(engine.TOOL_DISPATCH, "web_browse", fn)
    result = run(background._bg_exec("web_browse", {"task": "search for x"}))
    assert ran and result == "results…"


def test_background_openai_loop_marks_malformed_arguments():
    """The parser half, in background's own loop -- it carried the identical `= {}` bug
    and nothing had covered it."""
    import inspect
    src = inspect.getsource(background._run_compat_loop)
    assert "invalid" in src and "run_tool(tc.function.name, tool_input, invalid)" in src         and "run_tool = executor or _bg_exec" in src, (
        "background's compat loop no longer forwards unparseable arguments to the "
        "gateway -- malformed calls would execute again")


# ------------------------------------------------- run attribution for autonomous work
def test_background_task_gets_its_own_run_not_the_spawning_turns():
    """Receipts are only useful if attributable. `current_run_id` is a ContextVar, so a
    detached task inherits the turn that spawned it -- a ten-minute job's operations
    would be filed under the three-second turn that started it."""
    ops.current_run_id.set("turn-run")
    with background._own_run("background", "do the thing") as bg_run:
        assert bg_run is not None
        assert ops.current_run_id.get() == bg_run != "turn-run"
    assert ops.current_run_id.get() == "turn-run", "the caller's run id was clobbered"

    channel, preview = db.connect().execute(
        "SELECT channel, user_preview FROM runs WHERE run_id = ?", (bg_run,)).fetchone()
    assert channel == "background" and preview == "do the thing"


def test_night_task_run_id_does_not_leak_into_the_scheduler():
    """`execute_task` is AWAITED by the chronos loop rather than run as its own Task, so
    without the token reset its run id would claim every later operation there."""
    ops.current_run_id.set(None)
    with background._own_run("night_task", "research X"):
        pass
    assert ops.current_run_id.get() is None


def test_failed_autonomous_task_marks_its_run_failed():
    from backend.storage import runs as runs_mod

    with pytest.raises(RuntimeError):
        with background._own_run("background", "doomed") as rid:
            captured = rid
            raise RuntimeError("boom")

    conn = db.connect()
    state, err = conn.execute(
        "SELECT state, error FROM runs WHERE run_id = ?", (captured,)).fetchone()
    assert state == runs_mod.FAILED and "boom" in err


def test_own_run_degrades_when_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    ops.current_run_id.set("before")
    with background._own_run("background", "x") as rid:
        assert rid is None
    assert ops.current_run_id.get() == "before"

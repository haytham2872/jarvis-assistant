"""A04 slice 2: malformed arguments never execute.

Roadmap acceptance: *"A malformed tool call must become a structured validation failure,
not `{}` followed by execution."* Both provider paths did exactly that, so the tests here
drive the REAL parsing branches rather than asserting on a hand-built sentinel only.

The two schema-audit tests at the bottom are the guards that matter most over time: this
layer derives its rules from the registry, so a schema edit can now disable a tool
entirely (a `required` name nobody can satisfy) or keep advertising a parameter the
function cannot accept. Neither was true when this landed; both were verified, not
assumed.
"""
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from backend.brain import engine
from backend.storage import db, operations as ops
from backend.tools import validation
from backend.tools.registry import TOOLS


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


# ------------------------------------------------------------------ unit: validate()
def test_missing_required_argument_is_rejected():
    problem = validation.validate("vault_write", {"filename": "x.md"})
    assert problem and "content" in problem and "folder" in problem


def test_unknown_argument_is_rejected():
    problem = validation.validate(
        "vault_read", {"filename": "x.md", "folder": "topics", "recursive": True})
    assert problem and "recursive" in problem


def test_enum_violation_is_rejected():
    problem = validation.validate("gmail_action", {"action": "delete_everything"})
    assert problem and "delete_everything" in problem


def test_valid_call_passes():
    assert validation.validate("gmail_action", {"action": "list_inbox"}) is None
    assert validation.validate(
        "vault_write", {"filename": "x.md", "content": "c", "folder": "topics"}) is None


def test_non_dict_arguments_are_rejected():
    assert validation.validate("vault_read", "just a string") is not None
    assert validation.validate("vault_read", None) is not None


def test_unknown_tool_is_not_this_layers_problem():
    """Dispatch already reports an unknown tool. Duplicating it here would produce two
    different messages for one condition."""
    assert validation.validate("no_such_tool", {"anything": 1}) is None


def test_rejection_message_blames_the_arguments_not_the_tool():
    """The old behavior told the model the TOOL failed, for a call the model itself had
    malformed -- so it could not correct itself."""
    msg = validation.rejection_message("vault_write", "missing required argument(s): content")
    assert msg.startswith("Error:")            # classifies as a failure by the normal path
    assert "was not called" in msg
    assert "Fix the arguments" in msg


# --------------------------------------------------- the real parsing branches
def _chunk(*, name=None, args=None, tc_id=None):
    fn = SimpleNamespace(name=name, arguments=args)
    tcd = SimpleNamespace(index=0, id=tc_id, function=fn)
    delta = SimpleNamespace(content=None, reasoning_content=None, reasoning=None,
                            tool_calls=[tcd])
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


def _fake_client(chunks):
    async def create(**kwargs):
        return _FakeStream(chunks)

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_openai_path_marks_unparseable_arguments_instead_of_emptying_them():
    """The roadmap's named bug, in `_run_stream_openai`: a truncated argument string
    became `{}` and the tool ran anyway."""
    truncated = '{"filename": "x.md", "content": "half a sent'
    client = _fake_client([_chunk(tc_id="call_1", name="vault_write", args=truncated)])

    _, _, tool_calls = run(engine._run_stream_openai(
        client, "m", [], _noop_send, None, tools=[]))

    assert len(tool_calls) == 1
    call = tool_calls[0]
    assert call["input"] == {}
    assert validation.INVALID_ARGS_KEY in call, (
        "unparseable arguments were silently turned into an empty dict")
    assert "half a sent" in call[validation.INVALID_ARGS_KEY]


def test_openai_path_leaves_well_formed_calls_untouched():
    client = _fake_client([_chunk(tc_id="c1", name="vault_read",
                                  args='{"filename": "x.md", "folder": "topics"}')])
    _, _, tool_calls = run(engine._run_stream_openai(
        client, "m", [], _noop_send, None, tools=[]))
    assert tool_calls[0]["input"] == {"filename": "x.md", "folder": "topics"}
    assert validation.INVALID_ARGS_KEY not in tool_calls[0]


def test_sentinel_never_reaches_the_provider_request():
    """The sentinel rides on the tool-call dict, which is also what the assistant message
    is rebuilt from. Only `input` is serialized -- if that ever changes, a malformed call
    would put an unknown key in the request body and 400 the whole turn."""
    import json as _json
    src = inspect.getsource(engine._chat_compat)
    assert 'json.dumps(tc["input"])' in src, (
        "the assistant message no longer serializes only tc['input'] -- re-check that "
        "the validation sentinel cannot leak into the outgoing request")
    del _json


# ------------------------------------------------- end to end through _exec_tool
def _spy():
    ran = []

    async def fn(**kw):
        ran.append(kw)
        return "the tool actually executed"

    return ran, fn


def test_malformed_call_never_reaches_the_tool(monkeypatch):
    ran, fn = _spy()
    monkeypatch.setitem(engine.TOOL_DISPATCH, "vault_write", fn)
    ops.current_run_id.set("run-1")

    tc = {"id": "1", "name": "vault_write", "input": {},
          validation.INVALID_ARGS_KEY: '{"filename": "x.md", "content": "trunc'}
    _, result, ok = run(engine._exec_tool(tc, _noop_send))

    assert ran == [], "a malformed call executed"
    assert ok is False
    assert "not valid JSON" in result

    rec = ops.operations_for_run("run-1")[0]
    assert rec["outcome"] == ops.FAILED


def test_schema_violation_never_reaches_the_tool(monkeypatch):
    ran, fn = _spy()
    monkeypatch.setitem(engine.TOOL_DISPATCH, "gmail_action", fn)
    ops.current_run_id.set("run-2")

    tc = {"id": "1", "name": "gmail_action", "input": {"action": "delete_everything"}}
    _, result, ok = run(engine._exec_tool(tc, _noop_send))

    assert ran == []
    assert ok is False
    assert "delete_everything" in result
    assert ops.operations_for_run("run-2")[0]["outcome"] == ops.FAILED


def test_valid_call_still_executes(monkeypatch):
    """The control. Validation that rejects good calls is worse than none."""
    ran, fn = _spy()
    monkeypatch.setitem(engine.TOOL_DISPATCH, "gmail_action", fn)
    tc = {"id": "1", "name": "gmail_action", "input": {"action": "list_inbox"}}
    _, result, ok = run(engine._exec_tool(tc, _noop_send))
    assert ran and ok is True and result == "the tool actually executed"


def test_rejection_is_reported_as_an_error_to_the_tool_loop(monkeypatch):
    """`ok=False` is what makes the executor treat this as a failed step (and, under a
    plan, what can trigger a replan) rather than accepting garbage as a result."""
    _, fn = _spy()
    monkeypatch.setitem(engine.TOOL_DISPATCH, "vault_read", fn)
    tc = {"id": "1", "name": "vault_read", "input": {}}
    _, result, ok = run(engine._exec_tool(tc, _noop_send))
    assert ok is False
    assert engine._is_error_result(result, "vault_read") is True


# ------------------------------------------------------------- registry guards
def test_no_tool_requires_an_argument_it_does_not_define():
    """A `required` name absent from `properties` would make EVERY call to that tool
    unsatisfiable -- validation would reject all of them, silently disabling the tool."""
    broken = []
    for tool in TOOLS:
        schema = tool.get("input_schema") or {}
        props = set(schema.get("properties") or {})
        missing = set(schema.get("required") or []) - props
        if missing:
            broken.append((tool["name"], sorted(missing)))
    assert broken == [], f"unsatisfiable required arguments: {broken}"


def test_no_tool_advertises_a_parameter_its_function_cannot_accept():
    """The mirror hazard, which validation cannot catch: the model follows the schema,
    sends the parameter, and the call dies with a TypeError blamed on the tool."""
    mismatched = []
    for tool in TOOLS:
        fn = engine.TOOL_DISPATCH.get(tool["name"])
        if fn is None:
            continue
        params = inspect.signature(fn).parameters
        if any(p.kind == p.VAR_KEYWORD for p in params.values()):
            continue
        props = set((tool.get("input_schema") or {}).get("properties") or {})
        extra = props - set(params)
        if extra:
            mismatched.append((tool["name"], sorted(extra)))
    assert mismatched == [], f"schema advertises unaccepted parameters: {mismatched}"

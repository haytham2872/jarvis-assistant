"""A04 slice 5: replans replace old instructions.

Roadmap issue: *"The plan shown to the model can lag the actual plan."* Acceptance:
*"replans replace old instructions."*

It lagged completely. The plan block was written into the system prompt once, at turn
start, and never touched again. A replan updated the local `plan`, emitted `plan_update`
to the UI and DM'd Sam the new steps — but the model kept reading the ORIGINAL steps
under the heading "your authorized scope for this turn", while the `request_replan` tool
result told it "Plan revised — continue from there." It was instructed to continue from
a plan it could not see, against instructions that had been revoked.

These tests drive the real `_chat_compat` loop with only the streaming client faked, and
inspect the system message of the request that actually goes out next.
"""
import asyncio

import pytest

from backend.brain import engine


def run(coro):
    return asyncio.run(coro)


ORIGINAL = [
    {"id": 1, "label": "Email the summary to Sam", "tool": "gmail_action",
     "parallel_group": None},
    {"id": 2, "label": "Confirm delivery", "tool": None, "parallel_group": None},
]
REVISED = [
    {"id": 2, "label": "Save the summary to the vault instead", "tool": "vault_write",
     "parallel_group": None},
]


async def _noop_send(_):
    pass


@pytest.fixture
def harness(monkeypatch):
    """Drive the real loop; capture the system prompt of every outgoing request."""
    seen: list[str] = []
    state = {"n": 0, "scripts": []}

    async def fake_plan(*a, **k):
        return [dict(s) for s in ORIGINAL]

    async def fake_replan(*a, **k):
        return [dict(s) for s in REVISED]

    async def fake_stream(client, model, messages, send, sq, temperature=None,
                          tools=None, extra_body=None, *, purpose="chat",
                          iteration=None, plan_active=False, max_tokens=None):
        seen.append(messages[0]["content"])
        i = state["n"]
        state["n"] += 1
        if i < len(state["scripts"]):
            return state["scripts"][i]
        return "done", "", []

    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    monkeypatch.setattr(engine, "_plan_task", fake_plan)
    monkeypatch.setattr(engine, "_replan", fake_replan)
    monkeypatch.setattr(engine, "_run_stream_openai", fake_stream)
    return seen, state


def _drive(scripts, state, channel="local"):
    state["scripts"] = scripts
    return run(engine._chat_compat(
        "thinking-model", "SYSTEM CORE.", [{"role": "user", "content": "email it"}],
        _noop_send, None, tools=[], user_text="email the summary to me",
        channel=channel, use_thinking=True))


def _plan_block(system: str) -> str:
    i = system.find("[Execution Plan")
    return system[i:] if i != -1 else ""


REPLAN_CALL = ("", "", [{"id": "c1", "name": "request_replan",
                         "input": {"reason": "email is unavailable"}}])


def test_the_model_reads_the_revised_plan_after_a_deliberate_replan(harness):
    """The reproduced bug."""
    seen, state = harness
    _drive([REPLAN_CALL], state)

    after = _plan_block(seen[-1])
    assert "Save the summary to the vault instead" in after, (
        "the revised plan never reached the model -- only the UI and the DM")
    assert "Email the summary to Sam" not in after, (
        "the model still sees a revoked instruction as its authorized scope")


def test_the_revision_is_named_as_superseding(harness):
    """The model should not have to infer supersession from the text quietly changing."""
    seen, state = harness
    _drive([REPLAN_CALL], state)
    after = _plan_block(seen[-1])
    assert "REVISION 1" in after and "replaces every earlier plan" in after


def test_the_first_request_has_no_revision_marker(harness):
    seen, state = harness
    _drive([REPLAN_CALL], state)
    first = _plan_block(seen[0])
    assert "REVISION" not in first
    assert "Email the summary to Sam" in first


def test_an_error_triggered_replan_also_updates_the_model(monkeypatch, harness):
    """Both replan triggers had the same gap -- fixing only the deliberate one would
    leave the more common path stale."""
    seen, state = harness

    async def failing_tool(**kw):
        return "Error: mailbox unavailable"

    monkeypatch.setitem(engine.TOOL_DISPATCH, "gmail_action", failing_tool)
    # a read: a send would (correctly) become an approval request, not an error (P1)
    _drive([("", "", [{"id": "c1", "name": "gmail_action",
                       "input": {"action": "search_emails", "query": "summary"}}])], state)

    after = _plan_block(seen[-1])
    assert "Save the summary to the vault instead" in after
    assert "REVISION 1" in after


def test_the_base_system_prompt_survives_the_rewrite(harness):
    """Only the plan block is rebuilt; everything before it must be byte-identical, or
    the rewrite would silently drop context and bust the cached prefix."""
    seen, state = harness
    _drive([REPLAN_CALL], state)
    for system in seen:
        assert system.startswith("SYSTEM CORE.")


def test_completed_steps_are_marked_so_continue_from_there_means_something(harness,
                                                                           monkeypatch):
    """`done_through` is the step evidence the roadmap asks plan versions to be bound to."""
    out = engine._format_plan_for_system(ORIGINAL, revision=2, done_through=1)
    assert "Email the summary to Sam (tool: gmail_action) — DONE" in out
    assert "You are on step 2." in out
    assert "Confirm delivery (tool: no tool)" in out
    assert "— DONE" not in out.split("Confirm delivery")[1]


def test_formatter_is_unchanged_for_the_initial_plan():
    """Revision 0 must render exactly as before -- this block is on every planned turn,
    and a gratuitous change would invalidate the cached prefix for all of them."""
    out = engine._format_plan_for_system(ORIGINAL)
    assert out.startswith("\n\n[Execution Plan — your authorized scope for this turn]")
    assert "REVISION" not in out and "DONE" not in out


def test_sync_is_a_no_op_without_a_system_message():
    """Defensive: never index blindly into a message list shaped differently."""
    msgs = [{"role": "user", "content": "hi"}]
    engine._sync_plan_into_system(msgs, "base", ORIGINAL, revision=1, done_through=0)
    assert msgs == [{"role": "user", "content": "hi"}]
    engine._sync_plan_into_system([], "base", ORIGINAL, revision=1, done_through=0)


def test_two_replans_produce_two_revisions(harness):
    seen, state = harness
    _drive([REPLAN_CALL, REPLAN_CALL], state)
    assert "REVISION 1" in _plan_block(seen[1])
    assert "REVISION 2" in _plan_block(seen[2])


def test_replan_past_the_cap_does_not_bump_the_revision(harness):
    """Past the cap the turn finishes with the plan it has -- claiming a revision that
    did not happen would be its own lie."""
    seen, state = harness
    _drive([REPLAN_CALL] * (engine._MAX_REPLANS_PER_TURN + 1), state)
    last = _plan_block(seen[-1])
    assert f"REVISION {engine._MAX_REPLANS_PER_TURN}" in last
    assert f"REVISION {engine._MAX_REPLANS_PER_TURN + 1}" not in last

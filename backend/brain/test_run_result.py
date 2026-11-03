"""G0 / D2: a background job never returns its own narration as the result.

Reproduced on e2467c3 (docs/archive/GOALS-ROADMAP.md, Appendix A): the compat loop kept the last
non-empty content of ANY call, so when the final, tool-less call came back empty and cut off
(finish_reason "length"), the job's result was "Let me look up the fees first." -- written
to drafts/ and DM'd to Sam as the finished work. A cut-off answer, an empty one and a
loop that ran out of steps were all treated as done.

The loop now returns a typed RunResult. These tests assert its status AND its text, so a
regression that merely returns some other wrong string still fails.
"""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from backend.brain import background as B
from backend.brain import engine as C
from backend.brain import router

NARRATION = "Let me look up the fees first."


def run(coro):
    return asyncio.run(coro)


def _resp(content, tool_calls=None, finish="stop", reasoning=None, completion=10):
    message = NS(content=content, tool_calls=tool_calls, reasoning_content=reasoning,
                 reasoning=None)
    usage = NS(prompt_tokens=100, completion_tokens=completion, cost=None,
               prompt_tokens_details=None, completion_tokens_details=None)
    return NS(choices=[NS(message=message, finish_reason=finish)], usage=usage, model="m")


def _call(cid="c1"):
    return NS(id=cid, function=NS(name="web_browse", arguments='{"task":"ETF fees"}'))


@pytest.fixture
def loop(monkeypatch):
    """The real _run_compat_loop with only the provider client and web_browse faked.
    Returns the list of requests sent, and a setter for the scripted responses."""
    monkeypatch.setattr(B, "_with_skills", lambda system, text: (system, text))
    monkeypatch.setattr(B, "_stop_if_paused", lambda: None)
    sent: list[dict] = []
    script: list = []

    async def create(**kwargs):
        sent.append(json.loads(json.dumps(kwargs, default=str)))
        return script.pop(0)

    async def browse(**kwargs):
        return "Search results: TER 0.07%"
    monkeypatch.setattr(C, "_get_compat_client", lambda: NS(chat=NS(completions=NS(create=create))))
    monkeypatch.setitem(C.TOOL_DISPATCH, "web_browse", browse)
    return sent, script


def test_appendix_a_empty_cut_off_final_is_not_the_narration(loop, monkeypatch):
    sent, script = loop
    monkeypatch.setattr(B, "_MAX_BG_ITERATIONS", 2)
    script += [_resp(NARRATION, [_call()]), _resp("", finish="length"),
               _resp("", finish="length")]                 # the one recovery call fails too
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert out.status == B.INCOMPLETE
    assert out.text == ""
    assert out.ending == "cut_off"
    assert NARRATION not in out.text


def test_an_empty_final_gets_one_recovery_call_without_tools(loop):
    sent, script = loop
    script += [_resp(NARRATION, [_call()]), _resp("", finish="length"),
               _resp("The cheapest is CSPX at 0.07%.")]
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert (out.status, out.ending) == (B.COMPLETE, "recovered")
    assert out.text == "The cheapest is CSPX at 0.07%."
    recovery = sent[-1]
    assert "tools" not in recovery
    assert recovery["extra_body"]["reasoning"] == {"enabled": False}
    assert recovery["max_tokens"] == B._RESCUE_MAX_TOKENS
    # tool-result sequencing stays valid: assistant tool call, its result, then the nudge
    roles = [m["role"] for m in recovery["messages"]]
    assert roles[-3:] == ["assistant", "tool", "user"]
    assert recovery["messages"][-2]["tool_call_id"] == "c1"
    assert len(sent) == 3


def test_reasoning_only_cut_off_is_treated_as_no_answer(loop):
    sent, script = loop
    script += [_resp(None, finish="length", reasoning="hmm, let me think " * 50),
               _resp(None, finish="stop")]
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert (out.status, out.text, out.ending) == (B.INCOMPLETE, "", "cut_off")
    assert "hmm" not in out.text


def test_an_empty_stop_is_incomplete_not_done(loop):
    sent, script = loop
    script += [_resp(NARRATION, [_call()]), _resp("", finish="stop"), _resp("")]
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert (out.status, out.text, out.ending) == (B.INCOMPLETE, "", "empty")


def test_a_cut_off_answer_is_continued_once(loop):
    sent, script = loop
    script += [_resp("The three funds are CSPX, VUAA and", finish="length"),
               _resp(" SPXS; all track the index within 0.1%.")]
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert (out.status, out.ending) == (B.COMPLETE, "continued")
    assert out.text == "The three funds are CSPX, VUAA and SPXS; all track the index within 0.1%."
    cont = sent[-1]
    assert "tools" not in cont
    assert cont["messages"][-2] == {"role": "assistant",
                                    "content": "The three funds are CSPX, VUAA and"}
    assert cont["messages"][-1]["content"] == C._CONTINUE_ANSWER_NUDGE


def test_a_continuation_cut_off_again_is_incomplete_with_the_partial_text(loop):
    sent, script = loop
    script += [_resp("Part one", finish="length"), _resp(" part two", finish="length")]
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert (out.status, out.ending) == (B.INCOMPLETE, "cut_off")
    assert out.text == "Part one part two"
    assert len(sent) == 2                                     # one continuation, never more


def test_running_out_of_steps_is_incomplete_with_no_narration(loop, monkeypatch):
    sent, script = loop
    monkeypatch.setattr(B, "_MAX_BG_ITERATIONS", 2)
    # a host that returns a tool call even on the tool-less last step
    script += [_resp(NARRATION, [_call("c1")]), _resp(NARRATION, [_call("c2")])]
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert (out.status, out.text, out.ending) == (B.INCOMPLETE, "", "capped")


def test_an_ordinary_final_report_is_complete(loop):
    sent, script = loop
    script += [_resp(NARRATION, [_call()]), _resp("CSPX: 0.07%, Irish-domiciled.")]
    out = run(B._run_compat_loop("Compare ETF fees"))
    assert (out.status, out.ending, out.finish_reason) == (B.COMPLETE, "answer", "stop")
    assert out.text == "CSPX: 0.07%, Irish-domiciled."
    assert out.output_tokens == 20 and out.calls == 2


def test_output_tokens_count_the_recovery_too(loop):
    sent, script = loop
    script += [_resp("", finish="length", completion=4096), _resp("ok", completion=7)]
    out = run(B._run_compat_loop("x"))
    assert out.output_tokens == 4103 and out.calls == 2


# --- the worker and the legacy string API --------------------------------------------

def _worker(monkeypatch, result):
    async def runner(text, **kw):
        return result
    monkeypatch.setattr(B, "_run_compat_loop", runner)
    notes: list = []

    async def note(t):
        notes.append(t.id)
    monkeypatch.setattr(B, "_write_result_note", note)
    t = B.BgTask(id=901, description="compare ETFs", status="running")
    run(B._worker(t))
    return t, notes


def test_the_worker_records_an_incomplete_run_as_failed_with_no_note(monkeypatch):
    t, notes = _worker(monkeypatch, B.RunResult(text="", status=B.INCOMPLETE, ending="cut_off",
                                                 finish_reason="length"))
    assert t.status == "failed"
    assert "cut off" in t.summary and NARRATION not in t.summary
    assert notes == []


def test_the_worker_keeps_partial_text_but_still_calls_it_incomplete(monkeypatch):
    t, notes = _worker(monkeypatch, B.RunResult(text="Half a report", status=B.INCOMPLETE,
                                                 ending="cut_off"))
    assert t.status == "failed" and t.result == "Half a report"
    assert "Half a report" in t.summary and t.summary.startswith("Incomplete")


def test_the_worker_still_accepts_a_plain_string_from_a_legacy_runner(monkeypatch):
    t, notes = _worker(monkeypatch, "a full answer")
    assert t.status == "done" and t.result == "a full answer" and notes == [901]


def test_execute_task_raises_on_an_incomplete_run(monkeypatch):
    async def runner(text, **kw):
        return B.RunResult(text="", status=B.INCOMPLETE, ending="empty")
    monkeypatch.setattr(B, "_run_compat_loop", runner)
    with pytest.raises(B.IncompleteRun) as exc:
        run(B.execute_task("night work"))
    assert exc.value.result.ending == "empty"


def test_execute_task_still_returns_the_text_of_a_complete_run(monkeypatch):
    async def runner(text, **kw):
        return B.RunResult(text="findings", status=B.COMPLETE)
    monkeypatch.setattr(B, "_run_compat_loop", runner)
    assert run(B.execute_task("night work")) == "findings"

"""Tool-call markup written as text never reaches Sam as part of an answer.

Live (Sam, 15 October): a background job's result in the phone app opened with
"<｜DSML｜ calls> <｜DSML｜ invoke name="vault_write"> ... string="true" ...". Reproduced
the same day against V4.1 Flash: on a tool-less last step after a tool call, 3 of 4 trials
wrote the call it still wanted to make as text, after a line announcing it ("Saving the note
now:"). Nothing ran. The samples below are those outputs, shortened.
"""
import asyncio

from backend.brain import background as B
from backend.brain import engine
from backend.brain import tool_markup as tm
from backend.brain.test_run_result import _call, _resp, loop  # noqa: F401  (fixture)

LIVE = (
    "I checked both facts via a web search:\n\n"
    "- **Completed:** 1889.\n- **Height:** 330 m including the antennas.\n\n"
    "Saving the note now:\n\n"
    "<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"vault_write\">\n"
    "<｜DSML｜ parameter name=\"path\" string=\"true\">Notes/Eiffel Tower - Facts.md</｜DSML｜ parameter>\n"
    "<｜DSML｜ parameter name=\"content\" string=\"true\"># Eiffel Tower\n\n- **Year:** 1889\n"
    "</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>"
)
ANSWER = "I checked both facts via a web search:\n\n- **Completed:** 1889.\n- **Height:** 330 m including the antennas."


def run(coro):
    return asyncio.run(coro)


def test_the_live_shape_loses_the_block_and_the_line_announcing_it():
    assert tm.strip(LIVE) == ANSWER


def test_text_without_markup_is_returned_unchanged():
    plain = "Note: the 330 m figure includes antennas.\n\nAsk me again: I can check."
    assert tm.strip(plain) is plain or tm.strip(plain) == plain
    assert not tm.has_markup(plain)


def test_a_block_cut_off_at_the_output_limit_is_removed_to_the_end():
    cut = "The answer is 42.\n\n---\n\nSaving note to the vault:\n\n<｜DSML｜ calls>\n<｜DSML｜ invoke name=\"vault_write\">\n<｜DSML｜ parameter"
    assert tm.strip(cut) == "The answer is 42."


def test_markup_at_the_start_leaves_the_answer_after_it():
    first = ("<｜DSML｜function_calls>\n<｜DSML｜invoke name=\"vault_write\">\n</｜DSML｜invoke>\n"
             "</｜DSML｜function_calls>\n\nHere is what I found: the lease ends in June.")
    assert tm.strip(first) == "Here is what I found: the lease ends in June."


def test_ascii_bars_and_stray_tags_are_removed_too():
    assert tm.strip("Done.<|DSML| invoke name=\"x\">") == "Done."


def test_an_answer_that_is_only_markup_becomes_empty():
    only = LIVE[LIVE.index("<｜DSML｜ calls>"):]
    assert tm.strip(only) == ""


def test_a_colon_line_far_from_the_block_is_kept():
    text = "Two options:\n- A\n- B\n\nI'd pick A.\n\n<｜DSML｜ calls>\n</｜DSML｜ calls>"
    assert tm.strip(text) == "Two options:\n- A\n- B\n\nI'd pick A."


# ------------------------------------------------------------------ the real loops

def test_a_background_job_returns_the_answer_without_the_markup(loop):  # noqa: F811
    sent, script = loop
    script += [_resp("Looking it up.", [_call()]), _resp(LIVE)]
    out = run(B._run_compat_loop("Cross-check two facts"))
    assert (out.status, out.ending) == (B.COMPLETE, "answer")
    assert out.text == ANSWER


def test_a_background_final_that_is_only_markup_asks_once_for_a_real_answer(loop):  # noqa: F811
    sent, script = loop
    only = LIVE[LIVE.index("<｜DSML｜ calls>"):]
    script += [_resp("Looking it up.", [_call()]), _resp(only),
               _resp("1889, and 330 m with antennas.")]
    out = run(B._run_compat_loop("Cross-check two facts"))
    assert (out.status, out.ending, out.text) == (B.COMPLETE, "recovered", "1889, and 330 m with antennas.")


def test_the_live_turn_returns_the_answer_without_the_markup(monkeypatch):
    turns = [("Let me check.", "", [{"id": "1", "name": "web_browse", "input": {"task": "x"}}]),
             (LIVE, "", [])]
    calls = {"n": 0}

    async def fake_stream(client, model, messages, send, sentence_queue, temperature=None, tools=None,
                          extra_body=None, *, purpose="chat", iteration=None, plan_active=False,
                          max_tokens=None):
        calls["n"] += 1
        return turns[calls["n"] - 1]

    async def fake_exec(tc, send):
        return tc, "1889; 330 m", True

    monkeypatch.setattr(engine, "_run_stream_openai", fake_stream)
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    monkeypatch.setattr(engine, "_exec_tool", fake_exec)

    async def send(msg):
        pass
    out = run(engine._chat_compat(engine._provider().standard, "system",
                                  [{"role": "user", "content": "check"}], send, None, user_text=""))
    assert out == ANSWER


def test_the_recovery_answer_is_cleaned_too(loop):  # noqa: F811
    sent, script = loop
    only = LIVE[LIVE.index("<｜DSML｜ calls>"):]
    script += [_resp("Looking it up.", [_call()]), _resp(only), _resp(LIVE)]
    out = run(B._run_compat_loop("Cross-check two facts"))
    assert (out.status, out.text) == (B.COMPLETE, ANSWER)

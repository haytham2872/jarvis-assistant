"""The loop guard (20 October 2026): a voice note transcribed as "St. Wam" sent one call into
six and a half minutes of reasoning with no answer, until the server was restarted. A call
whose reasoning repeats itself, or runs past _THINK_LIMIT_S with no answer, is stopped, and
the turn gets one direct-answer call -- the recovery a thought cut off at the output limit
already had."""
import asyncio
import itertools
import types

from backend.brain import engine


def _delta(content=None, reasoning=None):
    return types.SimpleNamespace(content=content, reasoning=reasoning,
                                 reasoning_content=None, tool_calls=None)


class _Stream:
    """Records how much was read and whether it was closed."""
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False
        self.taken = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            d = next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration
        self.taken += 1
        return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=d, finish_reason=None)],
                                     usage=None)

    async def close(self):
        self.closed = True


def _client(stream):
    async def create(**kw):
        return stream
    return types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))


async def _noop(msg):
    pass


def _run(stream):
    return asyncio.run(engine._run_stream_openai(
        _client(stream), "m", [{"role": "user", "content": "St. Wam"}], _noop, None, tools=[]))


LOOP = ("Wait, St. Wam could be a place, or maybe a person he knows. Let me reconsider what he "
        "means by it before answering, because it does not match anything I have. ")


def test_reasoning_that_repeats_itself_is_stopped():
    stream = _Stream([_delta(reasoning=LOOP)] * 200)
    result = _run(stream)
    assert result.finish_reason == "loop" and stream.closed
    assert result[0] == "" and stream.taken < 40


def test_reasoning_that_runs_on_too_long_is_stopped(monkeypatch):
    clock = itertools.count(0, 10)            # ten "seconds" per call to time.time()
    monkeypatch.setattr(engine.time, "time", lambda: next(clock))
    stream = _Stream([_delta(reasoning=f"thought number {i}. ") for i in range(200)])
    result = _run(stream)
    assert result.finish_reason == "loop" and stream.closed


def test_a_long_varied_thought_that_answers_is_left_alone():
    thoughts = [_delta(reasoning=f"Step {i}: the fee on fund {i} is {i * 7 % 13} basis points. ")
                for i in range(3000)]
    stream = _Stream(thoughts + [_delta(content="The answer.")])
    result = _run(stream)
    assert result[0] == "The answer." and result.finish_reason != "loop" and not stream.closed


def test_reasoning_after_the_answer_started_is_not_checked():
    stream = _Stream([_delta(content="Answer: ")] + [_delta(reasoning=LOOP)] * 30)
    result = _run(stream)
    assert result.finish_reason != "loop" and result[0] == "Answer: "


def test_a_stopped_call_gets_one_direct_answer(monkeypatch):
    """On either tier: the stuck call in the incident may not have been the thinking tier."""
    from backend.brain import router
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    for use_thinking in (True, False):
        seen = []

        async def fake(client, model, messages, send, sentence_queue, temperature=None,
                       tools=None, extra_body=None, *, purpose="chat", iteration=None,
                       plan_active=False, max_tokens=None):
            seen.append(messages[-1]["content"])
            if len(seen) == 1:
                return engine._StreamResult(("", LOOP * 3, []), "loop")
            return ("What do you mean by St. Wam?", "", [])
        monkeypatch.setattr(engine, "_run_stream_openai", fake)
        result = asyncio.run(engine._chat_compat(
            router.OPENROUTER_THINKING if use_thinking else router.OPENROUTER_STANDARD,
            "system", [{"role": "user", "content": "St. Wam"}], _noop, None, user_text="",
            use_thinking=use_thinking))
        assert result == "What do you mean by St. Wam?" and "reconsider" not in result
        assert seen[1] == engine._DIRECT_ANSWER_NUDGE and len(seen) == 2


def test_kimi_never_sends_a_stopped_loop_as_its_answer(monkeypatch):
    from backend.brain import router
    monkeypatch.setattr(engine, "_get_compat_client", lambda: object())
    turns = iter([engine._StreamResult(("", LOOP * 3, []), "loop"), ("Direct.", "", [])])

    async def fake(*a, **k):
        return next(turns)
    monkeypatch.setattr(engine, "_run_stream_openai", fake)
    assert asyncio.run(engine._chat_compat(
        router.OPENROUTER_THINKING, "system", [{"role": "user", "content": "x"}], _noop, None,
        user_text="", use_thinking=True)) == "Direct."


def test_a_voice_note_says_to_ask_about_a_word_that_makes_no_sense():
    assert "makes no sense" in engine._VOICE_NOTE


def test_a_stopped_call_still_counts_what_it_generated(monkeypatch):
    """Closing the stream early means the final usage chunk never arrives. The reasoning
    already generated was billed: it must reach the turn budget, /cost and llm_calls."""
    from backend.brain import usage
    from backend.storage import llm_calls
    usage.reset()
    rows = []
    monkeypatch.setattr(llm_calls, "record", lambda **kw: rows.append(kw))
    stream = _Stream([_delta(reasoning=LOOP)] * 200)
    result = _run(stream)
    generated = sum(len(LOOP) for _ in range(stream.taken))
    assert result.finish_reason == "loop"
    assert result.output_tokens and result.output_tokens >= generated / 4
    assert usage.session_summary() and "m:" in usage.session_summary()
    fields = llm_calls.usage_fields(rows[-1]["usage"])
    assert fields["output_tokens"] == result.output_tokens
    assert fields["reasoning_tokens"] == result.output_tokens


def test_a_call_that_reports_usage_is_not_estimated(monkeypatch):
    from backend.brain import usage
    usage.reset()
    real = types.SimpleNamespace(prompt_tokens=10, completion_tokens=7,
                                 prompt_tokens_details=None, cost=None)

    class _Done(_Stream):
        async def __anext__(self):
            if self.taken == 1:
                self.taken += 1
                return types.SimpleNamespace(choices=[], usage=real)
            return await super().__anext__()
    result = _run(_Done([_delta(content="Fine.")]))
    assert result.output_tokens == 7

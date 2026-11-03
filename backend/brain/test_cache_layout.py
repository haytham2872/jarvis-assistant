"""The OpenAI-compatible request is laid out for prefix caching.

Providers cache the unchanged beginning of a request. The volatile block starts with the
[Now] clock line, so it used to break the cache before the tool schemas and the whole
history. Measured live (5-turn conversation, real prompt, all 24 tools): 35% cached and
$0.0140 with the volatile block in the system message; 84% cached and $0.0046 with it on
the latest user message.
"""
import inspect

from backend.brain import engine, instructions

HISTORY = [{"role": "user", "content": "earlier question"},
           {"role": "assistant", "content": "earlier answer"}]


def compose(volatile="[Now] Wednesday 12:15\n\n[Memory]\n- a fact", history=HISTORY):
    return engine._compose_compat_request("STABLE CORE", volatile, list(history),
                                          "what now?", "discord")


def test_the_system_message_is_only_the_stable_block():
    system, _ = compose()
    assert system == "STABLE CORE"


def test_the_volatile_block_rides_on_the_latest_user_message_after_his_words():
    _, messages = compose()
    last = messages[-1]["content"]
    assert last.startswith("what now?\n\n" + engine._TURN_CONTEXT_HEADER)
    assert "[Now] Wednesday 12:15" in last and "- a fact" in last


def test_history_is_sent_unchanged_so_it_stays_a_cached_prefix():
    _, messages = compose()
    assert messages[:-1] == HISTORY


def test_consecutive_turns_share_the_whole_earlier_request_as_a_prefix():
    """The property the cache needs: turn N+1 starts with everything turn N sent,
    except turn N's own context block, which only ever sat at the very end."""
    system1, turn1 = compose(volatile="[Now] 12:15")
    turn2_history = HISTORY + [{"role": "user", "content": "what now?"},
                               {"role": "assistant", "content": "reply"}]
    system2, turn2 = compose(volatile="[Now] 12:16", history=turn2_history)
    assert system1 == system2
    assert turn2[:len(turn1) - 1] == turn1[:-1]


def test_no_volatile_block_means_no_empty_context_header():
    _, messages = compose(volatile="")
    assert messages[-1]["content"] == "what now?"


def test_standing_rules_sit_right_against_his_words_before_the_context(monkeypatch):
    """Measured: rules after the context block were followed 1/3 (3-15 sentence
    replies to a two-sentence rule); right after his words, 6/6."""
    monkeypatch.setattr(instructions, "turn_note", lambda channel=None: "[Standing] be brief")
    _, messages = compose()
    content = messages[-1]["content"]
    assert content.startswith("what now?\n\n[Standing] be brief\n\n" + engine._TURN_CONTEXT_HEADER)


def test_chat_uses_the_layout_and_stores_only_the_plain_text():
    src = inspect.getsource(engine.chat)
    assert ("_compose_compat_request(\n        system_stable, system_volatile, history, user_text, channel, "
            "image_uris)") in src
    assert "system_stable + (\"\\n\\n\" + system_volatile" not in src     # old layout gone
    assert "await _ingest_mem(model, user_text, full_response)" in src
    assert "_record_thread_task(user_text, full_response," in src

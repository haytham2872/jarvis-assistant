"""H1: a research synthesis cut off by its own output limit is not the note's failure.

The failure hold (`_note_pass_outcome`) parks a note that keeps failing for 1 to 7 days.
A synthesis still unfinished after its one continuation ran out of OUR limit, so it is
logged as a skip, which the hold counts as neither success nor failure.
"""
import asyncio
import json

from backend.brain import research


def run(coro):
    return asyncio.run(coro)


def _pass(tmp_path, monkeypatch, synth_reply):
    topics = tmp_path / "topics"
    topics.mkdir()
    (topics / "camus.md").write_text("# Camus\n\n" + "Revolt is a response. " * 80,
                                     encoding="utf-8")
    monkeypatch.setattr(research, "_TOPICS_DIR", topics)
    monkeypatch.setattr(research, "_pop_queued", lambda: {
        "subject": "Camus", "angle": "deepen", "reason": "Explain revolt"})

    async def llm(_prompt, **_kwargs):
        return synth_reply

    async def search(_query):
        return "Camus primary text https://example.org/camus"

    async def write(filename, body, folder, **_kwargs):
        return "wrote"

    outcomes = []
    from backend.tools import browser, vault
    monkeypatch.setattr(research, "_llm", llm)
    monkeypatch.setattr(research, "_log_research_pass",
                        lambda subject, angle, outcome, **kw: outcomes.append(outcome))
    monkeypatch.setattr(browser, "web_browse", search)
    monkeypatch.setattr(vault, "vault_write", write)
    monkeypatch.setattr(vault, "note_revision", lambda *_args: None)
    run(research._tick())
    return outcomes


def test_a_synthesis_still_cut_off_is_a_skip_not_a_failure(tmp_path, monkeypatch):
    # Unclosed JSON, and the continuation returns the same unfinished shape.
    cut = '{"progress": true, "changed_understanding": "x", "note": "# Camus\n\nRevolt'
    assert _pass(tmp_path, monkeypatch, cut) == [research._CUT_OFF_OUTCOME]


def test_the_skip_does_not_count_toward_the_failure_hold(monkeypatch):
    state = {}
    monkeypatch.setattr(research, "_update_state", lambda mutate: mutate(state))
    research._note_pass_outcome("camus.md", research._CUT_OFF_OUTCOME, 1000.0)
    assert state == {}


def test_an_unusable_reply_that_was_not_cut_off_still_fails(tmp_path, monkeypatch):
    outcomes = _pass(tmp_path, monkeypatch, "I could not produce JSON this time.")
    assert outcomes == ["failed: synthesis unusable"]

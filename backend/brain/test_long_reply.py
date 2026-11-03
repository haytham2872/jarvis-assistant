"""H4: a long Discord answer is saved in full and summarised in the chat."""
import asyncio
import datetime

import pytest

from backend.brain import long_reply, research
from backend.tools import vault


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    for folder in vault.FOLDERS:
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


_LONG = "\n\n".join(f"Paragraph {i}: " + "the thesis holds on demand. " * 12 for i in range(20))


def test_a_short_answer_is_untouched(tmp_vault, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("no summary call for a short answer")
    monkeypatch.setattr(research, "_llm", boom)
    assert run(long_reply.shape_for_discord("Short answer.", "hi")) == "Short answer."
    assert not any((tmp_vault / "shared").iterdir())


def test_a_long_answer_is_saved_whole_and_summarised(tmp_vault, monkeypatch):
    async def summary(prompt, max_tokens=0):
        assert _LONG in prompt
        return "It holds. Demand is intact."
    monkeypatch.setattr(research, "_llm", summary)
    out = run(long_reply.shape_for_discord(_LONG, "compile the NVDA research into a conclusion"))
    (saved,) = list((tmp_vault / "shared").iterdir())
    assert saved.name.endswith("-compile-the-nvda-research-into-a-conclusion.md")
    assert _LONG in saved.read_text(encoding="utf-8")
    assert out.startswith("It holds. Demand is intact.")
    assert f"shared/{saved.name}" in out and len(out) < 1000


def test_a_failed_summary_falls_back_to_the_opening(tmp_vault, monkeypatch):
    async def empty(prompt, max_tokens=0):
        return ""
    monkeypatch.setattr(research, "_llm", empty)
    out = run(long_reply.shape_for_discord(_LONG, "compile it"))
    assert out.startswith("Paragraph 0:") and "shared/" in out and len(out) < 2000


def test_a_failed_save_sends_the_full_answer(tmp_vault, monkeypatch):
    async def refused(*a, **k):
        return "Error: disk full"
    monkeypatch.setattr(vault, "vault_write", refused)
    assert run(long_reply.shape_for_discord(_LONG, "compile it")) == _LONG


def test_note_name_is_dated_and_safe():
    name = long_reply.note_name("What's the ../../etc thesis?", datetime.datetime(2026, 9, 28))
    assert name == "2026-09-28-what-s-the-etc-thesis.md"


def test_the_discord_path_shapes_the_reply(monkeypatch):
    """History keeps the full answer (it is appended inside _execute_turn); only what
    run_pipeline_from_discord returns to the bridge is shaped."""
    from backend import main
    seen = {}

    async def turn(text, channel="local", want_tts=True):
        return _LONG

    async def shape(answer, request):
        seen["args"] = (answer, request)
        return "summary + note"
    monkeypatch.setattr(main, "_execute_turn", turn)
    monkeypatch.setattr(long_reply, "shape_for_discord", shape)
    assert run(main.run_pipeline_from_discord("compile it")) == "summary + note"
    assert seen["args"] == (_LONG, "compile it")

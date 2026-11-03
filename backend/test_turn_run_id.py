"""A live turn's tool receipts belong to that turn's run (found 4 October 2026).

main._execute_turn_once created the chat task BEFORE setting operations.current_run_id,
and an asyncio task copies its context when it is created: every receipt of a live turn
was written with no run id. On the server, 167 turns in 30 days showed no tool calls at
all, which broke "remember how you did that", the skills' overnight pass, and the
turn-to-tools trail A02/A09 rely on. Probes had hidden it by setting the id themselves.
"""
import asyncio

from backend import main
from backend.storage import db, operations
from backend.storage import runs as runs_store


def test_tool_receipts_carry_the_run_of_the_turn_that_made_them(monkeypatch):
    seen = {}

    async def fake_chat(text, history, send, **kw):
        seen["run_id"] = operations.current_run_id.get()
        operations.record_operation(tool="web_browse", outcome="ok", started_at=0.0,
                                    args={"task": "x"}, result="y")
        return "answer"

    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(main, "chat", fake_chat)
    monkeypatch.setattr(main, "broadcast", quiet)
    monkeypatch.setattr(main, "send_state", quiet)
    out = asyncio.run(main._execute_turn_once("research TTWO for me", channel="discord", want_tts=False))
    assert out == "answer"
    [run] = runs_store._ensure().execute("SELECT run_id, state FROM runs").fetchall()
    assert seen["run_id"] == run[0] and run[1] == "completed"
    rows = db.connect().execute("SELECT run_id, tool FROM operations").fetchall()
    assert [tuple(r) for r in rows] == [(run[0], "web_browse")]

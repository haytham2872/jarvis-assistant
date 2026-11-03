"""P4: the morning brief includes today's planned blocks (chronos.compose_brief)."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.brain import chronos, research

LOCAL_TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 6, 8, 0, tzinfo=LOCAL_TZ)
BLOCKS = "- 09:00-17:00 work\n- 17:30-18:30 gym"


@pytest.fixture
def quiet(monkeypatch):
    """Everything else the brief reads is empty; only the plan speaks."""
    monkeypatch.setattr(chronos, "_collect_research_trail", lambda now: "")
    monkeypatch.setattr(chronos, "_collect_open_intents", lambda: "")
    monkeypatch.setattr(chronos, "_collect_todays_reminders", lambda now: "")
    monkeypatch.setattr(chronos, "_collect_expensive_turns", lambda: "")

    async def no_calendar():
        return ""
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)


def compose(**kw):
    return asyncio.run(chronos.compose_brief(NOW, night_results=[]))


def test_the_prompt_carries_todays_blocks(quiet, monkeypatch):
    monkeypatch.setattr(chronos, "_collect_plan_today", lambda now: BLOCKS)
    seen = []

    async def llm(prompt, max_tokens=0):
        seen.append(prompt)
        return "Morning. Work all day, gym at half past five."
    monkeypatch.setattr(research, "_llm", llm)
    assert compose() == "Morning. Work all day, gym at half past five."
    (prompt,) = seen
    assert "Today's planned blocks" in prompt and BLOCKS in prompt


def test_a_day_with_only_a_plan_is_not_a_quiet_night(quiet, monkeypatch):
    monkeypatch.setattr(chronos, "_collect_plan_today", lambda now: BLOCKS)

    async def llm(prompt, max_tokens=0):
        return "ok"
    monkeypatch.setattr(research, "_llm", llm)
    assert "Quiet night" not in compose()


def test_without_a_plan_the_brief_is_as_before(quiet, monkeypatch):
    monkeypatch.setattr(chronos, "_collect_plan_today", lambda now: "")
    assert compose().startswith("Morning. Quiet night")


def test_the_plain_digest_lists_the_blocks_when_the_model_call_fails(quiet, monkeypatch):
    monkeypatch.setattr(chronos, "_collect_plan_today", lambda now: BLOCKS)

    async def llm(prompt, max_tokens=0):
        raise RuntimeError("model down")
    monkeypatch.setattr(research, "_llm", llm)
    text = compose()
    assert "Planned blocks today:\n" + BLOCKS in text


def test_a_failure_reading_the_plan_never_stops_the_brief(monkeypatch):
    from backend.brain import planday

    def boom(now=None):
        raise RuntimeError("no database")
    monkeypatch.setattr(planday, "todays_blocks_text", boom)
    assert chronos._collect_plan_today(NOW) == ""

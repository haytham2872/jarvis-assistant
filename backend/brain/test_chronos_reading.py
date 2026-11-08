"""Monthly morning-brief line naming the research topics that stopped unread."""

import asyncio
import datetime

import pytest

from backend.brain import chronos, research
from backend.brain.chronos import _TZ


def local(y, mo, d, h=8, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


def ts(y, mo, d):
    return local(y, mo, d, 12).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    box = {"stopped": []}
    monkeypatch.setattr(research, "_load_state", lambda: {"stopped_topics": box["stopped"]})
    return box


def topic(title, since, last_opened=None):
    return {"file": title + ".md", "title": title, "since": since, "last_opened": last_opened}


def test_empty_or_missing_is_blank(env, monkeypatch):
    assert chronos._collect_reading_line(local(2026, 11, 6)) == ""
    monkeypatch.setattr(research, "_load_state", lambda: {})
    assert chronos._collect_reading_line(local(2026, 11, 6)) == ""


def test_first_report_lists_all(env):
    env["stopped"] = [topic("Albert Camus", ts(2026, 8, 1), ts(2026, 9, 3)),
                      topic("Tesla stock", ts(2026, 10, 1))]
    assert chronos._collect_reading_line(local(2026, 11, 6)) == (
        "Research I stopped: Albert Camus (you haven't opened it since 3 Sep); "
        "Tesla stock (not opened since 1 Oct). Open the note or ask me and it comes back.")


def test_more_than_five_collapses(env):
    env["stopped"] = [topic(f"T{i}", ts(2026, 9, 1)) for i in range(7)]
    line = chronos._collect_reading_line(local(2026, 11, 6))
    assert "T4" in line and "T5" not in line and "and 2 more" in line


def test_same_month_is_blank(env):
    env["stopped"] = [topic("A", ts(2026, 9, 1))]
    chronos._update_state(lambda s: s.update(reading_report_date="2026-11-02"))
    assert chronos._collect_reading_line(local(2026, 11, 6)) == ""


def test_same_month_is_blank_even_for_a_topic_stopped_since(env):
    env["stopped"] = [topic("B", ts(2026, 10, 5))]                # stopped 4 Nov
    chronos._update_state(lambda s: s.update(reading_report_date="2026-11-02"))
    assert chronos._collect_reading_line(local(2026, 11, 6)) == ""


def test_the_line_reaches_the_composed_brief(env, monkeypatch):
    env["stopped"] = [topic("Albert Camus", ts(2026, 8, 1), ts(2026, 9, 3))]

    async def no_calendar():
        return ""

    async def prose(prompt, max_tokens=0):
        return "Morning, prose."
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)
    monkeypatch.setattr(chronos, "_collect_research_trail", lambda now: "")
    monkeypatch.setattr(chronos, "_collect_open_intents", lambda: "")
    monkeypatch.setattr(research, "_llm", prose)
    text = asyncio.run(chronos.compose_brief(local(2026, 11, 6), night_results=[]))
    assert "Research I stopped: Albert Camus" in text


def test_later_month_only_newly_stopped(env):
    env["stopped"] = [topic("Old", ts(2026, 8, 1)),      # stopped 31 Aug
                      topic("New", ts(2026, 10, 5))]     # stopped 4 Nov
    chronos._update_state(lambda s: s.update(reading_report_date="2026-10-02"))
    line = chronos._collect_reading_line(local(2026, 11, 6))
    assert "New" in line and "Old" not in line
    chronos._update_state(lambda s: s.update(reading_report_date="2026-11-05"))
    assert chronos._collect_reading_line(local(2026, 12, 6)) == ""


def test_research_state_error_is_blank(env, monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(research, "_load_state", boom)
    assert chronos._collect_reading_line(local(2026, 11, 6)) == ""


def _brief(monkeypatch, sent):
    async def compose(now, night_results=None):
        return "Morning.\n" + chronos._collect_reading_line(now)

    async def notifier(text):
        return sent

    async def nothing():
        return None
    monkeypatch.setattr(chronos, "compose_brief", compose)
    monkeypatch.setattr(chronos, "_notifier", notifier)
    monkeypatch.setattr(chronos, "_offer_skill_routines", nothing)


def test_delivered_brief_records_report_date(env, monkeypatch):
    env["stopped"] = [topic("A", ts(2026, 9, 1))]
    _brief(monkeypatch, True)
    asyncio.run(chronos._send_brief(local(2026, 11, 6)))
    assert chronos._load_state()["reading_report_date"] == "2026-11-06"


def test_failed_delivery_does_not_record(env, monkeypatch):
    env["stopped"] = [topic("A", ts(2026, 9, 1))]
    _brief(monkeypatch, False)
    asyncio.run(chronos._send_brief(local(2026, 11, 6)))
    assert "reading_report_date" not in chronos._load_state()


def test_brief_without_line_does_not_record(env, monkeypatch):
    _brief(monkeypatch, True)
    asyncio.run(chronos._send_brief(local(2026, 11, 6)))
    assert "reading_report_date" not in chronos._load_state()


def test_brief_now_does_not_record(env, monkeypatch):
    env["stopped"] = [topic("A", ts(2026, 9, 1))]
    _brief(monkeypatch, True)

    async def deliver(text):
        return True
    asyncio.run(chronos.brief_now(deliver=deliver))
    assert "reading_report_date" not in chronos._load_state()

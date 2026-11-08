"""The morning brief gives only actionable queue items, each at most once a week."""
import asyncio
from datetime import datetime
from backend import localtime

import pytest

from backend.brain import chronos

ZONE = localtime.ZONE
DAY0 = datetime(2026, 11, 1, 8, 0, tzinfo=ZONE)

# The shape of the real queue.md, shortened.
QUEUE = """\
# Queue

Active items:
- [open] 2026-10-14 — **Portrait correction still pending.** Waiting on a photo.
- [in_progress] 2026-10-20 — **Trading journal import.** Half done.
- [open] 2026-10-01 — Do not raise unprompted: the ex-flat deposit.
- [hold] 2026-10-02 — **Parked idea.** Not now.
- [resolved] 2026-10-03 — **Old thing.** Done.
[open] 2026-10-22 — **Bare line without a dash.**
- [open] 2026-10-23 — **Four.**
- [open] 2026-10-24 — **Five.**
- [open] 2026-10-25 — **Six.**
- [open] 2026-10-26 — **Seven.**

## Note to self
- 2026-10-05 sometimes a short answer is wanted.
- 2026-10-06 an open question about the garden.

## Closed (historical — do not propagate)
- [closed] 2026-09-01 — **Ancient.** Gone.
"""

FIRST_FIVE = ["Portrait correction", "Trading journal", "Do not raise", "Bare line", "Four."]


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "jarvis-vault").mkdir()
    (tmp_path / "jarvis-vault" / "queue.md").write_text(QUEUE, encoding="utf-8")
    monkeypatch.setattr(chronos, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "state.json")
    return tmp_path


def day(n):
    return DAY0.replace(day=DAY0.day + n)


def lines(now):
    return [line for _key, line in chronos._collect_open_intents(now)]


def test_only_open_and_in_progress_lines_count(env):
    got = lines(DAY0)
    assert len(got) == 5
    text = "\n".join(got)
    for bad in ("Parked", "Old thing", "Ancient", "Active items", "short answer", "garden", "Note to self", "# Queue"):
        assert bad not in text
    assert [any(w in l for l in got) for w in FIRST_FIVE] == [True] * 5


def test_at_most_five_items(env):
    assert len(chronos._collect_open_intents(DAY0)) == 5


def test_key_is_sha1_of_normalised_line_first_12_hex(env):
    import hashlib
    key, line = chronos._collect_open_intents(DAY0)[0]
    assert key == hashlib.sha1(" ".join(line.split()).encode("utf-8")).hexdigest()[:12]


def test_no_state_gives_every_actionable_item(env):
    assert chronos._load_state().get("brief_intents_seen") is None
    assert len(chronos._collect_open_intents(DAY0)) == 5


def test_delivered_item_is_not_repeated_for_seven_days(env):
    given = chronos._collect_open_intents(DAY0)
    chronos._mark_briefed(DAY0, [], reported_intents=[k for k, _ in given[:2]])
    for n in (1, 6):
        got = lines(day(n))
        assert not any("Portrait" in l or "Trading" in l for l in got)
        assert len(got) == 5          # the remaining items move up
    assert any("Portrait" in l for l in lines(day(7)))


def test_changed_text_counts_as_new(env):
    [(key, _line)] = chronos._collect_open_intents(DAY0)[:1]
    chronos._mark_briefed(DAY0, [], reported_intents=[key])
    q = env / "jarvis-vault" / "queue.md"
    q.write_text(QUEUE.replace("Waiting on a photo.", "Photo arrived."), encoding="utf-8")
    assert "Photo arrived" in lines(day(1))[0]
    q.write_text(QUEUE.replace("Waiting on a photo.", "Waiting  on a	photo."), encoding="utf-8")
    assert not any("Portrait" in l for l in lines(day(1)))     # whitespace alone is not a change


def test_failed_delivery_marks_nothing(env, monkeypatch):
    async def compose(now, night_results=None, intents=None):
        return "Morning."

    async def refused(text):
        return False
    monkeypatch.setattr(chronos, "compose_brief", compose)
    monkeypatch.setattr(chronos, "_notifier", refused)
    asyncio.run(chronos._send_brief(DAY0))
    assert not chronos._load_state().get("brief_intents_seen")
    assert len(chronos._collect_open_intents(day(1))) == 5

    async def deliver(text):
        return False
    asyncio.run(chronos.brief_now(deliver))
    assert not chronos._load_state().get("brief_intents_seen")


def test_delivered_brief_marks_the_items_it_gave(env, monkeypatch):
    async def compose(now, night_results=None, intents=None):
        assert len(intents) == 5
        return "Morning."

    async def ok(text):
        return True

    async def nothing():
        return None
    monkeypatch.setattr(chronos, "compose_brief", compose)
    monkeypatch.setattr(chronos, "_notifier", ok)
    monkeypatch.setattr(chronos, "_offer_skill_routines", nothing)
    asyncio.run(chronos._send_brief(DAY0))
    seen = chronos._load_state()["brief_intents_seen"]
    assert len(seen) == 5 and set(seen.values()) == {DAY0.strftime("%Y-%m-%d")}
    # the next brief moves on to the three items left
    assert len(lines(day(1))) == 3


def test_brief_now_delivered_marks_the_items_it_gave(env, monkeypatch):
    """Mutation check: /brief now not marking survived every other test."""
    async def compose(now, night_results=None, intents=None):
        return "Morning."

    async def deliver(text):
        return True
    monkeypatch.setattr(chronos, "compose_brief", compose)
    asyncio.run(chronos.brief_now(deliver))
    assert len(chronos._load_state().get("brief_intents_seen") or {}) == 5


def test_prompt_section_and_formatting(env, monkeypatch):
    from backend.brain import research
    seen = []

    async def llm(prompt, max_tokens=0):
        seen.append(prompt)
        return "Morning."
    for name in ("_collect_research_trail", "_collect_todays_reminders", "_collect_plan_today",
                 "_collect_skills_overnight", "_collect_goals_line", "_collect_reading_line"):
        monkeypatch.setattr(chronos, name, lambda *a: "")
    monkeypatch.setattr(chronos, "_collect_expensive_turns", lambda: "")
    monkeypatch.setattr("backend.brain.portfolio.brief_line", lambda: "")
    monkeypatch.setattr(research, "digest_line", lambda now: "", raising=False)

    async def no_calendar():
        return ""
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)
    monkeypatch.setattr(research, "_llm", llm)
    asyncio.run(chronos.compose_brief(DAY0, night_results=[]))
    assert "(each appears at most once a week" in seen[0]
    assert "Portrait correction" in seen[0] and "Parked" not in seen[0]


def test_seen_keeps_the_300_most_recent(env):
    keys = [f"{i:012d}" for i in range(310)]
    chronos._mark_briefed(DAY0, [], reported_intents=keys[:305])
    chronos._mark_briefed(day(1), [], reported_intents=keys[305:])
    seen = chronos._load_state()["brief_intents_seen"]
    assert len(seen) == 300
    assert all(k in seen for k in keys[305:])

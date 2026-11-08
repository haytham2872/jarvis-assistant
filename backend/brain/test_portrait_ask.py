"""The morning brief asks once whether a fading Situation line still holds."""
import asyncio
import datetime
import hashlib

import pytest

from backend.brain import chronos, consolidation, portrait as P
from backend.brain.chronos import _TZ

TODAY = "2026-11-07"
NOW = datetime.datetime(2026, 11, 7, 8, 0, tzinfo=_TZ)


def ago(n):
    return (datetime.date(2026, 11, 7) - datetime.timedelta(days=n)).isoformat()


def key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def portrait(*situation, other=()):
    lines = ["# Portrait", P.MARKER, "", "## Situation"]
    lines += [f"- {t} (as of {d})" for t, d in situation]
    lines += ["", "## Who they are"] + [f"- {t} (as of {d})" for t, d in other]
    return "\n".join(lines) + "\n"


def test_fading_window_edges():
    md = portrait(("a37", ago(37)), ("b38", ago(38)), ("c45", ago(45)), ("d46", ago(46)),
                  ("e40", ago(40)), other=[("who40", ago(40))])
    assert P.fading(md, TODAY) == ["b38", "c45", "e40"]


def test_fading_needs_marker_and_valid_date():
    md = portrait(("x", ago(40))).replace(P.MARKER, "")
    assert P.fading(md, TODAY) == []
    assert P.fading(portrait(("x", ago(40))), "garbage") == []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    box = {"md": portrait(("Job hunting in Lyon", ago(40)), ("Moving flat", ago(41)),
                          ("Third thing", ago(42)), ("Fresh", ago(2)))}
    monkeypatch.setattr(consolidation, "_current_portrait", lambda: box["md"])
    monkeypatch.setattr(chronos, "now_local", lambda: NOW)
    return box


def test_brief_line_up_to_two_and_not_repeated(env):
    line, keys = chronos._collect_portrait_line(NOW)
    assert line == ("Before these leave my picture of you, are they still true? · "
                    "Job hunting in Lyon · Moving flat (tell me either way)")
    assert keys == [key("Job hunting in Lyon"), key("Moving flat")]
    chronos._update_state(lambda s: s.update(portrait_asked={k: TODAY for k in keys}))
    line2, keys2 = chronos._collect_portrait_line(NOW)
    assert "Third thing" in line2 and "Job hunting" not in line2 and keys2 == [key("Third thing")]
    chronos._update_state(lambda s: s["portrait_asked"].update({key("Third thing"): TODAY}))
    assert chronos._collect_portrait_line(NOW) == ("", [])


def test_failure_gives_no_line(env, monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(consolidation, "_current_portrait", boom)
    assert chronos._collect_portrait_line(NOW) == ("", [])


def _brief(monkeypatch, sent, with_line=True):
    async def compose(now, night_results=None, intents=None):
        return "Morning.\n" + (chronos._collect_portrait_line(now)[0] if with_line else "")

    async def notifier(text):
        return sent

    async def nothing():
        return None
    monkeypatch.setattr(chronos, "compose_brief", compose)
    monkeypatch.setattr(chronos, "_notifier", notifier)
    monkeypatch.setattr(chronos, "_offer_skill_routines", nothing)


def test_send_brief_marks_only_when_delivered_and_present(env, monkeypatch):
    _brief(monkeypatch, False)
    asyncio.run(chronos._send_brief(NOW))
    assert "portrait_asked" not in chronos._load_state()
    _brief(monkeypatch, True, with_line=False)
    asyncio.run(chronos._send_brief(NOW))
    assert "portrait_asked" not in chronos._load_state()
    _brief(monkeypatch, True)
    asyncio.run(chronos._send_brief(NOW))
    assert chronos._load_state()["portrait_asked"] == {
        key("Job hunting in Lyon"): TODAY, key("Moving flat"): TODAY}


def test_brief_now_marks_only_when_delivered_and_present(env, monkeypatch):
    async def no(text):
        return False

    async def yes(text):
        return True
    _brief(monkeypatch, True)
    asyncio.run(chronos.brief_now(deliver=no))
    assert "portrait_asked" not in chronos._load_state()
    _brief(monkeypatch, True, with_line=False)
    asyncio.run(chronos.brief_now(deliver=yes))
    assert "portrait_asked" not in chronos._load_state()
    _brief(monkeypatch, True)
    asyncio.run(chronos.brief_now(deliver=yes))
    assert key("Moving flat") in chronos._load_state()["portrait_asked"]


def test_keeps_200_most_recent(env):
    old = {f"k{i:03d}": ago(100 - i % 50) for i in range(250)}
    chronos._update_state(lambda s: s.update(portrait_asked=old))
    chronos._mark_briefed(NOW, [], portrait_reported=["new"])
    asked = chronos._load_state()["portrait_asked"]
    assert len(asked) == 200 and asked["new"] == TODAY


def test_prompt_line_within_three_days_only(env):
    assert P.prompt_line() == ""
    chronos._update_state(lambda s: s.update(portrait_asked={key("Moving flat"): ago(2)}))
    assert "Moving flat" in P.prompt_line() and "Job hunting" not in P.prompt_line()
    chronos._update_state(lambda s: s.update(portrait_asked={key("Moving flat"): ago(4)}))
    assert P.prompt_line() == ""


def test_runtime_identity_carries_line(env):
    from backend.brain import runtime_status
    chronos._update_state(lambda s: s.update(portrait_asked={key("Moving flat"): TODAY}))
    assert "Moving flat" in runtime_status.runtime_identity_text()


def test_consolidation_prompt_carries_asked_lines_only_when_recent(env, monkeypatch):
    seen = {}

    async def fake_call(prompt):
        seen["p"] = prompt
        return "{}"
    monkeypatch.setattr(consolidation, "_call_compat", fake_call)
    monkeypatch.setattr(consolidation, "_facts_sample_sync", lambda: "")
    monkeypatch.setattr(consolidation, "_listening_log_tail", lambda: "")

    def gen():
        asyncio.run(consolidation._generate_consolidation(
            threads=[{"timestamp": 1000.0, "high": True, "user": "hi", "assistant": "hi"}],
            prior_letter="", owner_md=env["md"], queue_raw="", sleep=None))
    gen()
    assert "The brief asked" not in seen["p"]
    chronos._update_state(lambda s: s.update(portrait_asked={key("Moving flat"): ago(1)}))
    gen()
    assert "whether these still hold: Moving flat." in seen["p"]
    chronos._update_state(lambda s: s.update(portrait_asked={key("Moving flat"): ago(5)}))
    gen()
    assert "The brief asked" not in seen["p"]

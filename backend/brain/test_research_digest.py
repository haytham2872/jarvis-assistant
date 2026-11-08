"""Research findings are filed in the Inbox and summed up in the morning brief; the owner's thumbs steer."""
import asyncio
import json
import time

import pytest

from backend.brain import research

DAY = 86400.0


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    topics = tmp_path / "topics"
    topics.mkdir()
    monkeypatch.setattr(research, "_TOPICS_DIR", topics)
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "pace.json")
    monkeypatch.setattr(research, "_QUEUE_FILE", tmp_path / "research-queue.md")
    (topics / "mars.md").write_text("# Mars\n\nOld note.\n", encoding="utf-8")
    monkeypatch.setattr(research, "_pop_queued", lambda: {
        "subject": "Mars", "angle": "news", "reason": "check"})
    monkeypatch.setattr(research, "_log_research_pass", lambda *a, **k: None)
    filed, dms = [], []

    async def synth(_prompt, **_kw):
        return json.dumps({"progress": True, "changed_understanding": "new landing date",
                           "note": "# Mars\n\n" + "New facts. " * 40 + "https://example.org/mars",
                           "notify": True, "message": "Mars sample return slipped to 2031. More detail.",
                           "message_sources": ["https://example.org/mars"]})

    async def search(_q):
        return "Mars findings https://example.org/mars"

    async def write(filename, body, folder, **_kw):
        return "wrote"

    async def notify(message):
        dms.append(message)
        return True

    from backend.storage import notices
    real_record = notices.record

    def record(kind, text, key=None, **kw):
        filed.append((kind, text, key))
        return real_record(kind, text, key, **kw)

    from backend.tools import browser, vault
    monkeypatch.setattr(research, "_llm", synth)
    monkeypatch.setattr(research, "_notifier", notify)
    monkeypatch.setattr(browser, "web_browse", search)
    monkeypatch.setattr(vault, "vault_write", write)
    monkeypatch.setattr(vault, "note_revision", lambda *a: None)
    monkeypatch.setattr(notices, "record", record)
    return {"filed": filed, "dms": dms}


def test_finding_is_filed_not_dmed(env):
    run(research._tick())
    assert env["dms"] == []
    assert len(env["filed"]) == 1
    kind, text, key = env["filed"][0]
    assert kind == "research" and "Sources:" in text
    assert key.startswith("research:Mars@")
    digest = research._load_state()["research_digest"]
    assert [d["subject"] for d in digest] == ["Mars"]
    assert research._load_state().get("deferred_notifies", []) == []


def test_digest_keeps_last_30(env):
    research._save_state({"research_digest": [{"subject": f"s{i}", "message": "m", "ts": 1.0} for i in range(30)]})
    run(research._tick())
    digest = research._load_state()["research_digest"]
    assert len(digest) == 30 and digest[-1]["subject"] == "Mars" and digest[0]["subject"] == "s1"


def test_storage_failure_falls_back_to_dm(env, monkeypatch):
    from backend.storage import notices
    monkeypatch.setattr(notices, "record", lambda *a, **k: None)
    run(research._tick())
    assert len(env["dms"]) == 1 and "Sources:" in env["dms"][0]


def test_flush_deferred_still_sends_old_entries(env):
    research._save_state({"deferred_notifies": [
        {"ts": 1.0, "subject": "Old", "message": "Old thing. Sources: https://example.org/old"}]})
    run(research._flush_deferred(time.time()))
    assert len(env["dms"]) == 1


def test_digest_line(env):
    now = time.time()
    assert research.digest_line(now) == ""
    msgs = [{"subject": f"S{i}", "message": f"Finding {i} is here. Second sentence.\n\nSources: https://x.org",
             "ts": now - 3600 * (5 - i)} for i in range(6)]
    msgs.insert(0, {"subject": "Old", "message": "Stale.", "ts": now - 2 * DAY})
    research._save_state({"research_digest": msgs})
    line = research.digest_line(now)
    assert line.startswith("What I looked into: S5 — Finding 5 is here.")
    assert "Finding 2 is here." in line and "Finding 1" not in line and "Second sentence" not in line and "Sources" not in line
    assert "Old" not in line and "(and 2 more)" in line
    assert line.endswith("each is in your Inbox; 👍/👎 tells me what to keep looking into.")


def test_digest_line_truncates_long_sentence(env):
    now = time.time()
    research._save_state({"research_digest": [{"subject": "S", "message": "word " * 100, "ts": now}]})
    sentence = research.digest_line(now).split(" — ")[1]
    assert len(sentence) <= 160


# --- votes ---

def _hist(votes, passes=(), retired=(), now=None):
    h = research._History(list(passes), list(retired), [], votes=votes)
    return h


def _vote(subject, vote, days_ago, now):
    return {"notice_id": 1, "subject": subject, "vote": vote, "ts": now - days_ago * DAY}


def test_down_in_30_days_rests():
    now = time.time()
    assert _hist([_vote("Mars", "down", 5, now)]).gate("Mars", now) == ("rested", "marked not useful")


def test_old_down_does_not_rest():
    now = time.time()
    assert _hist([_vote("Mars", "down", 45, now)]).gate("Mars", now)[0] == "ok"


def test_two_downs_retire():
    now = time.time()
    h = _hist([_vote("Mars", "down", 50, now), _vote("Mars", "down", 5, now)])
    assert h.gate("Mars", now) == ("retire", "two 👎 votes")


def test_other_subject_unaffected():
    now = time.time()
    assert _hist([_vote("Fusion", "down", 1, now)]).gate("Mars", now)[0] == "ok"


def test_liked():
    now = time.time()
    assert _hist([_vote("Mars", "up", 3, now)]).liked("Mars", now)
    assert not _hist([_vote("Mars", "up", 20, now)]).liked("Mars", now)
    assert not _hist([_vote("Mars", "up", 5, now), _vote("Mars", "down", 2, now)]).liked("Mars", now)


def test_load_reads_votes_and_unavailable_is_harmless(env, monkeypatch):
    from backend.storage import research_feedback
    now = time.time()
    monkeypatch.setattr(research_feedback, "votes", lambda since: [_vote("Mars", "down", 1, now)])
    assert research._History.load(now).gate("Mars", now)[0] == "rested"

    def boom(since):
        raise RuntimeError("down")
    monkeypatch.setattr(research_feedback, "votes", boom)
    assert research._History.load(now).gate("Mars", now)[0] == "ok"


def test_liked_candidate_goes_first(env, monkeypatch):
    from backend.storage import research_feedback
    now = time.time()
    for name, title in (("fusion.md", "Fusion ignition"), ("mars.md", "Mars sample return")):
        (research._TOPICS_DIR / name).write_text(f"# {title}\n\n{'x' * 200}\n", encoding="utf-8")
    monkeypatch.setattr(research_feedback, "votes", lambda since: [_vote("Mars sample return", "up", 2, now)])

    async def kind(*a, **k):
        return "world"

    async def no_embed(texts):
        raise RuntimeError("no embedder")

    async def no_search(*a, **k):
        return []
    monkeypatch.setattr(research, "kind_of", kind)
    monkeypatch.setattr("backend.brain.embeddings.embed_many", no_embed)
    monkeypatch.setattr("backend.brain.semindex.search", no_search)
    order = [c["file"] for c in run(research._gather_candidates(now))]
    assert order[0] == "mars.md" and set(order) == {"fusion.md", "mars.md"}


# --- morning brief ---

def test_brief_tail_carries_the_digest_line(monkeypatch):
    from backend.brain import chronos
    from backend.brain.test_brief_plan import NOW

    for name in ("_collect_research_trail", "_collect_open_intents", "_collect_todays_reminders",
                 "_collect_expensive_turns", "_collect_plan_today", "_collect_skills_overnight",
                 "_collect_goals_line", "_collect_reading_line"):
        monkeypatch.setattr(chronos, name, lambda *a: "")
    monkeypatch.setattr("backend.brain.portfolio.brief_line", lambda: "")

    async def no_calendar():
        return ""

    async def llm(prompt, max_tokens=0):
        return "Morning."
    monkeypatch.setattr(chronos, "_collect_calendar", no_calendar)
    monkeypatch.setattr(research, "_llm", llm)
    monkeypatch.setattr(research, "digest_line", lambda now: "What I looked into: Mars — x.", raising=False)
    assert asyncio.run(chronos.compose_brief(NOW, night_results=[])) == "Morning.\nWhat I looked into: Mars — x."

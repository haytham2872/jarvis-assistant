"""Phone reading as a research signal: damp the staleness pull, stop after 30 unread days."""
import asyncio
import datetime

import pytest

from backend.brain import research

DAY = 86400.0
NOW = 1_800_000_000.0


def test_reading_curve():
    assert research.reading(None) == 1.0
    assert research.reading(0) == 1.0
    assert research.reading(15) == 0.5
    assert research.reading(30) == 0
    assert research.reading(60) == 0


def test_score_topic_read_factor():
    assert research.score_topic(0.4, 0.8) == 0.65 * 0.4 + 0.35 * 0.8
    assert research.score_topic(0.4, 0.8, 0.5) == pytest.approx(0.65 * 0.4 + 0.35 * 0.8 * 0.5)


def _member(file, days_unread, last_opened=None, since=None):
    return {"file": file, "title": file, "freshness": 0.0, "days_since": None, "last_angle": None,
            "digest": "", "days_unread": days_unread, "last_opened": last_opened, "since": since}


def test_aggregate_uses_smallest_days_unread():
    fam = research._aggregate_family([_member("a.md", 25, since=1.0), _member("b.md", 3, 7.0, 7.0),
                                      _member("c.md", None)])
    assert fam["days_unread"] == 3
    assert (fam["last_opened"], fam["since"]) == (7.0, 7.0)
    assert fam["score"] == pytest.approx(research.score_topic(0.0, 1.0, research.reading(3)))


def test_aggregate_none_when_no_member_has_it():
    fam = research._aggregate_family([_member("a.md", None), _member("b.md", None)])
    assert fam["days_unread"] is None and fam["since"] is None
    assert fam["score"] == research.score_topic(0.0, 1.0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    topics = tmp_path / "topics"
    topics.mkdir()
    monkeypatch.setattr(research, "_TOPICS_DIR", topics)
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "pace.json")
    from backend.brain import semindex, embeddings
    from backend.storage import note_opens

    hits = {}

    async def search(query, **k):
        for title, h in hits.items():
            if title in query:
                return h
        return []

    async def no_vecs(texts):
        raise RuntimeError("no embedder in tests")
    monkeypatch.setattr(semindex, "search", search)
    monkeypatch.setattr(embeddings, "embed_many", no_vecs)

    class E:
        pass
    e = E()
    e.topics, e.hits, e.opens, e.first = topics, hits, {}, [None]
    monkeypatch.setattr(note_opens, "last_opened", lambda folder: dict(e.opens))
    monkeypatch.setattr(note_opens, "first_opened", lambda: e.first[0])
    e.note = lambda name, body="# {t}\nbody\n", **kw: (topics / name).write_text(
        body.format(t=name[:-3]), encoding="utf-8")
    e.gather = lambda: asyncio.run(research._gather_candidates(NOW))
    return e


def _hot(sim=1.0):
    """An exchange hit from right now, so freshness == sim * 0.5 (significance 0)."""
    return [{"score": sim, "significance": 0.0, "timestamp": NOW}]


def test_no_opens_changes_nothing(env):
    env.note("alpha.md")
    env.note("zebra.md")
    cands = env.gather()
    assert {c["file"] for c in cands} == {"alpha.md", "zebra.md"}
    assert all(c["days_unread"] is None and c["score"] == research.score_topic(0.0, 1.0) for c in cands)
    assert research._load_state()["stopped_topics"] == []


def test_unread_31_days_excluded_unless_fresh(env):
    env.first[0] = NOW - 31 * DAY
    env.note("alpha.md")
    assert env.gather() == []
    # freshness 0.5 -> 0.65 * 0.5 = 0.325 >= _MIN_SCORE_TO_ATTEMPT
    env.hits["alpha"] = _hot()
    cands = env.gather()
    assert [c["file"] for c in cands] == ["alpha.md"]
    assert research._load_state()["stopped_topics"] == []


def test_opened_yesterday_outranks_equally_stale_unread_20_days(env):
    env.first[0] = NOW - 40 * DAY
    env.note("alpha.md")
    env.note("zebra.md")
    env.opens = {"alpha.md": NOW - 20 * DAY, "zebra.md": NOW - 1 * DAY}
    assert [c["file"] for c in env.gather()] == ["zebra.md", "alpha.md"]


def test_days_unread_counts_from_latest_of_open_tracking_and_created(env):
    env.first[0] = NOW - 10 * DAY
    env.note("alpha.md", "---\ndate: 2000-01-01\n---\n# alpha\nbody\n")        # old: tracking start wins
    created = datetime.datetime.fromtimestamp(NOW - 4 * DAY, datetime.timezone.utc).strftime("%Y-%m-%d")
    env.note("zebra.md", f"---\ncreated: {created}\n---\n# zebra\nbody\n")     # created wins over tracking
    env.note("moose.md")
    env.opens = {"moose.md": NOW - 2 * DAY}                                    # open wins
    by = {c["file"]: c for c in env.gather()}
    assert by["alpha.md"]["days_unread"] == pytest.approx(10)
    assert by["zebra.md"]["days_unread"] == pytest.approx(4, abs=1.0)
    assert by["moose.md"]["days_unread"] == pytest.approx(2)


def test_stopped_topics_recorded_and_overwritten(env):
    env.first[0] = NOW - 100 * DAY
    env.note("alpha.md")
    env.note("zebra.md")
    env.opens = {"zebra.md": NOW - 50 * DAY}
    assert env.gather() == []
    stopped = {s["file"]: s for s in research._load_state()["stopped_topics"]}
    assert set(stopped) == {"alpha.md", "zebra.md"}
    assert stopped["alpha.md"] == {"file": "alpha.md", "title": "alpha", "last_opened": None,
                                   "since": NOW - 100 * DAY}
    assert stopped["zebra.md"]["last_opened"] == NOW - 50 * DAY
    assert stopped["zebra.md"]["since"] == NOW - 50 * DAY
    env.opens = {"zebra.md": NOW - DAY, "alpha.md": NOW - DAY}   # read again: current set, not history
    assert len(env.gather()) == 2
    assert research._load_state()["stopped_topics"] == []


def test_opens_unavailable_means_no_signal(env, monkeypatch):
    from backend.storage import note_opens

    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(note_opens, "last_opened", boom)
    env.note("alpha.md")
    assert [c["days_unread"] for c in env.gather()] == [None]

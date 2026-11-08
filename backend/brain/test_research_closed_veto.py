"""The closed-topic veto: meaning must agree with a shared word, and a veto never wastes the tick.

Live incident (2026-09-25 morning brief): seven autonomous passes in one day on "V4.1 Flash
consolidation", every one skipped for matching the closed note meta-s-muse-model.md. That
note's only family token is "model" ("muse"/"meta" fall under the 5-letter token minimum),
so any subject containing "model" was vetoed as if it were the Muse note, and the veto ended
the tick, so the same conversation-fresh subject was proposed and vetoed again next tick.
"""
import asyncio
import json

import pytest

from backend.brain import research

MUSE = "meta-s-muse-model.md"
INCIDENT = "DeepSeek V4.1 Flash model consolidation"


@pytest.fixture
def topics(tmp_path, monkeypatch):
    d = tmp_path / "topics"
    d.mkdir()
    monkeypatch.setattr(research, "_TOPICS_DIR", d)
    return d


def note(topics, filename, title, closed=False):
    fm = "research_closed: true\n" if closed else ""
    (topics / filename).write_text(
        f"---\ntype: topic\n{fm}---\n\n# {title}\n\nBody.\n", encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ the rule
def test_the_incident_word_alone_vetoed(topics):
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    assert research._topic_tokens("Meta's Muse Model", MUSE) == {"model"}
    assert research._closed_family_conflict(INCIDENT, "x.md") == MUSE   # no similarity: old rule


def test_a_shared_word_with_unrelated_meaning_no_longer_vetoes(topics):
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    # 0.806 is the measured cosine of the incident subject against a Muse note
    assert research._closed_family_conflict(INCIDENT, "x.md", {MUSE: 0.806}) is None


def test_a_shared_word_with_matching_meaning_still_vetoes(topics):
    note(topics, "viktor-frankl.md", "Viktor Frankl", closed=True)
    hit = research._closed_family_conflict("Frankl's logotherapy", "frankl-s-logotherapy.md",
                                           {"viktor-frankl.md": 0.93})
    assert hit == "viktor-frankl.md"


def test_meaning_alone_catches_a_reworded_closed_subject(topics):
    """'Meta's Muse Spark benchmarks' shares no token with the Muse note (0.921 measured):
    token-only let it through and would have reopened a closed topic."""
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    subject = "Meta's Muse Spark benchmarks"
    assert research._closed_family_conflict(subject, "x.md") is None
    assert research._closed_family_conflict(subject, "x.md", {MUSE: 0.921}) == MUSE


@pytest.mark.parametrize("sim", [0.85, 0.89])
def test_meaning_below_family_strength_without_a_shared_word_does_not_veto(topics, sim):
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    assert research._closed_family_conflict("Kimi routing costs", "x.md", {MUSE: sim}) is None


def test_the_same_file_is_always_vetoed(topics):
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    assert research._closed_family_conflict("anything", MUSE, {MUSE: 0.1}) == MUSE


def test_thresholds_sit_between_the_measured_distributions():
    """Real subject->own note min 0.871; unrelated max 0.850 (n=1028)."""
    assert 0.850 < research._CLOSED_TOKEN_SIM_MIN < 0.871
    assert research._CLOSED_SIM_MIN >= 0.90


# ------------------------------------------------------------------ the similarity batch
def test_no_closed_notes_means_no_embedding(topics, monkeypatch):
    from backend.brain import embeddings
    async def boom(texts):
        raise AssertionError("must not embed when nothing is closed")
    monkeypatch.setattr(embeddings, "embed_many", boom)
    assert run(research._closed_similarity("x")) == {}


def test_similarity_is_one_batch_against_each_closed_note(topics, monkeypatch):
    from backend.brain import embeddings
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    note(topics, "viktor-frankl.md", "Viktor Frankl", closed=True)
    note(topics, "open.md", "Open Note")
    calls = []
    async def fake(texts):
        calls.append(texts)
        return [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    monkeypatch.setattr(embeddings, "embed_many", fake)
    sims = run(research._closed_similarity("subject"))
    assert len(calls) == 1 and len(calls[0]) == 3            # subject + the two closed notes
    assert sims == {MUSE: pytest.approx(1.0), "viktor-frankl.md": pytest.approx(0.0)}


def test_an_unavailable_embedder_degrades_to_the_word_rule(topics, monkeypatch):
    from backend.brain import embeddings
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    async def fail(texts):
        raise RuntimeError("no model")
    monkeypatch.setattr(embeddings, "embed_many", fail)
    assert run(research._closed_similarity("x")) is None


# ------------------------------------------------------------------ the tick
class Tick:
    def __init__(self, monkeypatch, topics, pick, sims, candidates):
        self.prompts, self.searches, self.passes = [], [], []
        monkeypatch.setattr(research, "_pop_queued", lambda: None)

        async def gather(now):
            return candidates
        async def recent(now):
            return "Sam: let's consolidate onto V4.1 Flash"
        async def llm(prompt, **kw):
            self.prompts.append(prompt)
            return json.dumps(pick)
        async def similarity(subject):
            return sims
        async def web(q):
            self.searches.append(q)
            raise RuntimeError("stop the tick here")   # nothing downstream is under test
        monkeypatch.setattr(research, "_gather_candidates", gather)
        monkeypatch.setattr(research, "_recent_exchange_lines", recent)
        monkeypatch.setattr(research, "_llm", llm)
        monkeypatch.setattr(research, "_closed_similarity", similarity)
        monkeypatch.setattr(research, "_log_research_pass",
                            lambda s, a, o, queued=False, file=None: self.passes.append((s, o)))
        from backend.tools import browser
        monkeypatch.setattr(browser, "web_browse", web)
        run(research._tick())


def candidate(file, title):
    return {"file": file, "title": title, "score": 0.5, "freshness": 0.2, "staleness": 0.3,
            "last_angle": None, "digest": "..."}


PICK = {"subject": INCIDENT, "existing_file": None, "angle": "news", "why": "fresh",
        "question": "What changed in consolidation costs and why?",
        "queries": ["deepseek v4.1 flash consolidation cost"]}


def test_the_incident_now_researches_the_subject(monkeypatch, topics):
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    t = Tick(monkeypatch, topics, PICK, {MUSE: 0.806}, [candidate("albert-camus.md", "Albert Camus")])
    assert t.searches == ["deepseek v4.1 flash consolidation cost"]
    assert not any("skipped" in o for _s, o in t.passes)


def test_a_real_veto_researches_the_next_open_candidate_instead(monkeypatch, topics):
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    pick = dict(PICK, subject="Meta Muse model benchmarks", queries=["muse benchmarks"])
    t = Tick(monkeypatch, topics, pick, {MUSE: 0.95}, [candidate("albert-camus.md", "Albert Camus")])
    assert t.searches and all("Albert Camus" in q for q in t.searches)
    assert not any("skipped" in o for _s, o in t.passes)


def test_a_veto_with_nothing_else_due_skips_once(monkeypatch, topics):
    # No note due, but recent conversation: the picker still runs (a fresh subject can come
    # from conversation) and the closed-topic veto still applies to what it proposes.
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    pick = dict(PICK, subject="Meta Muse model benchmarks")
    t = Tick(monkeypatch, topics, pick, {MUSE: 0.95}, [])
    assert t.searches == []
    assert [o for _s, o in t.passes] == [f"skipped: matches closed topic {MUSE}"]


def test_the_picker_is_told_what_is_closed(monkeypatch, topics):
    note(topics, MUSE, "Meta's Muse Model", closed=True)
    t = Tick(monkeypatch, topics, PICK, {MUSE: 0.806}, [candidate("albert-camus.md", "Albert Camus")])
    assert "closed to research" in t.prompts[0] and "Meta's Muse Model" in t.prompts[0]


def test_with_nothing_closed_the_picker_prompt_says_none(monkeypatch, topics):
    t = Tick(monkeypatch, topics, PICK, {}, [candidate("albert-camus.md", "Albert Camus")])
    assert "closed to research -- never propose these, or a new note about them:\n(none)" \
        in t.prompts[0]


@pytest.fixture(autouse=True)
def _every_note_is_world(monkeypatch):
    """These tests are about scoring, cooldowns and vetoes, not kinds (see test_research_v2.py)."""
    async def world(*a, **k):
        return "world"
    monkeypatch.setattr(research, "_known_kind", lambda *a, **k: "world")
    monkeypatch.setattr(research, "kind_of", world)

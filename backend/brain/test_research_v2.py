"""Research loop v2: kinds, subject history by meaning, daily budget.

No real model, embedder or vault: _llm, embeddings.embed_many, semindex.search
and the vault paths are all faked (see the `env` fixture). Shapes come from the
measurements on the server: variants of one subject score 0.89-0.99 cosine,
distinct subjects <= 0.84.
"""

import asyncio
import datetime
import hashlib
import json
import time

import pytest

from backend.brain import research

ZONE = research._localtime.ZONE
NOTE_BODY = "x" * 200

# keyword -> (cluster basis index, kind the fake classifier answers)
CLUSTERS = {
    "letter corpus": (0, "personal"),
    "internship": (1, "personal"),
    "model routing": (2, "self"),
    "mid-turn steering": (3, "self"),
    "mars sample return": (4, "world"),
    "solid state battery": (5, "world"),
    "fusion ignition": (6, "world"),
}
DIM = 80


def _vec(text: str) -> list[float]:
    low = text.lower()
    v = [0.0] * DIM
    idx = next((i for k, (i, _) in CLUSTERS.items() if k in low), None)
    if idx is None:
        idx = 8 + int(hashlib.md5(low.encode()).hexdigest(), 16) % 30
    v[idx] = 1.0
    # variants of one subject differ by a small, text-specific component:
    # same cluster -> cosine ~0.92, different cluster -> 0
    v[40 + int(hashlib.md5(low.encode()).hexdigest(), 16) % 30] = 0.3
    return v


class Env:
    def __init__(self):
        self.llm_calls: list[tuple[str, int]] = []
        self.kind_answers: dict[str, str] = {}   # keyword -> raw answer (overrides)
        self.pick_json = ""
        self.synth_json = json.dumps({"note": "n" * 300, "notify": False})
        self.queries: list[str] = []


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env()
    topics = tmp_path / "topics"
    topics.mkdir()
    monkeypatch.setattr(research, "_TOPICS_DIR", topics)
    monkeypatch.setattr(research, "_QUEUE_FILE", tmp_path / "research-queue.md")
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "pace.json")
    e.topics = topics

    async def fake_llm(prompt, max_tokens=3000):
        e.llm_calls.append((prompt, max_tokens))
        if "answer with one word" in prompt.lower():
            low = prompt.lower()
            for kw, ans in e.kind_answers.items():
                if kw in low:
                    return ans
            for kw, (_, kind) in CLUSTERS.items():
                if kw in low:
                    return kind
            return "world"
        if "scored existing topic notes" in prompt.lower():
            return e.pick_json
        return e.synth_json

    async def fake_embed(texts):
        return [_vec(t) for t in texts]

    async def fake_search(*a, **k):
        return []

    async def fake_recent(now):
        return ""

    async def fake_browse(q):
        e.queries.append(q)
        return "findings about " + q

    async def fake_vault_write(filename, content, folder, **kw):
        (topics / filename).write_text(content, encoding="utf-8")
        return "ok"

    monkeypatch.setattr(research, "_llm", fake_llm)
    monkeypatch.setattr("backend.brain.embeddings.embed_many", fake_embed)
    monkeypatch.setattr("backend.brain.semindex.search", fake_search)
    monkeypatch.setattr(research, "_recent_exchange_lines", fake_recent)
    monkeypatch.setattr("backend.tools.browser.web_browse", fake_browse)
    monkeypatch.setattr("backend.tools.vault.vault_write", fake_vault_write)
    return e


def write_note(env, name, title, body=NOTE_BODY, fm=""):
    front = f"---\n{fm}\n---\n" if fm else ""
    (env.topics / name).write_text(f"{front}# {title}\n\n{body}\n", encoding="utf-8")


def stamp(minutes_ago=0.0, days_ago=0.0):
    t = datetime.datetime.now(ZONE) - datetime.timedelta(minutes=minutes_ago, days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M")


def trail(env, *lines):
    path = research._QUEUE_FILE
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(old + "\n".join(lines) + "\n", encoding="utf-8")


def done(subject, outcome, days_ago=0.0, minutes_ago=0.0, origin="auto", angle="news"):
    return f"[done] {stamp(minutes_ago, days_ago)} | {subject} [{angle}] ({origin}) | {outcome}"


def run(coro):
    return asyncio.run(coro)


def state():
    return research._load_state()


# ---------------------------------------------------------------------------
# kind_of
# ---------------------------------------------------------------------------

def test_frontmatter_kind_wins_without_a_model_call(env):
    content = "---\nresearch_kind: world\n---\n# Letter Corpus Analysis\n\nbody"
    kind = run(research.kind_of("lca.md", "Letter Corpus Analysis", "body", content=content))
    assert kind == "world"
    assert env.llm_calls == []


def test_kind_is_classified_once_and_cached(env):
    k1 = run(research.kind_of("a.md", "Mars Sample Return", "head"))
    k2 = run(research.kind_of("a.md", "Mars Sample Return", "head"))
    assert k1 == k2 == "world"
    assert len(env.llm_calls) == 1
    assert env.llm_calls[0][1] == 300
    cached = state()["research_kinds"]["a.md"]
    assert cached["kind"] == "world"
    assert cached["hash"] == hashlib.sha256(b"Mars Sample Returnhead").hexdigest()


def test_kind_reclassified_when_note_changes(env):
    run(research.kind_of("a.md", "Mars Sample Return", "head"))
    run(research.kind_of("a.md", "Mars Sample Return", "head, edited"))
    assert len(env.llm_calls) == 2


def test_kind_bad_answer_is_none_and_not_cached(env):
    env.kind_answers["mars"] = "It is probably a world subject, I think."
    assert run(research.kind_of("a.md", "Mars Sample Return", "head")) is None
    assert "a.md" not in state().get("research_kinds", {})


def test_kind_failed_call_is_none(env, monkeypatch):
    async def boom(prompt, max_tokens=0):
        raise RuntimeError("down")
    monkeypatch.setattr(research, "_llm", boom)
    assert run(research.kind_of("a.md", "Mars Sample Return", "head")) is None
    assert "a.md" not in state().get("research_kinds", {})


def test_kind_prompt_carries_the_three_definitions(env):
    run(research.kind_of("a.md", "Mars Sample Return", "head"))
    prompt = env.llm_calls[0][0]
    for word in ("world", "personal", "self"):
        assert f"{word} =" in prompt


# ---------------------------------------------------------------------------
# _gather_candidates: kinds
# ---------------------------------------------------------------------------

def test_gather_keeps_only_world_families(env):
    write_note(env, "mars.md", "Mars Sample Return")
    write_note(env, "letters.md", "Letter Corpus Analysis")
    write_note(env, "routing.md", "Model Routing")
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["mars.md"]


def test_gather_skips_family_with_unclassifiable_member(env):
    write_note(env, "mars.md", "Mars Sample Return")
    env.kind_answers["fusion"] = "maybe"
    write_note(env, "fusion.md", "Fusion Ignition")
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["mars.md"]


def test_gather_honours_frontmatter_override(env):
    write_note(env, "letters.md", "Letter Corpus Analysis", fm="research_kind: world")
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["letters.md"]
    assert env.llm_calls == []


def test_gather_classifies_at_most_six_unknown_notes_per_tick(env):
    for i in range(8):
        write_note(env, f"topic{i}.md", f"Fusion Ignition part {i}x{i}")
    run(research._gather_candidates(time.time()))
    assert len(env.llm_calls) == 6
    run(research._gather_candidates(time.time()))
    assert len(env.llm_calls) == 8   # only the remaining two


# ---------------------------------------------------------------------------
# Subject history by meaning
# ---------------------------------------------------------------------------

def test_variants_share_history_and_hit_the_weekly_cap(env):
    # Three different filenames/titles of one subject: each passes alone, but
    # two passes in 7 days across the variants cap the whole subject.
    write_note(env, "lca.md", "Letter Corpus Analysis: per-phase", fm="research_kind: world")
    write_note(env, "mars.md", "Mars Sample Return")
    trail(env,
          done("Letter Corpus Analysis: Method", "wrote topics/a.md", days_ago=1),
          done("Letter Corpus Analysis: self-documenting", "wrote topics/b.md", days_ago=2))
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["mars.md"]


def test_distinct_subjects_do_not_share_history(env):
    write_note(env, "mars.md", "Mars Sample Return")
    write_note(env, "fusion.md", "Fusion Ignition")
    trail(env,
          done("Fusion Ignition", "wrote topics/a.md", days_ago=1),
          done("Fusion Ignition", "wrote topics/a.md", days_ago=2))
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["mars.md"]


def test_old_passes_outside_the_week_do_not_cap(env):
    write_note(env, "mars.md", "Mars Sample Return")
    trail(env,
          done("Mars Sample Return", "wrote topics/a.md", days_ago=8),
          done("Mars Sample Return", "wrote topics/a.md", days_ago=9))
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["mars.md"]


def test_rest_after_nothing_new_ladder(env):
    write_note(env, "mars.md", "Mars Sample Return")
    nothing = "no new understanding: sources repeat the note"
    # one nothing, 1 day ago: rests 2 days
    trail(env, done("Mars Sample Return", nothing, days_ago=1))
    assert run(research._gather_candidates(time.time())) == []
    # ... 3 days ago: rest over
    research._QUEUE_FILE.unlink()
    trail(env, done("Mars Sample Return", nothing, days_ago=3))
    assert len(run(research._gather_candidates(time.time()))) == 1
    # two in a row, last 3 days ago: rests 4 days
    research._QUEUE_FILE.unlink()
    trail(env, done("Mars Sample Return", nothing, days_ago=6),
          done("Mars Sample Return", nothing, days_ago=3))
    assert run(research._gather_candidates(time.time())) == []


def test_content_resets_the_rest(env):
    write_note(env, "mars.md", "Mars Sample Return")
    trail(env,
          done("Mars Sample Return", "no new understanding", days_ago=9),
          done("Mars Sample Return", "wrote topics/mars.md", days_ago=8),
          done("Mars Sample Return", "no new understanding", days_ago=3))
    # streak is 1 (the content pass reset it) -> 2-day rest, long over
    assert len(run(research._gather_candidates(time.time()))) == 1


def test_three_failures_in_a_row_retire_the_subject(env):
    write_note(env, "mars.md", "Mars Sample Return")
    write_note(env, "fusion.md", "Fusion Ignition")
    trail(env,
          done("Mars Sample Return", "failed: all searches failed", days_ago=20),
          done("Mars Sample Return [variant]", "failed: note has no traceable source", days_ago=15),
          done("Mars Sample Return", "failed: synthesis unusable", days_ago=10))
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["fusion.md"]
    retired = state()["research_retired"]
    assert len(retired) == 1
    assert retired[0]["subject"].startswith("Mars Sample Return")
    assert retired[0]["last_outcome"] == "failed: synthesis unusable"
    assert retired[0]["since"]
    # stays retired next tick even though the trail ages out
    research._QUEUE_FILE.unlink()
    cands = run(research._gather_candidates(time.time()))
    assert [c["file"] for c in cands] == ["fusion.md"]


def test_nothing_new_does_not_count_as_a_failure(env):
    write_note(env, "mars.md", "Mars Sample Return")
    trail(env,
          done("Mars Sample Return", "failed: all searches failed", days_ago=25),
          done("Mars Sample Return", "no new understanding", days_ago=20),
          done("Mars Sample Return", "failed: all searches failed", days_ago=15),
          done("Mars Sample Return", "failed: all searches failed", days_ago=12))
    assert len(run(research._gather_candidates(time.time()))) == 1   # rested long ago, not retired
    assert not state().get("research_retired")


def test_trail_stamps_are_written_in_local_time(env):
    research._log_research_pass("Mars Sample Return", "news", "wrote x")
    line = [l for l in research._QUEUE_FILE.read_text(encoding="utf-8").splitlines() if l.startswith("[done]")][0]
    stamp = line[len("[done] "):len("[done] ") + 16]
    assert stamp == datetime.datetime.now(ZONE).strftime("%Y-%m-%d %H:%M")


def test_two_failures_then_content_do_not_retire(env):
    write_note(env, "mars.md", "Mars Sample Return")
    trail(env,
          done("Mars Sample Return", "failed: all searches failed", days_ago=20),
          done("Mars Sample Return", "failed: all searches failed", days_ago=15),
          done("Mars Sample Return", "wrote topics/mars.md", days_ago=10))
    assert len(run(research._gather_candidates(time.time()))) == 1
    assert not state().get("research_retired")


def test_parse_done_lines_keeps_last_30_days_only(env):
    lines = [
        done("Old", "wrote x", days_ago=40),
        done("Fresh [with brackets]", "wrote y", days_ago=2, origin="queued", angle="deepen"),
        "[queued] 2026-07-05 | something | auto | why",
    ]
    out = research.parse_done_lines(lines, time.time())
    assert [p["subject"] for p in out] == ["Fresh [with brackets]"]
    assert out[0]["origin"] == "queued" and out[0]["outcome"] == "wrote y"


# ---------------------------------------------------------------------------
# research_queue: retired status / clearing / personal add
# ---------------------------------------------------------------------------

def _retire(subject="Mars Sample Return"):
    research._save_state({"research_retired": [
        {"subject": subject, "since": "2026-11-01", "last_outcome": "failed: x"}]})


def test_status_lists_retired(env):
    _retire()
    out = run(research.research_queue("status"))
    assert "Retired" in out and "Mars Sample Return" in out


def test_add_clears_retired_for_a_variant_subject(env):
    _retire("Mars Sample Return: method")
    out = run(research.research_queue("add", subject="Mars Sample Return: per-phase"))
    assert out.startswith("Queued")
    assert state().get("research_retired") == []


def test_add_keeps_retired_for_a_distinct_subject(env):
    _retire("Mars Sample Return")
    run(research.research_queue("add", subject="Fusion Ignition"))
    assert len(state()["research_retired"]) == 1


def test_reopen_clears_retired_even_without_a_note(env):
    _retire("Mars Sample Return")
    out = run(research.research_queue("reopen", subject="Mars Sample Return"))
    assert state().get("research_retired") == []
    assert "Mars Sample Return" in out


def test_cleared_retirement_forgets_the_old_failures(env):
    write_note(env, "mars.md", "Mars Sample Return")
    trail(env,
          done("Mars Sample Return", "failed: a", days_ago=20),
          done("Mars Sample Return", "failed: b", days_ago=15),
          done("Mars Sample Return", "failed: c", days_ago=10))
    assert run(research._gather_candidates(time.time())) == []
    run(research.research_queue("reopen", subject="Mars Sample Return"))
    assert len(run(research._gather_candidates(time.time()))) == 1


def test_add_on_a_personal_subject_still_works(env):
    out = run(research.research_queue("add", subject="My internship letters"))
    assert out.startswith("Queued")
    assert "[queued]" in research._QUEUE_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _tick: daily budget, no world candidate, queued passes, new subjects
# ---------------------------------------------------------------------------

def test_daily_budget_stops_autonomous_passes(env):
    write_note(env, "mars.md", "Mars Sample Return")
    research.set_pace("normal")
    trail(env,
          done("Fusion Ignition", "wrote topics/a.md", minutes_ago=0),
          done("Solid State Battery", "wrote topics/b.md", minutes_ago=0))
    run(research._tick())
    assert env.queries == []
    assert not any("scored existing topic notes" in p.lower() for p, _ in env.llm_calls)


def test_daily_budget_per_pace_and_queued_do_not_count(env):
    assert research._DAILY_PASSES == {"low": 1, "normal": 2, "high": 4}
    write_note(env, "mars.md", "Mars Sample Return")
    env.pick_json = json.dumps({"subject": "Mars Sample Return", "existing_file": "mars.md",
                                "angle": "news", "why": "w", "question": "What changed recently and why?", "queries": ["mars q"]})
    research.set_pace("normal")
    trail(env,
          done("A", "wrote x", origin="queued"),
          done("B", "wrote x", origin="queued"),
          done("C", "wrote x", origin="auto"))
    run(research._tick())
    assert env.queries == ["mars q"]


def test_no_world_candidate_skips_but_queued_request_runs(env):
    write_note(env, "letters.md", "Letter Corpus Analysis")
    run(research._tick())
    assert env.queries == []
    assert not any("scored existing topic notes" in p.lower() for p, _ in env.llm_calls)
    run(research.research_queue("add", subject="Letter Corpus Analysis"))
    run(research._tick())
    assert env.queries            # the deliberate request overrode the kind
    assert "(queued)" in research._QUEUE_FILE.read_text(encoding="utf-8")


def test_queued_request_ignores_cap_rest_and_retirement(env):
    _retire("Mars Sample Return")
    trail(env, done("Mars Sample Return", "no new understanding", days_ago=0.5))
    run(research.research_queue("add", subject="Mars Sample Return"))
    run(research._tick())
    assert env.queries


def test_new_personal_subject_is_rejected_and_falls_back(env):
    write_note(env, "mars.md", "Mars Sample Return")
    env.pick_json = json.dumps({"subject": "Internship application letters",
                                "existing_file": None, "angle": "news",
                                "why": "mentioned them", "question": "What changed recently and why?", "queries": ["internship q"]})
    run(research._tick())
    assert "internship q" not in env.queries
    assert env.queries and "Mars Sample Return" in env.queries[0]
    assert not (env.topics / "internship-application-letters.md").exists()


def test_new_world_subject_is_created(env):
    write_note(env, "mars.md", "Mars Sample Return")
    env.pick_json = json.dumps({"subject": "Solid State Battery", "existing_file": None,
                                "angle": "news", "why": "asked", "question": "What changed recently and why?", "queries": ["battery q"]})
    run(research._tick())
    assert env.queries == ["battery q"]
    assert "| Solid State Battery [news] (auto) |" in research._QUEUE_FILE.read_text(encoding="utf-8")


def test_new_subject_that_is_a_retired_variant_is_rejected(env):
    write_note(env, "mars.md", "Mars Sample Return")
    _retire("Fusion Ignition: method")
    env.pick_json = json.dumps({"subject": "Fusion Ignition: per-phase", "existing_file": None,
                                "angle": "news", "why": "w", "question": "What changed recently and why?", "queries": ["fusion q"]})
    run(research._tick())
    assert "fusion q" not in env.queries
    assert env.queries


# Review finding: with no note due, recent conversation can still give a
# fresh subject (as before v2), and it is held to the same kind rule.

def test_with_no_note_due_a_world_subject_from_conversation_still_runs(env, monkeypatch):
    async def recent(now):
        return "[1.0h ago] U: what's new with solid state batteries?"
    monkeypatch.setattr(research, "_recent_exchange_lines", recent)
    env.pick_json = json.dumps({"subject": "Solid State Battery", "existing_file": None,
                                "angle": "news", "why": "asked", "question": "What changed recently and why?",
                                "queries": ["battery q"]})
    run(research._tick())
    assert env.queries == ["battery q"]


def test_with_no_note_due_a_personal_subject_from_conversation_is_refused(env, monkeypatch):
    async def recent(now):
        return "[1.0h ago] U: my internship interview went well"
    monkeypatch.setattr(research, "_recent_exchange_lines", recent)
    env.kind_answers["internship"] = "personal"
    env.pick_json = json.dumps({"subject": "Sam's bank internship", "existing_file": None,
                                "angle": "news", "why": "mentioned it", "question": "What next?",
                                "queries": ["internship q2"]})
    run(research._tick())
    assert env.queries == []


def test_with_no_note_due_and_no_conversation_the_picker_is_not_called(env):
    env.pick_json = json.dumps({"subject": "Anything", "existing_file": None, "angle": "news",
                                "why": "w", "question": "q?", "queries": ["x"]})
    run(research._tick())
    assert not any("scored existing topic notes" in p.lower() for p, _ in env.llm_calls)

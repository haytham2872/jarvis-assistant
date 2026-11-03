"""The git-derived evolution log.

What is actually being pinned here is the set of properties the old writer lacked,
each of which was measured before this module existed:

* a commit cannot be missed regardless of whether a backend was running,
* an entry carries substance, not just a section name,
* an entry survives a failed model call with its facts intact,
* the file is written through `vault_write`, so it stays searchable,
* and a first run does not re-log days a human already documented by hand.
"""
import asyncio
import json

import pytest

from backend.brain import evolution as ev


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the module at a tmp project root + state file."""
    (tmp_path / "jarvis-vault" / "self").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ev, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ev, "_STATE_FILE", tmp_path / ".jarvis_evolution.json")
    return tmp_path


def _commit(sha="a" * 40, date="2026-09-22", subject="do a thing", body="", files=None):
    return {"sha": sha, "date": date, "subject": subject, "body": body,
            "files": files or ["backend/brain/engine.py"]}


# ---------------------------------------------------------------------------
# git parsing
# ---------------------------------------------------------------------------

def _fake_log(records):
    """Build raw `git log` output in the module's own record/field format."""
    out = []
    for sha, date, subject, body, files in records:
        out.append(
            ev._GIT_RECORD
            + ev._GIT_FIELD.join([sha, date, subject, body, "\n" + "\n".join(files)])
        )
    return "".join(out)


def test_commits_since_parses_the_record_format(monkeypatch):
    raw = _fake_log([
        ("s1" + "0" * 38, "2026-09-21", "first subject", "first body",
         ["backend/a.py", "CLAUDE.md"]),
        ("s2" + "0" * 38, "2026-09-22", "second subject", "", ["backend/b.py"]),
    ])
    monkeypatch.setattr(ev, "_git", lambda *a: raw)

    commits = ev.commits_since(None)

    assert [c["subject"] for c in commits] == ["first subject", "second subject"]
    assert commits[0]["date"] == "2026-09-21"
    assert commits[0]["body"] == "first body"
    assert commits[0]["files"] == ["backend/a.py", "CLAUDE.md"]
    assert commits[1]["files"] == ["backend/b.py"]


def test_commits_since_skips_bookkeeping_commits(monkeypatch):
    """Vault auto-backup commits are the cron's, not a change worth reflecting on;
    including them would make most entries say nothing."""
    raw = _fake_log([
        ("s1" + "0" * 38, "2026-09-22", "vault auto-backup 2026-09-22", "", ["jarvis-vault/x.md"]),
        ("s2" + "0" * 38, "2026-09-22", "Merge branch 'main'", "", ["x.py"]),
        ("s3" + "0" * 38, "2026-09-22", "real change", "", ["backend/a.py"]),
    ])
    monkeypatch.setattr(ev, "_git", lambda *a: raw)

    assert [c["subject"] for c in ev.commits_since(None)] == ["real change"]


def test_commits_since_is_empty_when_git_is_unavailable(monkeypatch):
    """Degradation: a machine without git, or a non-checkout, produces no entries
    rather than an exception on a background loop."""
    monkeypatch.setattr(ev, "_git", lambda *a: "")
    assert ev.commits_since(None) == []
    assert ev.commits_since("abc123") == []


def test_commits_since_asks_for_the_range_after_the_watermark(monkeypatch):
    seen = {}

    def fake_git(*args):
        seen["args"] = args
        return ""

    monkeypatch.setattr(ev, "_git", fake_git)

    ev.commits_since("deadbeef")
    assert "deadbeef..HEAD" in seen["args"]

    ev.commits_since(None)
    assert "HEAD" in seen["args"]
    assert not any(".." in a for a in seen["args"])


def test_git_reads_trust_only_the_fixed_checkout(monkeypatch, env):
    from types import SimpleNamespace

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        return SimpleNamespace(returncode=0, stdout="abc12345", stderr="")

    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    assert ev._git("rev-parse", "HEAD") == "abc12345"
    assert seen["argv"] == (
        "git", "-c", f"safe.directory={env.resolve().as_posix()}", "rev-parse", "HEAD"
    )
    assert seen["cwd"] == str(env)


def test_loop_catches_up_before_waiting_for_the_periodic_tick(monkeypatch):
    calls = []

    async def fake_cycle():
        calls.append("catchup")

    async def fake_periodic(*, name, check_interval_s, on_cycle):
        calls.append("periodic")
        assert name == "evolution"
        assert check_interval_s == ev._CHECK_INTERVAL_S
        assert on_cycle is fake_cycle

    monkeypatch.setattr(ev, "_cycle", fake_cycle)
    monkeypatch.setattr(ev.pacing, "run_periodic", fake_periodic)
    run(ev.run_loop())
    assert calls == ["catchup", "periodic"]


def test_strip_trailers_drops_attribution_not_content():
    body = (
        "Real explanation of the change.\n"
        "More explanation.\n"
        "\n"
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
        "Signed-off-by: someone\n"
    )
    out = ev._strip_trailers(body)
    assert "Real explanation of the change." in out
    assert "More explanation." in out
    assert "Co-Authored-By" not in out
    assert "Signed-off-by" not in out


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------

def test_group_by_day_is_one_entry_per_batch_in_the_normal_case():
    commits = [_commit(sha=f"{i}" * 40, subject=f"c{i}") for i in range(4)]
    days = ev.group_by_day(commits)
    assert list(days) == ["2026-09-22"]
    assert len(days["2026-09-22"]) == 4


def test_group_by_day_splits_a_multi_day_catchup_oldest_first():
    commits = [
        _commit(sha="1" * 40, date="2026-09-15"),
        _commit(sha="2" * 40, date="2026-09-21"),
        _commit(sha="3" * 40, date="2026-09-21"),
        _commit(sha="4" * 40, date="2026-09-22"),
    ]
    days = ev.group_by_day(commits)
    assert list(days) == ["2026-09-15", "2026-09-21", "2026-09-22"]
    assert len(days["2026-09-21"]) == 2


def test_summarise_files_dedupes_and_caps():
    commits = [
        _commit(files=["a.py", "b.py"]),
        _commit(files=["b.py", "c.py"]),
    ]
    assert ev._summarise_files(commits) == ["a.py", "b.py", "c.py"]

    many = [_commit(files=[f"f{i}.py" for i in range(12)])]
    out = ev._summarise_files(many, cap=8)
    assert len(out) == 9
    assert out[-1] == "…and 4 more"


# ---------------------------------------------------------------------------
# rendering — the degradation guarantee
# ---------------------------------------------------------------------------

def test_render_entry_puts_prose_above_facts():
    commits = [_commit(sha="abc12345" + "0" * 32, subject="fix the thing")]
    entry = ev.render_entry("2026-09-22", "1.27.0", commits, "I fixed the thing today.")

    lines = [l for l in entry.splitlines() if l.strip()]
    assert lines[0] == "## 2026-09-22 — v1.27.0"
    assert lines[1] == "I fixed the thing today."
    assert any("fix the thing" in l for l in lines[2:])


def test_an_entry_survives_a_failed_reflection_with_its_facts_intact():
    """The reason the facts block is never generated: a model failure must cost the
    prose, not the record."""
    commits = [_commit(sha="abc12345" + "0" * 32, subject="fix the thing",
                       files=["backend/brain/engine.py"])]
    entry = ev.render_entry("2026-09-22", "1.27.0", commits, "")

    assert "## 2026-09-22 — v1.27.0" in entry
    assert "abc12345" in entry
    assert "fix the thing" in entry
    assert "backend/brain/engine.py" in entry


def test_reflection_returns_empty_when_the_model_call_raises(monkeypatch):
    import backend.brain.research as research

    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(research, "_llm", boom)
    assert run(ev._reflect("2026-09-22", [_commit()])) == ""


def test_reflection_strips_markdown_headings_the_model_was_told_not_to_emit(monkeypatch):
    """A model that ignores 'no markdown' must not inject a heading into a file
    whose structure is parsed by heading."""
    import backend.brain.research as research

    async def fake(*a, **k):
        return "## 2026-09-22\nI changed the tool gateway today."

    monkeypatch.setattr(research, "_llm", fake)
    out = run(ev._reflect("2026-09-22", [_commit()]))
    assert out == "I changed the tool gateway today."
    assert not out.startswith("#")


# ---------------------------------------------------------------------------
# reflection shape — enforced in code, not requested in the prompt
# (both cases below were observed on the first real catch-up run)
# ---------------------------------------------------------------------------

def test_a_truncated_reflection_never_reaches_the_log():
    """Measured live: a 33-commit batch ran past the token cap and ended
    '...including a 196-line legacy self-model'. A half-sentence in the log is
    worse than no prose at all."""
    text = ("I reworked the tool gateway today. It now classifies outcomes per tool. "
            "The last change was a removal of a 196-line legacy self-model")
    out = ev._trim_reflection(text)
    assert out.endswith(".")
    assert "196-line legacy self-model" not in out
    assert "I reworked the tool gateway today." in out


def test_a_single_truncated_sentence_yields_no_reflection():
    """Nothing complete survived, so the entry falls back to facts only."""
    assert ev._trim_reflection("I started explaining something and then ran out of") == ""


def test_reflection_is_capped_at_the_sentence_limit():
    """The prompt asks for 3-5; the model wrote eight. The cap is what makes the
    log readable regardless of compliance."""
    text = " ".join(f"Sentence number {i} says a thing." for i in range(1, 11))
    out = ev._trim_reflection(text, max_sentences=6)
    assert out.count("Sentence number") == 6
    assert out.endswith("Sentence number 6 says a thing.")


def test_trimming_does_not_split_on_version_numbers_or_abbreviations():
    """The split fires only between a terminal mark and a capital, so 'v1.22.0'
    and 'e.g. something' stay whole."""
    text = ("I moved to v1.22.0 today, which bundles the gateway work. "
            "It affects a lot, e.g. the research loop and consolidation both.")
    out = ev._trim_reflection(text, max_sentences=2)
    assert "v1.22.0" in out
    assert "e.g. the research loop" in out
    assert out == text


def test_reflection_collapses_line_breaks_into_one_paragraph():
    """The log's structure is heading-delimited; stray blank lines inside an entry
    read as a break between entries.

    The mid-sentence break is the load-bearing case: a break at a sentence boundary
    is collapsed incidentally by the split-and-rejoin, so a test using only that
    shape passes even with the collapse removed (found by mutation).
    """
    out = ev._trim_reflection(
        "First thing happened.\n\nSecond thing\nhappened too, on a wrapped line.")
    assert "\n" not in out
    assert out == "First thing happened. Second thing happened too, on a wrapped line."


def test_a_regurgitated_prompt_is_cut_at_the_marker():
    """Observed live: the model finished its paragraph, then reprinted the prompt's
    own 'CHANGES (2026-09-21):' header and worked through the commit list. The echo
    was two well-formed sentences, so the sentence cap alone let it through."""
    text = ("I swapped both model tiers to DeepSeek V4.1 Flash today. "
            "It was backed by a measured head-to-head rather than a guess. "
            "CHANGES (2026-09-21): * Switch openrouter standard tier to V4.1 Flash. "
            "The old slug is still reachable via OPENROUTER_STANDARD_MODEL.")
    out = ev._trim_reflection(text)

    assert "CHANGES" not in out
    assert "OPENROUTER_STANDARD_MODEL" not in out
    assert out.endswith("rather than a guess.")


def test_a_bullet_list_ends_the_reflection():
    """'No bullet points' is a request; this is what enforces it."""
    text = ("I reworked the tool gateway today.\n"
            "* typed outcomes\n"
            "* pre-dispatch validation\n")
    assert ev._trim_reflection(text) == "I reworked the tool gateway today."


def test_prose_mentioning_changes_is_not_mistaken_for_an_echo():
    """The marker is the dated prompt header, not the word -- ordinary prose about
    changes must survive."""
    text = ("The changes today were mostly mechanical. "
            "Nothing about them surprised me.")
    assert ev._trim_reflection(text) == text


def test_the_prompt_caps_how_many_commits_the_model_sees():
    """Past the cap the model stops reflecting and starts enumerating -- which the
    facts block already does better, and completely."""
    commits = [_commit(sha=str(i) * 40, subject=f"change {i}") for i in range(30)]
    text = ev._format_for_prompt(commits)

    assert "change 0" in text
    assert "change 29" not in text
    assert "and 15 further commit(s)" in text


def test_the_facts_block_is_never_capped_even_when_the_prompt_is():
    """The prose may generalise over a large batch; the record must not."""
    commits = [_commit(sha=str(i) * 40, subject=f"change {i}") for i in range(30)]
    facts = ev._facts_block("2026-09-22", "1.27.0", commits)
    for i in range(30):
        assert f"change {i}" in facts


def test_reflect_applies_the_trim_not_just_the_heading_strip(monkeypatch):
    """The wiring, not the unit. `_trim_reflection` being correct is worthless if
    `_reflect` returns the raw text -- and every other test here exercises the two
    separately, so that mutation survived until this case existed."""
    import backend.brain.research as research

    async def overlong(*a, **k):
        return (" ".join(f"Sentence number {i} says a thing." for i in range(1, 12))
                + " And then it was cut off mid")

    monkeypatch.setattr(research, "_llm", overlong)
    out = run(ev._reflect("2026-09-22", [_commit()]))

    assert out.count("Sentence number") == ev._MAX_REFLECTION_SENTENCES
    assert "cut off mid" not in out
    assert out.endswith(".")


def test_the_prompt_carries_real_commit_substance():
    """The old entries said '- router: updated'. The point of this one is that the
    model sees the actual message and files."""
    commits = [_commit(subject="A04 slice 5: replans replace old instructions",
                       body="The plan shown to the model could lag the actual plan.",
                       files=["backend/brain/engine.py"])]
    text = ev._format_for_prompt(commits)
    assert "A04 slice 5: replans replace old instructions" in text
    assert "could lag the actual plan" in text
    assert "backend/brain/engine.py" in text


# ---------------------------------------------------------------------------
# watermark seeding — the "do not re-log what a human wrote" rule
# ---------------------------------------------------------------------------

def test_last_logged_date_takes_the_newest_and_ignores_prose_headings(env):
    (env / "jarvis-vault" / "self" / "evolution.md").write_text(
        "# Evolution Log\n\n"
        "## 2026-05-01 — v1.0.0\nold\n\n"
        "## Some prose heading\nnot a date\n\n"
        "## 2026-09-09 — v1.20.0\nnewer\n",
        encoding="utf-8")
    assert ev.last_logged_date() == "2026-09-09"


def test_last_logged_date_is_empty_when_the_file_is_missing(env):
    assert ev.last_logged_date() == ""


def test_seed_watermark_starts_after_the_newest_hand_written_entry(env, monkeypatch):
    """The real hazard this exists for: git history here starts after the earliest
    hand-written entries, so seeding from nothing would re-log days already covered."""
    (env / "jarvis-vault" / "self" / "evolution.md").write_text(
        "## 2026-09-09 — v1.20.0\nhand written\n", encoding="utf-8")

    asked = {}

    def fake_git(*args):
        asked["args"] = args
        return "cafebabe" + "0" * 32 + "\n"

    monkeypatch.setattr(ev, "_git", fake_git)

    sha = ev.seed_watermark()
    assert sha.startswith("cafebabe")
    assert any("--until=2026-09-09" in a for a in asked["args"])


def test_seed_watermark_is_none_on_an_empty_log(env, monkeypatch):
    """Nothing documented yet means everything git knows is fair game."""
    monkeypatch.setattr(ev, "_git", lambda *a: "")
    assert ev.seed_watermark() is None


# ---------------------------------------------------------------------------
# catch-up
# ---------------------------------------------------------------------------

class _Catchup:
    """Drives run_catchup with fake git + fake model, recording what was appended."""

    def __init__(self, monkeypatch, commits, append_ok=True):
        self.appended: list[str] = []
        self.append_ok = append_ok
        monkeypatch.setattr(ev, "commits_since", lambda last: list(commits))
        monkeypatch.setattr(ev, "seed_watermark", lambda: None)
        monkeypatch.setattr(ev, "_version_at", lambda sha: "1.27.0")

        async def fake_reflect(day, day_commits):
            return f"reflection for {day}"

        async def fake_append(entry):
            if not self.append_ok:
                return False
            self.appended.append(entry)
            return True

        monkeypatch.setattr(ev, "_reflect", fake_reflect)
        monkeypatch.setattr(ev, "_append", fake_append)


def test_catchup_writes_one_entry_per_day_and_advances_the_watermark(env, monkeypatch):
    commits = [
        _commit(sha="1" * 40, date="2026-09-21", subject="day one"),
        _commit(sha="2" * 40, date="2026-09-22", subject="day two a"),
        _commit(sha="3" * 40, date="2026-09-22", subject="day two b"),
    ]
    h = _Catchup(monkeypatch, commits)

    assert run(ev.run_catchup()) == 2
    assert len(h.appended) == 2
    assert "## 2026-09-21" in h.appended[0]
    assert "## 2026-09-22" in h.appended[1]

    state = json.loads((env / ".jarvis_evolution.json").read_text(encoding="utf-8"))
    assert state["last_logged_commit"] == "3" * 40
    assert state["last_logged_date"] == "2026-09-22"
    assert state["entries_written"] == 2
    assert state["commits_logged"] == 3


def test_catchup_is_idempotent_once_the_watermark_has_moved(env, monkeypatch):
    """The second tick five minutes later must be a no-op, not a duplicate entry."""
    commits = [_commit(sha="1" * 40, date="2026-09-22")]
    h = _Catchup(monkeypatch, commits)
    assert run(ev.run_catchup()) == 1

    # git now reports nothing new past the watermark
    monkeypatch.setattr(ev, "commits_since", lambda last: [])
    assert run(ev.run_catchup()) == 0
    assert len(h.appended) == 1


def test_a_failed_append_leaves_the_watermark_so_the_batch_retries(env, monkeypatch):
    """Losing an entry permanently is the failure mode this whole module exists to
    fix, so a write failure must not be recorded as progress."""
    commits = [_commit(sha="1" * 40, date="2026-09-22")]
    _Catchup(monkeypatch, commits, append_ok=False)

    assert run(ev.run_catchup()) == 0
    assert not (env / ".jarvis_evolution.json").exists() or \
        not json.loads((env / ".jarvis_evolution.json").read_text(
            encoding="utf-8")).get("last_logged_commit")


def test_a_failed_append_midway_keeps_the_days_already_written(env, monkeypatch):
    commits = [
        _commit(sha="1" * 40, date="2026-09-21"),
        _commit(sha="2" * 40, date="2026-09-22"),
    ]
    h = _Catchup(monkeypatch, commits)

    calls = {"n": 0}
    real_append = ev._append

    async def fail_on_second(entry):
        calls["n"] += 1
        if calls["n"] == 2:
            return False
        h.appended.append(entry)
        return True

    monkeypatch.setattr(ev, "_append", fail_on_second)

    assert run(ev.run_catchup()) == 1
    state = json.loads((env / ".jarvis_evolution.json").read_text(encoding="utf-8"))
    assert state["last_logged_commit"] == "1" * 40, (
        "the watermark advanced past a batch that was never written")


def test_catchup_caps_entries_per_run(env, monkeypatch):
    """A first run against a long history should produce a readable log, not a wall;
    the remainder lands on the next tick."""
    monkeypatch.setattr(ev, "_MAX_ENTRIES_PER_RUN", 3)
    commits = [_commit(sha=str(i) * 40, date=f"2026-09-{i:02d}") for i in range(10, 20)]
    h = _Catchup(monkeypatch, commits)

    assert run(ev.run_catchup()) == 3
    assert len(h.appended) == 3
    state = json.loads((env / ".jarvis_evolution.json").read_text(encoding="utf-8"))
    assert state["last_logged_date"] == "2026-09-12"


def test_catchup_with_no_commits_writes_nothing_and_calls_no_model(env, monkeypatch):
    """The common case, every five minutes: one empty `git log` and nothing else."""
    monkeypatch.setattr(ev, "commits_since", lambda last: [])
    monkeypatch.setattr(ev, "seed_watermark", lambda: None)

    async def must_not_run(*a, **k):
        raise AssertionError("a model call was made with no commits to reflect on")

    monkeypatch.setattr(ev, "_reflect", must_not_run)
    assert run(ev.run_catchup()) == 0


def test_catchup_never_raises_into_the_loop(env, monkeypatch):
    """Same silent-degradation contract as every other paced loop."""
    def boom(last):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(ev, "commits_since", boom)
    monkeypatch.setattr(ev, "seed_watermark", lambda: None)
    with pytest.raises(RuntimeError):
        run(ev.run_catchup())   # run_catchup itself propagates...

    # ...and pacing.run_periodic is what absorbs it, which is already tested in
    # test_pacing.py. What matters here is that _cycle delegates to it.
    import inspect
    assert "run_catchup" in inspect.getsource(ev._cycle)


# ---------------------------------------------------------------------------
# the indexing fix
# ---------------------------------------------------------------------------

def test_append_goes_through_vault_write_so_the_file_is_reindexed(env, monkeypatch):
    """The measured defect: the old writer used raw pathlib, so `jarvis_semantic`
    held only the file's oldest ~1KB and 'when did you get X' searched May."""
    import backend.tools.vault as vault

    captured = {}

    async def fake_vault_write(filename, content, folder):
        captured["args"] = (filename, content, folder)
        return f"Confirmed written to jarvis-vault/{folder}/{filename} (ok)"

    monkeypatch.setattr(vault, "vault_write", fake_vault_write)

    ok = run(ev._append("\n## 2026-09-22 — v1.27.0\n- `abc` thing\n"))

    assert ok is True
    filename, content, folder = captured["args"]
    assert (filename, folder) == ("evolution.md", "self")
    assert "## 2026-09-22" in content


def test_append_preserves_existing_content(env, monkeypatch):
    import backend.tools.vault as vault

    path = env / "jarvis-vault" / "self" / "evolution.md"
    path.write_text("# Evolution Log\n\n## 2026-09-09\nearlier entry\n", encoding="utf-8")

    captured = {}

    async def fake_vault_write(filename, content, folder):
        captured["content"] = content
        return "Confirmed written to jarvis-vault/self/evolution.md (ok)"

    monkeypatch.setattr(vault, "vault_write", fake_vault_write)
    run(ev._append("\n## 2026-09-22\nnew entry\n"))

    assert "earlier entry" in captured["content"]
    assert "new entry" in captured["content"]
    assert captured["content"].index("earlier entry") < captured["content"].index("new entry")


def test_append_reports_failure_when_the_write_does_not_verify(env, monkeypatch):
    """`vault_write` reads back before confirming; an unverified write must not
    advance the watermark, so it has to be detected rather than assumed."""
    import backend.tools.vault as vault

    async def failing_write(filename, content, folder):
        return "Error: could not write to vault"

    monkeypatch.setattr(vault, "vault_write", failing_write)
    assert run(ev._append("\n## 2026-09-22\nx\n")) is False


# ---------------------------------------------------------------------------
# versioning
# ---------------------------------------------------------------------------

def test_version_at_reads_the_version_as_it_stood_then(monkeypatch):
    """Catch-up writes entries for days that may be weeks old; stamping today's
    version on them would date every past change to the present release."""
    monkeypatch.setattr(ev, "_git", lambda *a: 'VERSION = "1.21.0"\n')
    assert ev._version_at("abc123") == "1.21.0"


def test_version_at_falls_back_to_the_current_version(monkeypatch):
    from backend.brain.version import VERSION

    monkeypatch.setattr(ev, "_git", lambda *a: "")
    assert ev._version_at("abc123") == VERSION


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def test_run_loop_is_wired_through_pacing(monkeypatch):
    captured = {}

    async def fake_cycle():
        return None

    async def fake_run_periodic(*, name, check_interval_s, on_cycle):
        captured.update(name=name, interval=check_interval_s, on_cycle=on_cycle)

    monkeypatch.setattr(ev, "_cycle", fake_cycle)
    monkeypatch.setattr(ev.pacing, "run_periodic", fake_run_periodic)
    run(ev.run_loop())

    assert captured["name"] == "evolution"
    assert captured["interval"] == ev._CHECK_INTERVAL_S
    assert captured["on_cycle"] is fake_cycle


def test_main_starts_the_evolution_loop():
    """A loop nobody starts is the old writer's failure mode in a new shape."""
    import inspect
    from backend import main

    src = inspect.getsource(main)
    assert "evolution.run_loop" in src


# ---------------------------------------------------------------------------
# the file, not the watermark, decides what has been logged
# ---------------------------------------------------------------------------

def _logged_file(env, shas):
    body = "# Evolution Log\n\n## 2026-09-22 — v1.27.0\nprose\n" + "".join(
        f"- `{s[:8]}` subject {s[:4]}\n" for s in shas)
    (env / "jarvis-vault" / "self" / "evolution.md").write_text(body, encoding="utf-8")


def test_a_branch_round_trip_does_not_relog_what_the_file_already_has(env, monkeypatch):
    """`git log <watermark>..HEAD` after switching between two diverged branches lists
    every commit of the branch you came back to -- measured here: 40, all logged."""
    commits = [_commit(sha=f"{i:x}" * 40, date="2026-09-22") for i in range(1, 6)]
    _logged_file(env, [c["sha"] for c in commits])
    h = _Catchup(monkeypatch, commits)
    monkeypatch.setattr(ev, "_commit_exists", lambda sha: True)

    assert run(ev.run_catchup()) == 0
    assert h.appended == []
    state = json.loads((env / ".jarvis_evolution.json").read_text(encoding="utf-8"))
    assert state["last_logged_commit"] == commits[-1]["sha"], (
        "the watermark was not moved past the already-logged range")


def test_only_the_unlogged_commits_of_a_mixed_range_are_written(env, monkeypatch):
    old = _commit(sha="a" * 40, date="2026-09-21", subject="already logged")
    new = _commit(sha="b" * 40, date="2026-09-22", subject="genuinely new")
    _logged_file(env, [old["sha"]])
    h = _Catchup(monkeypatch, [old, new])

    assert run(ev.run_catchup()) == 1
    assert "genuinely new" in h.appended[0]
    assert "already logged" not in h.appended[0]


def test_a_watermark_missing_from_the_repo_reseeds_instead_of_going_silent(env, monkeypatch):
    """A force-push or a copied state file leaves a sha git cannot resolve; `git log`
    then fails and `_git` returns '' -- the log would stop forever without this."""
    (env / ".jarvis_evolution.json").write_text(
        json.dumps({"last_logged_commit": "f" * 40}), encoding="utf-8")
    seen = {}
    commits = [_commit(sha="c" * 40, date="2026-09-22")]
    h = _Catchup(monkeypatch, commits)
    monkeypatch.setattr(ev, "_commit_exists", lambda sha: False)
    monkeypatch.setattr(ev, "seed_watermark", lambda: "seeded")
    monkeypatch.setattr(ev, "commits_since",
                        lambda last: (seen.setdefault("last", last), commits)[1])

    assert run(ev.run_catchup()) == 1
    assert seen["last"] == "seeded"


def test_logged_shas_reads_the_facts_blocks_only(env):
    (env / "jarvis-vault" / "self" / "evolution.md").write_text(
        "## 2026-09-22 — v1\nprose mentioning `12345678` inline is not a record\n"
        "- `abcdef12` a real record\n", encoding="utf-8")
    assert ev.logged_shas() == {"abcdef12"}

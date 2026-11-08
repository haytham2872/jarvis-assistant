"""The owner's portrait changes by typed, dated edits (backend/brain/portrait.py)."""
import asyncio

import pytest

from backend.brain import consolidation as C
from backend.brain import portrait as P
from backend.storage import consolidation_state as cstate, db

TODAY = "2026-11-07"
_TS = __import__("datetime").datetime(2026, 11, 7, 12).timestamp()

V2 = """# Portrait
<!-- portrait v2 -->

## Situation
- Job hunting in Lyon (as of 2026-11-01)
- The stitch line is known-stale (as of 2026-08-01)

## Who they are
- Prefers short answers (as of 2026-10-01)

## How to be with them
- Direct, no cheerleading (as of 2026-10-01)

## Notes
free prose stays
"""


def run(coro):
    return asyncio.run(coro)


def test_round_trip_is_byte_stable():
    pre, secs = P.parse(V2)
    assert P.render(pre, secs) == V2
    odd = V2 + "\n\n"
    assert P.render(*P.parse(odd)) == odd
    assert P.is_v2(V2) and not P.is_v2(V2.replace(P.MARKER, ""))


def test_each_op():
    ops = [
        {"section": "Situation", "action": "add", "text": "Moving flat in December"},
        {"section": "Who they are", "action": "revise", "match": "short answers", "text": "Likes short answers"},
        {"section": "Situation", "action": "remove", "match": "stitch"},
        {"section": "How to be with them", "action": "confirm", "match": "no cheerleading"},
    ]
    out, report = P.apply_patch(V2, ops, TODAY)
    assert report == []
    assert f"- Moving flat in December (as of {TODAY})" in out
    assert f"- Likes short answers (as of {TODAY})" in out
    assert "short answers (as of 2026-10-01)" not in out
    assert "stitch" not in out
    assert f"- Direct, no cheerleading (as of {TODAY})" in out
    assert "free prose stays" in out and "## Notes" in out
    assert P.render(*P.parse(out)) == out


def test_ambiguous_and_missing_match_skipped_and_reported():
    ops = [
        {"section": "Situation", "action": "remove", "match": "in"},        # both lines
        {"section": "Situation", "action": "confirm", "match": "nothing like it"},
        {"section": "Nowhere", "action": "add", "text": "x"},
    ]
    out, report = P.apply_patch(V2, ops, TODAY)
    assert out == V2
    assert len(report) == 3


def test_duplicate_add_becomes_confirm():
    out, report = P.apply_patch(
        V2, [{"section": "Situation", "action": "add", "text": "Job hunting in Lyon right now"}], TODAY)
    assert out.count("Lyon") == 1
    assert f"- Job hunting in Lyon (as of {TODAY})" in out
    assert "confirmed it instead" in report[0]


@pytest.mark.parametrize("ops", ["remove everything", {"a": 1}, ["add a line"], [{"section": "Situation", "action": "add", "text": "ok"}, 3]])
def test_malformed_op_list_leaves_file_unchanged(ops):
    out, report = P.apply_patch(V2, ops, TODAY)
    assert out == V2 and report


def test_no_patch_without_marker():
    v1 = V2.replace(P.MARKER + "\n", "")
    out, _ = P.apply_patch(v1, [{"section": "Situation", "action": "add", "text": "x y z"}], TODAY)
    assert out == v1


def test_aging_moves_only_stale_situation_lines():
    md = V2.replace("(as of 2026-10-01)", "(as of 2026-01-01)")
    out, aged = P.age_out(md, TODAY)
    assert aged == ["- The stitch line is known-stale (as of 2026-08-01)"]
    assert "stitch" not in out and "Job hunting" in out
    assert "Prefers short answers (as of 2026-01-01)" in out      # other sections never age
    hist = P.append_history("", aged, TODAY)
    assert hist.startswith("# Portrait history") and f"## {TODAY}" in hist and aged[0] in hist
    again = P.append_history(hist, aged, TODAY)
    assert again == hist
    later = P.append_history(hist, ["- other (as of 2026-08-02)"], "2026-11-09")
    assert later.startswith(hist) and "## 2026-11-09" in later


def test_no_aging_without_marker():
    v1 = V2.replace(P.MARKER + "\n", "")
    assert P.age_out(v1, TODAY) == (v1, [])


# ---------------------------------------------------------------------------
# Through consolidation
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "test.db"))
    db._reset_for_tests()
    vault = tmp_path / "vault"
    for d in ("letters", "people", "topics", "self", "creative", "conversations/significant"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(C, "_VAULT", vault)

    state = {"proposal": {}, "prompts": []}

    async def fake_generate(threads, prior, owner_md, *a, **k):
        # the real prompt builder, with the model call replaced
        async def fake_call(prompt):
            state["prompts"].append(prompt)
            return None
        monkeypatch.setattr(C, "_call_compat", fake_call)
        await real_generate(threads, prior, owner_md, *a, **k)
        return {"letter": "a letter", **state["proposal"]}

    real_generate = C._generate_consolidation

    async def fake_threads(_sid):
        return [{"user": "hi", "assistant": "hello", "timestamp": _TS}]

    async def fake_sleep():
        return None

    monkeypatch.setattr(C, "_generate_consolidation", fake_generate)
    monkeypatch.setattr(C, "threads_for_session", fake_threads)
    monkeypatch.setattr(C, "latest_sleep", fake_sleep)
    monkeypatch.setattr(C, "_snapshot_unconsolidated_ids_sync", lambda _u: [])
    monkeypatch.setattr(C, "_mark_facts_consolidated_sync", lambda ids: None)
    import backend.tools.vault as V
    monkeypatch.setattr(V, "_VAULT", vault)
    monkeypatch.setattr(V, "_index_note_async", lambda *a, **k: None)
    yield {"vault": vault, "state": state}
    db._reset_for_tests()


def _portrait_path(env):
    return env["vault"] / "people" / "owner.md"


def test_prompt_asks_for_the_right_field(env):
    p = _portrait_path(env)
    p.write_text("# Portrait\n\n## Situation\nold prose\n", encoding="utf-8")
    run(C.consolidate_session("sess-v1-aaaa"))
    v1_prompt = env["state"]["prompts"][-1]
    assert '"owner_update"' in v1_prompt and '"portrait_patch"' not in v1_prompt
    assert "<!-- portrait v2 -->" in v1_prompt

    p.write_text(V2, encoding="utf-8")
    run(C.consolidate_session("sess-v2-bbbb"))
    v2_prompt = env["state"]["prompts"][-1]
    assert '"portrait_patch"' in v2_prompt and '"owner_update"' not in v2_prompt
    for phrase in ("wrong or stale", "still holds", "commit hashes", "command lists",
                   "dated incidents", "what is true now and can change", "lasting"):
        assert phrase in v2_prompt


def test_migration_writes_the_rewrite_once_then_patches(env):
    p = _portrait_path(env)
    p.write_text("# Portrait\n\nold v1 prose\n", encoding="utf-8")
    env["state"]["proposal"] = {"letter": "l", "owner_update": V2}
    run(C.consolidate_session("sess-mig-cccc"))
    assert p.read_text(encoding="utf-8") == V2

    # now v2: a stray full rewrite is ignored, the patch is applied
    env["state"]["proposal"] = {"letter": "l2", "owner_update": "# Portrait\nWIPE",
                                "portrait_patch": [{"section": "Situation", "action": "add",
                                                    "text": "Moving flat in December"}]}
    run(C.consolidate_session("sess-pat-dddd"))
    out = p.read_text(encoding="utf-8")
    assert "WIPE" not in out and "Moving flat in December" in out and P.is_v2(out)


def test_patch_ages_out_and_replay_does_not_repeat(env):
    p = _portrait_path(env)
    p.write_text(V2, encoding="utf-8")
    hist = env["vault"] / "people" / "owner-history.md"
    env["state"]["proposal"] = {"letter": "l", "portrait_patch": [
        {"section": "Situation", "action": "add", "text": "Moving flat in December"}]}
    sid = "sess-age-eeee"
    run(C.consolidate_session(sid))
    out = p.read_text(encoding="utf-8")
    assert "stitch" not in out and "Moving flat in December" in out
    assert hist.read_text(encoding="utf-8").count("stitch") == 1

    # replay: the per-output state says it is done, and history is content-idempotent
    before = (p.read_text(encoding="utf-8"), hist.read_text(encoding="utf-8"))
    run(C.consolidate_session(sid))
    assert (p.read_text(encoding="utf-8"), hist.read_text(encoding="utf-8")) == before


def test_history_append_is_idempotent_when_portrait_write_is_retried(env, monkeypatch):
    p = _portrait_path(env)
    p.write_text(V2, encoding="utf-8")
    hist = env["vault"] / "people" / "owner-history.md"
    run(C._apply_portrait([], "2026-11-07"))           # history lands, portrait lands
    p.write_text(V2, encoding="utf-8")                  # as if the portrait write had been lost
    run(C._apply_portrait([], "2026-11-07"))
    assert hist.read_text(encoding="utf-8").count("stitch") == 1
    assert "stitch" not in p.read_text(encoding="utf-8")


def test_a_date_the_model_wrote_itself_is_stripped_even_with_a_full_stop():
    """Live replay, 7 November: the model ended its text with '(as of 2026-11-07).' and
    matched a line with its date included; the line got two dates and the match missed."""
    from backend.brain import portrait as P
    md = ("# Portrait\n<!-- portrait v2 -->\n\n## Situation\n"
          "- The Acme and Globex applications are off the list (as of 2026-11-01)\n\n## Who they are\n\n## How to be with them\n")
    out, report = P.apply_patch(md, [{"section": "Situation", "action": "revise",
                                      "match": "The Acme and Globex applications are off the list (as of 2026-11-01).",
                                      "text": "Initech is off too (as of 2026-11-07)."}], "2026-11-07")
    assert report == []
    assert "- Initech is off too (as of 2026-11-07)\n" in out
    assert out.count("(as of") == 1

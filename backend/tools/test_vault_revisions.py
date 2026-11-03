"""A05 slice 3: two writers do not lose updates.

Roadmap acceptance: *"two writers do not lose updates"*, root cause *"vault writes lack
revision coordination"*.

`research._tick` reads a `topics/` note, awaits a synthesis LLM call measured in
**minutes** (the truncation-continuation notes record one at 2+ minutes), then writes a
full rewrite derived from that read. A research tick only checks the pipeline lock at
*tick start*, so a conversation can freely write the same note while synthesis runs —
and the rewrite then deletes it.

Reproduced before the fix: a note updated mid-tick with "Sam just told me he SOLD
the position" was replaced by the rewrite built from the pre-write read, silently.
`test_the_reproduced_incident` is that scenario.
"""
import asyncio

import pytest

import backend.tools.vault as V


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for d in ("topics", "people"):
        (root / d).mkdir(parents=True)
    monkeypatch.setattr(V, "_VAULT", root)
    monkeypatch.setattr(V, "_index_note_async", lambda *a, **k: None)
    return root


# ------------------------------------------------------------- the revision marker
def test_revision_changes_with_content(vault):
    run(V.vault_write("n.md", "one", "topics"))
    first = V.note_revision("topics", "n.md")
    run(V.vault_write("n.md", "two", "topics"))
    assert V.note_revision("topics", "n.md") != first


def test_revision_of_a_missing_note_is_empty(vault):
    assert V.note_revision("topics", "nope.md") == ""


def test_changed_then_changed_back_compares_equal(vault):
    """A content hash, not an mtime: the caller cares whether its read went stale, not
    how many times the file was touched. mtime granularity on Windows is also coarse
    enough for two writes in one tick to share a timestamp."""
    run(V.vault_write("n.md", "original", "topics"))
    rev = V.note_revision("topics", "n.md")
    run(V.vault_write("n.md", "changed", "topics"))
    run(V.vault_write("n.md", "original", "topics"))
    assert V.note_revision("topics", "n.md") == rev


def test_revision_never_raises_on_a_bad_folder(vault):
    assert V.note_revision("no_such_folder", "n.md") == ""


# --------------------------------------------------------------- the precondition
def test_the_reproduced_incident(vault):
    """The live repro, kept as a regression test."""
    note = vault / "topics" / "nvidia.md"
    note.write_text("# NVIDIA\n\nOriginal body.\n", encoding="utf-8")

    existing = note.read_text(encoding="utf-8")
    rev = V.note_revision("topics", "nvidia.md")          # research reads

    run(V.vault_write("nvidia.md",                        # a conversation writes
                      "# NVIDIA\n\nSam just told me he SOLD the position.\n",
                      "topics"))

    rewritten = existing.replace("Original body.", "Original body plus findings.")
    result = run(V.vault_write("nvidia.md", rewritten, "topics", expected_revision=rev))

    assert "SOLD the position" in note.read_text(encoding="utf-8"), (
        "the concurrent write was destroyed by a rewrite from a stale read")
    assert result.startswith("Error:")
    assert "changed since you read it" in result


def test_an_unchanged_note_still_writes(vault):
    """The control. A precondition that blocks ordinary writes is worse than none."""
    run(V.vault_write("n.md", "original", "topics"))
    rev = V.note_revision("topics", "n.md")
    result = run(V.vault_write("n.md", "updated", "topics", expected_revision=rev))
    assert "Confirmed written" in result
    assert (vault / "topics" / "n.md").read_text(encoding="utf-8") == "updated"


def test_callers_that_pass_nothing_are_unaffected(vault):
    """Every existing caller omits the argument and must behave exactly as before."""
    run(V.vault_write("n.md", "first", "topics"))
    result = run(V.vault_write("n.md", "second", "topics"))
    assert "Confirmed written" in result
    assert (vault / "topics" / "n.md").read_text(encoding="utf-8") == "second"


def test_creating_a_new_note_under_a_precondition(vault):
    """A note that did not exist at read time has revision "" — creating it is not a
    conflict, but losing a race to create it is."""
    assert "Confirmed written" in run(
        V.vault_write("fresh.md", "body", "topics", expected_revision=""))

    run(V.vault_write("raced.md", "someone got there first", "topics"))
    result = run(V.vault_write("raced.md", "my version", "topics", expected_revision=""))
    assert result.startswith("Error:")
    assert (vault / "topics" / "raced.md").read_text(encoding="utf-8") == \
        "someone got there first"


def test_a_refused_write_leaves_the_file_completely_untouched(vault):
    run(V.vault_write("n.md", "theirs", "topics"))
    run(V.vault_write("n.md", "theirs, then edited", "topics"))
    run(V.vault_write("n.md", "mine", "topics", expected_revision="stale-hash"))
    assert (vault / "topics" / "n.md").read_text(encoding="utf-8") == "theirs, then edited"


def test_a_refusal_classifies_as_a_failure():
    """It must reach the tool loop as ok=False, not as a success whose text happens to
    mention an error -- and via tools/outcomes.py, not a second text match."""
    from backend.tools import outcomes
    msg = V._STALE_WRITE_MSG.format(folder="topics", filename="n.md")
    assert outcomes.is_failure("vault_write", msg)


# ------------------------------------------------------------- research's handling
def test_research_captures_the_revision_before_synthesis():
    """Capturing it after the LLM call would make the precondition meaningless."""
    import inspect
    from backend.brain import research

    src = inspect.getsource(research._tick)
    read_at = src.index("existing_revision = note_revision")
    synth_at = src.index("synth_raw = await _llm(")
    assert read_at < synth_at, (
        "the revision is captured after synthesis -- it would always match and protect "
        "nothing")


def test_every_research_note_write_carries_the_precondition():
    """Every write path rewrites the whole note from the same stale read."""
    import inspect
    from backend.brain import research

    src = inspect.getsource(research._tick)
    # Counted per call rather than by one literal call shape: the no-progress path
    # writes the note through a differently formatted call and must still count.
    assert src.count("await vault_write(") == \
        src.count("expected_revision=existing_revision"), (
        "a research note write bypasses the revision precondition")


def test_research_treats_a_refusal_as_a_failed_pass():
    """Before this, `write_result` was logged and never checked -- so a refused write
    still claimed 'wrote topics/x.md', DM'd Sam about findings that were never
    saved, and logged a successful pass."""
    import inspect
    from backend.brain import research

    src = inspect.getsource(research._tick)
    assert "_is_write_refused(write_result)" in src, (
        "research no longer checks whether the note write was refused -- a declined "
        "write would again be reported as a successful pass")
    assert src.index("_is_write_refused(write_result)") < \
        src.index('outcome = f"wrote topics/{filename}'), \
        "the success path runs before the refusal is checked"


def test_the_refusal_check_uses_the_shared_classifier():
    from backend.brain import research
    from backend.tools import vault

    assert research._is_write_refused(
        vault._STALE_WRITE_MSG.format(folder="topics", filename="n.md")) is True
    assert research._is_write_refused(
        "Confirmed written to jarvis-vault/topics/n.md (10 chars, verified by read-back)."
    ) is False

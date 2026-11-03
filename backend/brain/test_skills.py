"""Skills, slice 1 (4 October 2026): a list in the prompt, opened on demand.

Runs against a temporary vault and the real SQLite store (conftest gives each test its own
database). The three skills used here are the server's real ones, in their June format.
"""
import asyncio
import inspect
import re
import textwrap

import pytest

from backend.brain import personality
from backend.brain import skills
from backend.storage import skills as store
from backend.tools import vault

CREATIVE_JUNE = textwrap.dedent("""\
    ---
    name: creative-write-on-demand
    description: When the user invites open-ended creative writing with phrases like 'write something if you want,' produce an original piece and save it to the creative/ vault folder.
    category: creative
    tags: [creative, writing, spontaneity, vault]
    requires_tools: [vault_write]
    confidence: 0.86
    source: learned
    version: 1.0.0
    status: active
    created: 2026-06-10
    uses: 103
    successes: 94
    success_rate: 0.91
    last_used: 2026-10-04
    ---

    ## When to use
    User explicitly invites creative writing without a specific topic constraint.

    ## Procedure
    1. Recognize the invitation as a trigger for unscripted creative output.
    2. Save to creative/ with kebab-case filename.

    ## Pitfalls
    - Do not overwrite existing files.
    """)

STOCK_JUNE = textwrap.dedent("""\
    ---
    name: structured-financial-research
    description: Multi-vector research workflow for stocks and financial assets with parallel query execution, structured synthesis, and automatic vault archiving
    category: research
    requires_tools: []
    confidence: 0.98
    status: active
    created: 2026-06-10
    uses: 64
    ---

    ## Procedure
    1. Identify 4 parallel research vectors.
    2. Execute web searches for each vector simultaneously.
    """)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _live_turn():
    """These calls stand for the live turn; the gateway sets the caller in production."""
    from backend.tools import policy
    token = policy.current_caller.set(policy.LIVE)
    yield
    policy.current_caller.reset(token)


@pytest.fixture
def vault_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    (tmp_path / "skills").mkdir()
    return tmp_path


def put(vault_dir, name, text):
    (vault_dir / "skills" / f"{name}.md").write_bytes(text.encode("utf-8"))


def save(**kw):
    args = dict(name="meeting-prep", description="Prepare Sam for a meeting: who, history, open points.",
                when_to_use="He has a meeting coming up and asks to be prepared.",
                steps=["Read the calendar event with calendar_action", "Search his mail for the attendees"],
                tools=["calendar_action", "gmail_action"])
    args.update(kw)
    return run(skills.skill("save", **args))


# ------------------------------------------------------------------ the June format
def test_the_june_files_still_read(vault_dir):
    s = skills.parse(CREATIVE_JUNE, "x")
    assert s.name == "creative-write-on-demand" and s.active
    assert s.tools == ["vault_write"]                      # from the old requires_tools
    assert s.description.startswith("When the user invites")
    # the meaningless counters are not carried as data
    assert not ({"uses", "successes", "success_rate", "confidence"} & set(s.extra))
    assert s.steps() == 2


def test_a_file_without_frontmatter_is_still_a_skill():
    s = skills.parse("Do the weekly review.\n\n## Procedure\n1. Count the week", "weekly")
    assert s.name == "weekly" and s.description == "Do the weekly review." and s.active


def test_an_unclosed_frontmatter_block_is_text_not_metadata():
    s = skills.parse("---\nname: x\nno closing fence", "fallback")
    assert s.name == "fallback"


def test_render_and_parse_round_trip_with_a_colon_in_the_description():
    s = skills.Skill(name="a-b", description="Research: then write", body="## Procedure\n1. go",
                     tools=["web_browse"], created="2026-10-04", updated="2026-10-04",
                     extra={"superseded_by": "c"})
    back = skills.parse(skills.render(s), "")
    assert (back.name, back.description, back.tools, back.extra) == \
        ("a-b", "Research: then write", ["web_browse"], {"superseded_by": "c"})
    assert 'description: "Research: then write"' in skills.render(s)


# ------------------------------------------------------------------ the live bug, fixed
def test_an_unrelated_message_no_longer_gets_a_skill_pasted_in(vault_dir):
    """Reproduced 4 October with the server's real skills: the 0.68 similarity floor gave
    'what's on my calendar tomorrow' the creative-writing procedure. Now only the one-line
    list is in the prompt; the procedure comes only when JARVIS opens it."""
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    stable, volatile = personality.build_system_prompt("what's on my calendar tomorrow")
    assert "- creative-write-on-demand: When the user invites" in stable
    assert "Recognize the invitation" not in stable + volatile


def test_the_list_is_in_the_cached_block_and_not_the_volatile_one(vault_dir):
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    stable, volatile = personality.build_system_prompt("hello")
    assert skills.CATALOG_FRAME in stable and skills.CATALOG_FRAME not in volatile


def test_no_skills_means_no_list_at_all(vault_dir):
    stable, volatile = personality.build_system_prompt("hello")
    assert skills.CATALOG_FRAME not in stable and "Skills you keep" not in volatile


def test_the_names_ride_with_the_turn_but_not_the_procedures(vault_dir):
    """Measured live: with the list only in the cached block, 5 of 10 research requests
    opened the skill; with the names also beside his message, 10 of 10, and 0 of 12
    unrelated messages opened one."""
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    put(vault_dir, "structured-financial-research", STOCK_JUNE)
    run(skills.skill("retire", name="structured-financial-research"))
    _, volatile = personality.build_system_prompt("dig into SPCX")
    line = skills.reminder_line()
    assert line in volatile and "creative-write-on-demand" in line
    assert "structured-financial-research" not in line           # retired
    assert "When the user invites" not in line and "Recognize" not in volatile


def test_the_list_is_stable_from_turn_to_turn_even_as_skills_are_opened(vault_dir):
    """No counters in it: opening a skill must not change the cached prefix."""
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    put(vault_dir, "structured-financial-research", STOCK_JUNE)
    first = skills.catalog_text()
    run(skills.skill("view", name="creative-write-on-demand"))
    assert skills.catalog_text() == first
    assert re.search(r"\d+ (uses|times)|opened", first) is None


def test_the_list_is_sorted_active_only_and_changes_when_a_skill_does(vault_dir):
    put(vault_dir, "structured-financial-research", STOCK_JUNE)
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    put(vault_dir, "_notes", "---\nname: notes\ndescription: not a skill\n---\n")
    lines = skills.catalog_text().splitlines()[1:]
    assert [l.split(":")[0] for l in lines] == ["- creative-write-on-demand", "- structured-financial-research"]
    run(skills.skill("retire", name="structured-financial-research", reason="merged"))
    assert "structured-financial-research" not in skills.catalog_text()


def test_a_long_hand_edited_description_is_cut_in_the_list(vault_dir):
    put(vault_dir, "long", f"---\nname: long\ndescription: {'word ' * 100}\n---\nbody")
    line = skills.catalog_text().splitlines()[1]
    assert len(line) <= len("- long: ") + skills.CATALOG_LINE and line.endswith("…")


# ------------------------------------------------------------------ view
def test_view_returns_the_procedure_and_records_one_use(vault_dir):
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    out = run(skills.skill("view", name="creative-write-on-demand"))
    assert "Recognize the invitation" in out and out.startswith("Skill 'creative-write-on-demand'")
    assert store.use_stats()["creative-write-on-demand"]["uses"] == 1


def test_view_of_an_unknown_skill_names_the_close_ones(vault_dir):
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    out = run(skills.skill("view", name="creative-writing"))
    assert out.startswith("Error") and "creative-write-on-demand" in out
    assert store.use_stats() == {}


def test_view_of_a_retired_skill_is_refused(vault_dir):
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    run(skills.skill("retire", name="creative-write-on-demand"))
    assert "retired" in run(skills.skill("view", name="creative-write-on-demand"))


# ------------------------------------------------------------------ save
def test_save_writes_reads_back_and_confirms(vault_dir):
    out = save(name="Meeting Prep", source="taught")
    assert out.startswith("Confirmed: skill 'meeting-prep'") and "2 steps" in out and "new" in out
    s = skills.load("meeting-prep")
    assert s.source == "taught" and s.tools == ["calendar_action", "gmail_action"]
    assert "## Procedure\n1. Read the calendar event" in s.body
    assert "meeting-prep" in skills.catalog_text()


def test_saving_over_a_skill_keeps_the_old_version(vault_dir):
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    out = save(name="creative-write-on-demand", description="Write a short piece when invited.",
               steps=["Write it", "Save it"], tools=["vault_write"])
    assert "replaced the earlier version, which is kept" in out
    [rev] = store.revisions("creative-write-on-demand")
    assert rev["action"] == "replace" and rev["content"] == CREATIVE_JUNE
    s = skills.load("creative-write-on-demand")
    assert s.created == "2026-06-10"                      # kept from the old file
    assert "uses:" not in (vault_dir / "skills" / "creative-write-on-demand.md").read_text(encoding="utf-8")


def test_saving_over_a_skill_is_refused_when_the_old_version_cannot_be_kept(vault_dir, monkeypatch):
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    monkeypatch.setattr(store, "add_revision", lambda *a, **k: None)
    assert save(name="creative-write-on-demand").startswith("Error")
    assert (vault_dir / "skills" / "creative-write-on-demand.md").read_text(encoding="utf-8") == CREATIVE_JUNE


def test_saving_a_retired_skill_brings_it_back(vault_dir):
    save()
    run(skills.skill("retire", name="meeting-prep"))
    save()
    assert skills.load("meeting-prep").active


@pytest.mark.parametrize("kw, needle", [
    (dict(name="!!!"), "name"),
    (dict(description=""), "description"),
    (dict(description="x" * 301), "under 300"),
    (dict(steps=[]), "at least one step"),
    (dict(steps=["s"] * 21), "at most 20"),
    (dict(tools=["web_browse", "teleport"]), "teleport"),
    (dict(source="invented"), "source"),
])
def test_bad_skills_are_refused_and_nothing_is_written(vault_dir, kw, needle):
    out = save(**kw)
    assert out.startswith("Error") and needle in out
    assert list((vault_dir / "skills").iterdir()) == []


def test_the_library_is_capped(vault_dir, monkeypatch):
    monkeypatch.setattr(skills, "MAX_ACTIVE", 1)
    save()
    assert "Retire one" in save(name="another")


def test_a_write_that_does_not_read_back_is_not_confirmed(vault_dir, monkeypatch):
    real = skills.parse
    monkeypatch.setattr(skills, "parse", lambda text, fb="": skills.Skill(name="other", description="", body=""))
    assert "NOT saved" in save()
    monkeypatch.setattr(skills, "parse", real)


def test_numbering_the_model_adds_is_not_doubled(vault_dir):
    save(steps=["1. Read the event", "2) Search the mail", "- Draft the brief"])
    assert "1. Read the event\n2. Search the mail\n3. Draft the brief" in skills.load("meeting-prep").body


# ------------------------------------------------------------------ retire
def test_retire_keeps_the_file_and_the_old_text(vault_dir):
    put(vault_dir, "structured-financial-research", STOCK_JUNE)
    out = run(skills.skill("retire", name="structured-financial-research", reason="merged"))
    assert out.startswith("Retired")
    assert skills.load("structured-financial-research").status == "retired"
    assert store.revisions("structured-financial-research")[0]["content"] == STOCK_JUNE
    assert "already retired" in run(skills.skill("retire", name="structured-financial-research"))


# ------------------------------------------------------------------ export
def test_export_is_a_valid_agent_skills_file(vault_dir):
    save()
    filename, text, caption = skills.export_file("meeting-prep")
    assert filename == "SKILL.md" and "meeting-prep/SKILL.md" in caption
    head, body = text.split("\n---\n", 1)
    keys = [l.split(":")[0] for l in head.splitlines()[1:] if l and not l.startswith(" ")]
    allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert set(keys) <= allowed and keys[:2] == ["name", "description"]
    assert re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", "meeting-prep")
    # metadata values are strings, as the standard requires
    meta = [l for l in head.splitlines() if l.startswith("  ")]
    assert meta and all(re.match(r'^  [a-z]+: ".*"$', l) for l in meta)
    assert "## Procedure" in body


def test_export_sends_the_file_and_says_so(vault_dir):
    save()
    sent = {}

    async def sender(filename, text, caption):
        sent.update(filename=filename, text=text)
        return True, "in this conversation, as the attached file SKILL.md"
    skills.set_file_sender(sender)
    try:
        out = run(skills.skill("export", name="meeting-prep"))
    finally:
        skills.set_file_sender(None)
    assert out.startswith("Sent 'meeting-prep'") and sent["filename"] == "SKILL.md"
    assert "name: meeting-prep" in sent["text"]


def test_export_without_discord_or_a_failed_send_is_not_claimed(vault_dir):
    save()
    assert run(skills.skill("export", name="meeting-prep")).startswith("Error: Discord is not connected")

    async def fails(*a):
        return False, "uncertain"
    skills.set_file_sender(fails)
    try:
        assert "Don't say it was sent" in run(skills.skill("export", name="meeting-prep"))
    finally:
        skills.set_file_sender(None)


def test_export_of_an_unknown_skill(vault_dir):
    assert skills.export_file("nope").startswith("Error")


# ------------------------------------------------------------------ /skill and /skills
def test_slash_skill_brings_the_procedure_and_counts_as_his_use(vault_dir):
    put(vault_dir, "structured-financial-research", STOCK_JUNE)
    name, block = skills.forced("/skill structured-financial-research TTWO before earnings")
    assert name == "structured-financial-research"
    assert "for his request (TTWO before earnings)" in block and "Identify 4 parallel" in block
    [(skill_name, stats)] = store.use_stats().items()
    assert skill_name == name and stats["uses"] == 1


def test_slash_skill_with_an_unknown_name_tells_him_so(vault_dir):
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    name, block = skills.forced("/skill creative TTWO")
    assert name == "" and "no skill by that name" in block and "creative-write-on-demand" in block


@pytest.mark.parametrize("text", ["/skills", "/skill export meeting-prep", "research TTWO",
                                  "what does /skill do?", ""])
def test_other_text_is_not_a_skill_run(vault_dir, text):
    assert skills.forced(text) == ("", "")


def test_the_list_command_shows_uses_and_retired_skills(vault_dir):
    assert "No skills yet" in skills.handle_skills_command("")
    put(vault_dir, "creative-write-on-demand", CREATIVE_JUNE)
    put(vault_dir, "structured-financial-research", STOCK_JUNE)
    run(skills.skill("view", name="creative-write-on-demand"))
    run(skills.skill("retire", name="structured-financial-research"))
    text = skills.handle_skills_command("")
    assert "• creative-write-on-demand — opened 1 time, last" in text
    assert "Retired (1): structured-financial-research" in text
    assert skills.handle_skills_command("nonsense").startswith("Usage")


# ------------------------------------------------------------------ the store
def test_the_store_rejects_an_unknown_way_in():
    with pytest.raises(ValueError):
        store.record_use("x", "telepathy")


def test_revisions_are_newest_first():
    store.add_revision("x", "replace", "one")
    store.add_revision("x", "retire", "two")
    assert [r["content"] for r in store.revisions("x")] == ["two", "one"]


def test_storage_unavailable_never_raises(monkeypatch):
    monkeypatch.setattr(store, "_conn", lambda: None)
    assert store.record_use("x", "model") is False
    assert store.use_stats() == {} and store.revisions("x") == []
    assert store.add_revision("x", "replace", "t") is None


# ------------------------------------------------------------------ the claim check
def test_saying_a_skill_was_saved_needs_a_save_that_worked():
    from backend.brain import engine
    reply = "Done, I saved that as a skill so next time it's quicker."
    assert engine._unbacked_claims(reply, set(), 0, asked_to_save=True)
    assert engine._unbacked_claims(reply, {"skill", "skill:view"}, 1, asked_to_save=True)
    assert not engine._unbacked_claims(reply, {"skill", "skill:save"}, 1, asked_to_save=True)


def test_describing_a_skill_from_the_list_is_not_a_claim():
    from backend.brain import engine
    reply = "You already have a stock-research skill; the skill is saved in your vault."
    assert not engine._unbacked_claims(reply, {"skill:catalog"}, 0)
    # ...but a save he asks for now still needs the save
    assert engine._unbacked_claims(reply, {"skill:catalog"}, 0, asked_to_save=True)


def test_the_catalog_marker_is_set_when_the_list_was_in_the_prompt():
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    assert '_skills.CATALOG_FRAME in system_stable' in src and '"skill:catalog"' in src


# ------------------------------------------------------------------ wiring
def test_chat_no_longer_picks_a_skill_by_similarity_and_reads_slash_skill():
    from backend.brain import engine, semindex
    src = inspect.getsource(engine.chat)
    assert "_read_relevant_skill_named" not in src and "record_skill_outcome" not in src
    assert "_skill_context(user_text, channel," in src and "_skills.forced(user_text)" in inspect.getsource(engine._skill_context)
    assert not hasattr(semindex, "recall_skill")
    assert not hasattr(personality, "read_relevant_skill_named")


def test_the_tool_is_registered_and_dispatched():
    from backend.brain import engine
    from backend.tools.registry import TOOLS
    names = {t["name"] for t in TOOLS}
    assert "skill" in names and "vault_skill_write" not in names
    assert engine.TOOL_DISPATCH["skill"] is skills.skill
    schema = next(t for t in TOOLS if t["name"] == "skill")["input_schema"]
    assert schema["properties"]["action"]["enum"] == ["list", "view", "save", "learn", "patch", "undo", "retire", "export"]
    assert "skill" in engine._PERSISTENCE_TOOLS


def test_opening_a_skill_is_a_read_and_saving_one_is_his_own_space():
    from backend.tools import policy
    assert policy.risk_of("skill", {"action": "view"}) == policy.READ
    assert policy.risk_of("skill", {"action": "list"}) == policy.READ
    assert policy.risk_of("skill", {"action": "save"}) == policy.OWN
    assert policy.risk_of("skill", {"action": "export"}) == policy.OWN


def test_the_discord_commands_are_wired():
    from backend import main
    from backend.brain import discord_bridge
    src = inspect.getsource(discord_bridge)
    assert 'text.lower() == "/skills"' in src and 'startswith("/skill export")' in src
    cmds = [c for c, _ in discord_bridge._COMMAND_HELP]
    assert "/skills" in cmds and any(c.startswith("/skill <name>") for c in cmds)
    msrc = inspect.getsource(main)
    assert "skills_handler      = skills.handle_skills_command" in msrc
    assert "skills.set_file_sender(discord_bridge.send_file)" in msrc


def test_he_is_told_about_the_skill_tool():
    core = personality._INVARIANT_CORE
    assert 'skill(action="view")' in core and 'skill(action="save")' in core
    assert "/skill <name>" in core and "vault_skill_write" not in core

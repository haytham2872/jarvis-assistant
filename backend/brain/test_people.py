"""Tests for person profiles (backend/brain/people.py).

Pure-function-first like the rest of the suite: the vault is redirected to tmp_path and
semantic indexing is stubbed, so nothing reaches the real vault or Qdrant.
"""
import asyncio

import pytest

from backend.brain import people as P
from backend.tools import vault as V

TODAY = "2026-09-26"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "jarvis-vault"
    (root / "people").mkdir(parents=True)
    monkeypatch.setattr(V, "_VAULT", root)
    monkeypatch.setattr(V, "_index_note_async", lambda *a, **k: None)
    monkeypatch.setattr(P, "_today", lambda: TODAY)
    return root


def specs():
    return P.parse_template(P.DEFAULT_TEMPLATE)


def titles_for(kind):
    return [s.title for s in specs() if s.applies_to(kind)]


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

def test_default_template_parses_every_section_with_guidance():
    s = specs()
    assert len(s) >= 20
    assert all(x.about for x in s), "every section says what belongs in it"
    assert [x.title for x in s][0] == "Snapshot"
    assert P.REVISIONS not in [x.title for x in s]


def test_known_sections_all_have_an_interview_question():
    for s in specs():
        if s.applies_to("known"):
            assert s.ask, f"{s.title} applies to people Sam knows but has no question"


def test_researchable_sections_say_what_to_research():
    for s in specs():
        if s.applies_to("historical") and s.title != "Why they matter to Sam":
            assert s.research, f"{s.title} applies to historical figures but has no research brief"


def test_kinds_get_different_but_overlapping_sections():
    known, historical = titles_for("known"), titles_for("historical")
    assert "With Sam" in known and "With Sam" not in historical
    assert "Legacy and influence" in historical and "Legacy and influence" not in known
    assert "Personality and temperament" in known and "Personality and temperament" in historical


def test_template_keys_and_fallbacks():
    text = """# T
## Alpha
<!--
applies: known
enough: 1
ask: Who?
-->
## Beta
Just prose, no comment.
## Gamma
<!-- applies: nobody -->
## Revisions
"""
    s = P.parse_template(text)
    assert [x.title for x in s] == ["Alpha", "Beta"]   # unknown kinds and Revisions dropped
    assert s[0].applies == frozenset({"known"}) and s[0].enough == 1 and s[0].ask == "Who?"
    assert s[1].applies == frozenset(P.KINDS) and s[1].enough == 2


def test_template_is_seeded_on_first_use_and_then_read_from_the_vault(vault):
    s = P.load_template()
    path = vault / "people" / P.TEMPLATE_FILE
    assert path.exists() and len(s) == len(specs())
    path.write_text("## Only this\n<!-- applies: all -->\n", encoding="utf-8")
    assert [x.title for x in P.load_template()] == ["Only this"]


def test_an_unusable_template_falls_back_to_the_default(vault):
    (vault / "people" / P.TEMPLATE_FILE).write_text("no sections at all", encoding="utf-8")
    assert len(P.load_template()) == len(specs())


# ---------------------------------------------------------------------------
# Attribution and edits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source, expected", [
    ("owner", f"Sam, {TODAY}"),
    ("Sam", f"Sam, {TODAY}"),
    ("inference", f"my inference, {TODAY}"),
    ("https://en.wikipedia.org/wiki/Marcus_Aurelius",
     "[en.wikipedia.org](https://en.wikipedia.org/wiki/Marcus_Aurelius)"),
    ("Meditations, Book II", "Meditations, Book II"),
    ("", ""),
])
def test_attribution(source, expected):
    assert P.attribution(source, TODAY) == expected


def test_a_citation_cannot_break_the_entry_separator():
    assert " — " not in P.attribution("Hadot — The Inner Citadel", TODAY)


def new(kind="known"):
    return P.Profile(name="Karim Benali", kind=kind, created=TODAY, updated=TODAY)


def test_add_records_the_source_on_the_entry():
    p = new()
    n, report = P.apply_edits(p, [{"section": "Personality and temperament",
                                   "text": "Goes quiet rather than argue", "source": "owner"}],
                              specs(), TODAY)
    assert n == 1 and "added" in report[0]
    assert p.section("Personality and temperament") == [
        f"- Goes quiet rather than argue — Sam, {TODAY}"]


def test_an_entry_without_a_source_is_refused():
    """The A05 rule: nothing lands without saying whose word it is."""
    p = new()
    n, report = P.apply_edits(p, [{"section": "Snapshot", "text": "Runs a bakery"}], specs(), TODAY)
    assert n == 0 and "no source" in report[0] and not p.sections.get("Snapshot")


def test_inference_is_labelled_as_mine_not_his():
    p = new()
    P.apply_edits(p, [{"section": "Personality", "text": "Probably shy", "source": "inference"}],
                  specs(), TODAY)
    [line] = p.section("Personality and temperament")
    assert "my inference" in line and "Sam" not in line


def test_section_names_resolve_by_unique_prefix_and_case():
    p = new()
    n, _ = P.apply_edits(p, [{"section": "values", "text": "Loyalty", "source": "owner"}],
                         specs(), TODAY)
    assert n == 1 and p.section("Values and beliefs")


def test_a_section_that_does_not_apply_to_this_kind_is_refused():
    p = new("historical")
    n, report = P.apply_edits(p, [{"section": "With Sam", "text": "x", "source": "owner"}],
                              specs(), TODAY)
    assert n == 0 and "not used for a historical person" in report[0]


def test_unknown_section_and_revisions_are_refused():
    p = new()
    n, report = P.apply_edits(p, [
        {"section": "Horoscope", "text": "x", "source": "owner"},
        {"section": "Revisions", "text": "x", "source": "owner"},
    ], specs(), TODAY)
    assert n == 0 and all("skipped" in r for r in report)


def test_a_duplicate_add_is_a_no_op():
    p = new()
    e = {"section": "Snapshot", "text": "Runs a bakery in Lyon", "source": "owner"}
    P.apply_edits(p, [e], specs(), TODAY)
    n, report = P.apply_edits(p, [dict(e, text="runs a  bakery in lyon")], specs(), TODAY)
    assert n == 0 and "already there" in report[0]


def test_revise_replaces_one_entry_and_records_the_old_text():
    p = new()
    P.apply_edits(p, [{"section": "Snapshot", "text": "Runs a bakery in Lyon", "source": "owner"}],
                  specs(), TODAY)
    n, _ = P.apply_edits(p, [{"section": "Snapshot", "action": "revise", "match": "bakery",
                              "text": "Runs a café in Lyon", "source": "owner"}], specs(), TODAY)
    assert n == 1
    assert p.section("Snapshot") == [f"- Runs a café in Lyon — Sam, {TODAY}"]
    [note] = p.section(P.REVISIONS)
    assert 'revised "Runs a bakery in Lyon" to "Runs a café in Lyon"' in note


def test_revise_needs_exactly_one_match():
    p = new()
    P.apply_edits(p, [{"section": "Interests", "text": "Loves chess", "source": "owner"},
                      {"section": "Interests", "text": "Loves cycling", "source": "owner"}],
                  specs(), TODAY)
    n, report = P.apply_edits(p, [{"section": "Interests", "action": "remove", "match": "loves"}],
                              specs(), TODAY)
    assert n == 0 and "2 entries" in report[0]
    assert len(p.section("Interests and passions")) == 2


def test_remove_drops_an_emptied_section_but_keeps_the_record():
    p = new()
    P.apply_edits(p, [{"section": "Snapshot", "text": "Wrong", "source": "owner"}], specs(), TODAY)
    n, _ = P.apply_edits(p, [{"section": "Snapshot", "action": "remove", "match": "wrong",
                              "source": "owner"}], specs(), TODAY)
    assert n == 1 and "Snapshot" not in p.sections
    assert 'removed "Wrong"' in p.section(P.REVISIONS)[0]


def test_malformed_edits_never_raise():
    p = new()
    n, report = P.apply_edits(p, ["nope", {}, {"section": "Snapshot", "action": "explode",
                                               "text": "x", "source": "owner"}], specs(), TODAY)
    assert n == 0 and len(report) == 3


# ---------------------------------------------------------------------------
# Parse / render
# ---------------------------------------------------------------------------

def filled_profile():
    p = new()
    P.apply_edits(p, [
        {"section": "Things to remember", "text": "Birthday 3 March", "source": "owner"},
        {"section": "Snapshot", "text": "Childhood friend", "source": "owner"},
    ], specs(), TODAY)
    p.relation, p.aliases = "friend from school", ["Karim", "K"]
    return p


def test_render_uses_template_order_and_only_filled_sections():
    text = P.render_profile(filled_profile(), specs())
    assert text.index("## Snapshot") < text.index("## Things to remember")
    assert "## Personality" not in text                     # optional: absent until filled
    assert "type: person-profile" in text and "*Related: [[Sam]]*" in text


def test_round_trip_is_stable():
    text = P.render_profile(filled_profile(), specs())
    again = P.render_profile(P.parse_profile(text), specs())
    assert again == text
    p = P.parse_profile(text)
    assert p.name == "Karim Benali" and p.aliases == ["Karim", "K"] and p.relation == "friend from school"


def test_hand_edits_survive_a_rewrite():
    """Sam edits profiles in Obsidian: his own sections, prose, keys and links stay."""
    text = P.render_profile(filled_profile(), specs())
    text = text.replace("tags: [person]", "tags: [person]\nmood: fond")
    text = text.replace("## Snapshot", "Met at school in 2004.\n\n## Snapshot")
    text = text.replace("*Related: [[Sam]]*", "*Related: [[Sam]] · [[Lyon]]*")
    text = text.replace("---\n*Related", "## My own notes\nOwes me a book.\n\n---\n*Related")
    text = text.replace(f"- Childhood friend — Sam, {TODAY}",
                        f"- Childhood friend — Sam, {TODAY}\nA paragraph I wrote myself.")
    p = P.parse_profile(text)
    P.apply_edits(p, [{"section": "Snapshot", "text": "Lives in Lyon", "source": "owner"}],
                  specs(), TODAY)
    out = P.render_profile(p, specs())
    for kept in ("mood: fond", "Met at school in 2004.", "## My own notes", "Owes me a book.",
                 "[[Lyon]]", "A paragraph I wrote myself.", "Lives in Lyon"):
        assert kept in out, kept


def test_an_unknown_kind_reads_as_known():
    p = P.parse_profile("---\ntype: person-profile\nname: X\nkind: alien\n---\n# X\n")
    assert p.kind == "known"


def test_gaps_split_filled_thin_and_empty():
    p = filled_profile()
    filled, thin, empty = P.gaps(p, specs())
    assert [s.title for s in filled] == ["Snapshot"]            # enough: 1
    assert [s.title for s in thin] == ["Things to remember"]     # 1 of 2
    assert len(filled) + len(thin) + len(empty) == len(titles_for("known"))


def test_file_stem():
    assert P.file_stem("Jean-Paul Sartre") == "JeanPaulSartre"
    assert P.file_stem("marcus aurelius") == "MarcusAurelius"
    assert P.file_stem("Frédéric Chopin") == "FrédéricChopin"
    assert P.file_stem("  ") == ""


# ---------------------------------------------------------------------------
# The tool, end to end on a temporary vault
# ---------------------------------------------------------------------------

def test_create_update_read_gaps_list(vault):
    r = run(P.person_profile("create", name="Karim Benali", kind="known",
                             relation="friend from school", aliases=["Karim"],
                             edits=[{"section": "Snapshot", "text": "Childhood friend",
                                     "source": "owner"}]))
    assert r.startswith("Confirmed"), r
    path = vault / "people" / "KarimBenali.md"
    assert path.exists()

    r = run(P.person_profile("update", name="karim", edits=[
        {"section": "Personality", "text": "Dry humour", "source": "owner"}]))
    assert r.startswith("Confirmed"), r
    assert "Dry humour — Sam" in path.read_text(encoding="utf-8")

    assert "Dry humour" in run(P.person_profile("read", name="Karim", section="personality"))
    gaps = run(P.person_profile("gaps", name="Karim"))
    assert "Empty: With Sam -- ask:" in gaps and "Filled: Snapshot" in gaps
    assert "KarimBenali.md" in run(P.person_profile("list"))


def test_create_twice_is_refused(vault):
    run(P.person_profile("create", name="Ada Lovelace", kind="historical"))
    r = run(P.person_profile("create", name="ada lovelace", kind="historical"))
    assert r.startswith("Error:") and "already exists" in r


def test_create_needs_a_valid_kind(vault):
    assert run(P.person_profile("create", name="X", kind="friend")).startswith("Error:")


def test_historical_gaps_give_research_briefs(vault):
    run(P.person_profile("create", name="Marcus Aurelius", kind="historical"))
    gaps = run(P.person_profile("gaps", name="Marcus Aurelius"))
    assert "-- research:" in gaps and "With Sam" not in gaps


def test_the_portrait_is_never_touched(vault):
    """people/owner.md is consolidation's portrait, not a profile."""
    portrait = vault / "people" / "owner.md"
    portrait.write_text("## Situation\nThe real portrait.\n", encoding="utf-8")
    # Someone whose name is the portrait's file name gets <Name>Profile.md instead.
    r = run(P.person_profile("create", name="Owner", kind="known",
                             edits=[{"section": "Snapshot", "text": "x", "source": "owner"}]))
    assert r.startswith("Confirmed") and "OwnerProfile.md" in r
    assert portrait.read_text(encoding="utf-8") == "## Situation\nThe real portrait.\n"
    assert run(P.person_profile("update", name="Owner", edits=[
        {"section": "Snapshot", "text": "y", "source": "owner"}])).startswith("Confirmed")
    assert portrait.read_text(encoding="utf-8") == "## Situation\nThe real portrait.\n"


def test_update_with_only_refused_edits_is_an_error_and_writes_nothing(vault):
    run(P.person_profile("create", name="Karim", kind="known"))
    path = vault / "people" / "Karim.md"
    before = path.read_text(encoding="utf-8")
    r = run(P.person_profile("update", name="Karim", edits=[{"section": "Snapshot", "text": "x"}]))
    assert r.startswith("Error:") and "no source" in r
    assert path.read_text(encoding="utf-8") == before


def test_an_obsidian_edit_during_the_update_is_kept(vault, monkeypatch):
    """A stale write is refused by the revision check; the edit is reapplied on fresh text."""
    run(P.person_profile("create", name="Karim", kind="known"))
    path = vault / "people" / "Karim.md"
    real_write = P._write
    calls = []

    async def racing_write(p, content, revision):
        if not calls:                       # Sam saves in Obsidian just before us
            path.write_text(path.read_text(encoding="utf-8").replace(
                "# Karim\n", "# Karim\nHand note.\n"), encoding="utf-8")
        calls.append(1)
        return await real_write(p, content, revision)

    monkeypatch.setattr(P, "_write", racing_write)
    r = run(P.person_profile("update", name="Karim", edits=[
        {"section": "Snapshot", "text": "Friend", "source": "owner"}]))
    assert r.startswith("Confirmed") and len(calls) == 2
    text = path.read_text(encoding="utf-8")
    assert "Hand note." in text and "Friend — Sam" in text


def test_missing_profile_and_bad_action(vault):
    assert run(P.person_profile("read", name="Nobody")).startswith("Error:")
    assert run(P.person_profile("gaps")).startswith("Error:")
    run(P.person_profile("create", name="Karim", kind="known"))
    assert run(P.person_profile("explode", name="Karim")).startswith("Error:")


def test_non_profile_notes_in_people_are_ignored(vault):
    (vault / "people" / "predictions.md").write_text("# Predictions\n", encoding="utf-8")
    assert run(P.person_profile("list")) == "No person profiles yet."


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_registered_dispatched_and_guarded():
    from backend.brain import engine
    from backend.tools.registry import TOOLS
    assert "person_profile" in {t["name"] for t in TOOLS}
    assert engine.TOOL_DISPATCH["person_profile"] is P.person_profile
    assert "person_profile" in engine._PERSISTENCE_TOOLS


def test_not_available_to_autonomous_background_work():
    """Filing a person is a judgment made in conversation, like saving a skill."""
    from backend.brain import background
    assert "person_profile" not in background._ALLOWED_TOOLS


@pytest.mark.parametrize("reply, flagged", [
    ("I've added that to Karim's profile.", True),
    ("Started a profile for Marcus Aurelius.", True),
    ("The profile has been updated.", True),
    ("I haven't added it to his profile yet.", False),
    ("Tell me more about him.", False),
])
def test_claimed_profile_saves_are_checked(reply, flagged):
    from backend.brain import engine
    problems = engine._unbacked_claims(reply, set(), 0)
    assert bool([p for p in problems if "person_profile" in p]) == flagged
    assert not [p for p in engine._unbacked_claims(reply, {"person_profile"}, 1)
                if "person_profile" in p]


def test_template_is_kept_out_of_the_semantic_index():
    import inspect
    from backend.brain import semindex
    assert 'not p.name.startswith("_")' in inspect.getsource(semindex._backfill_sync)


# ---------------------------------------------------------------------------
# Conversation: mentions, gap questions, the offer, declines
# ---------------------------------------------------------------------------

def make(name, kind="known", aliases=None, edits=None):
    r = run(P.person_profile("create", name=name, kind=kind, aliases=aliases or [],
                             edits=edits or []))
    assert r.startswith("Confirmed"), r


@pytest.fixture(autouse=True)
def _reset_offer_clock():
    P._last_offer.clear()
    yield
    P._last_offer.clear()


def test_a_named_person_is_matched_by_name_alias_and_unique_first_name(vault):
    make("Karim Benali", aliases=["KB"])
    for text in ("had dinner with Karim Benali", "karim benali called", "KB is back",
                 "Karim got the job"):
        assert [p.name for _, p in P.mentioned_profiles(text)] == ["Karim Benali"], text


def test_loose_matches_are_not_taken_for_people(vault):
    make("Will Turner", aliases=["Wi"])
    for text in ("I will call later", "wi-fi is down", "willpower"):
        assert P.mentioned_profiles(text) == [], text


def test_a_shared_first_name_needs_the_full_name(vault):
    make("Sara Lopez")
    make("Sara Kim")
    assert P.mentioned_profiles("Sara said hi") == []
    assert [p.name for _, p in P.mentioned_profiles("Sara Kim said hi")] == ["Sara Kim"]


def test_mentions_are_ordered_and_capped_at_two(vault):
    for n in ("Ana Silva", "Ben Ode", "Cy Twombly"):
        make(n)
    found = P.mentioned_profiles("Cy Twombly, then Ana Silva, then Ben Ode")
    assert [p.name for _, p in found] == ["Cy Twombly", "Ana Silva"]


def test_turn_block_loads_the_profile_and_one_gap_question(vault):
    make("Karim Benali", edits=[{"section": "Snapshot", "text": "Childhood friend",
                                 "source": "owner"}])
    block = P.turn_block("Karim is moving to Lyon", False, now=1000.0)
    assert "[Person profile: Karim Benali (known)" in block
    assert "Snapshot: Childhood friend — Sam" in block
    assert "A gap worth filling" in block and "at most one question" in block


def test_questions_are_spaced_out_and_not_repeated(vault):
    make("Karim Benali")
    first = P.turn_block("Karim called", False, now=1000.0)
    assert "A gap worth filling" in first
    assert "A gap worth filling" not in P.turn_block("Karim again", False, now=1100.0)
    later = P.turn_block("Karim again", False, now=1000.0 + P._QUESTION_GAP_S)
    asked = [b.split(" -- e.g.")[0] for b in (first, later)]
    assert "A gap worth filling" in later and asked[0] != asked[1]


def test_historical_figures_get_their_profile_without_an_interview(vault):
    make("Marcus Aurelius", kind="historical")
    block = P.turn_block("reading Marcus Aurelius tonight", False, now=1000.0)
    assert "Person profile: Marcus Aurelius" in block
    # Research answers the rest; only what Sam alone can say is asked.
    assert "A gap worth filling, only if the moment is right: Why they matter to Sam" in block
    assert "Snapshot -- e.g." not in block


def test_the_offer_appears_for_someone_he_knows_without_a_profile(vault):
    block = P.turn_block("my sister just got engaged", True, channel="discord", now=1000.0)
    assert "offer once" in block and "person_profile(action='decline')" in block


def test_no_offer_without_the_signal(vault):
    assert P.turn_block("what's the weather", False, now=1000.0) == ""


def test_the_offer_is_rate_limited_per_channel(vault):
    assert P.turn_block("my brother called", True, channel="discord", now=1000.0)
    assert P.turn_block("my brother called", True, channel="discord", now=1500.0) == ""
    assert P.turn_block("my brother called", True, channel="local", now=1500.0)
    assert P.turn_block("my brother", True, channel="discord", now=1000.0 + P._OFFER_GAP_S)


def test_decline_is_remembered_and_listed(vault):
    r = run(P.person_profile("decline", name="Nadia"))
    assert r.startswith("Confirmed")
    assert "Nadia" in P.turn_block("my colleague Nadia", True, now=1000.0)
    assert "not to profile: Nadia" in run(P.person_profile("list"))


def test_creating_a_declined_person_clears_the_decline(vault):
    run(P.person_profile("decline", name="Nadia"))
    assert P.store.declined() == ["Nadia"]
    make("Nadia")
    assert P.store.declined() == []


@pytest.mark.parametrize("text, hit", [
    ("my friend Karim got married", True),
    ("My best friend is moving", True),
    ("my old boss called", True),
    ("my mom says hi", True),
    ("I finished my exam", False),
    ("my exercise routine", False),
    ("the friend zone", False),
])
def test_relation_keyword_fallback(text, hit):
    assert P.is_known_person_mention(text) is hit


def test_turn_block_never_raises(vault, monkeypatch):
    monkeypatch.setattr(P, "mentioned_profiles", lambda t: 1 / 0)
    assert P.turn_block("Karim", True, now=1.0) == ""


def test_chat_wires_the_people_block():
    import inspect
    from backend.brain import engine
    src = inspect.getsource(engine.chat)
    assert "people.turn_block" in src and 'signals.get("known_person")' in src


def test_jev_asks_about_known_people():
    from backend.brain import turn_signals as ts
    assert "known_person" in ts.QUESTIONS and "known_person" in ts.THRESHOLDS
    # measured on 336 real messages: Jev at 0.65 OR the relation words (see turn_signals)
    assert "known_person" in ts.KEYWORD_OR


def test_a_very_short_first_name_alone_is_not_a_match(vault):
    """'An Nguyen' must not be loaded for "An hour later"; the full name still works."""
    make("An Nguyen")
    assert P.mentioned_profiles("An hour later we left") == []
    assert [p.name for _, p in P.mentioned_profiles("An Nguyen called")] == ["An Nguyen"]


def test_the_model_cannot_write_people_notes_with_vault_write():
    """Live (2026-09-26): told "my sister Lina just got engaged", the model offered a
    profile but had already written a free-form people/Lina.md with vault_write, before
    he said yes. people/ is not a vault_write folder any more; profiles go through
    person_profile. Validation rejects it before dispatch, from the schema itself."""
    from backend.tools.registry import TOOLS
    from backend.tools.validation import validate
    schema = next(t for t in TOOLS if t["name"] == "vault_write")["input_schema"]
    assert "people" not in schema["properties"]["folder"]["enum"]
    err = validate("vault_write", {"filename": "Lina.md", "content": "x", "folder": "people"})
    assert err and "folder" in err
    from backend.brain.people import _offer_block
    assert "Write nothing about them anywhere until he says yes" in _offer_block()


def test_create_names_the_sections_it_can_fill(vault):
    """Live: the model guessed "Who they are" and "Profile" and lost a fact he stated,
    because nothing told it the template's section names."""
    r = run(P.person_profile("create", name="Samir", kind="known", relation="cousin"))
    assert r.startswith("Confirmed")
    assert "Sections for a known person:" in r and "Shared history" in r
    assert "Era and context" not in r             # historical/public only


def test_a_wrong_section_name_is_answered_with_the_real_ones(vault):
    run(P.person_profile("create", name="Samir", kind="known"))
    r = run(P.person_profile("update", name="Samir", edits=[
        {"section": "Who they are", "text": "Sam's cousin", "source": "owner"}]))
    assert r.startswith("Error:") and "Sections for a known person:" in r
    ok = run(P.person_profile("update", name="Samir", edits=[
        {"section": "Shared history", "text": "Got Sam into programming at 12",
         "source": "owner"}]))
    assert ok.startswith("Confirmed") and "Sections for" not in ok   # only when it helps


def test_the_offer_names_who_is_already_profiled(vault):
    run(P.person_profile("create", name="Samir", kind="known"))
    run(P.person_profile("create", name="Ada Lovelace", kind="historical"))
    block = P.turn_block("honestly I think he'll stay in Canada", True, channel="t", now=5.0)
    assert "Already profiled (not them): Samir." in block
    assert "Ada Lovelace" not in block


def test_a_bare_first_name_only_matches_someone_he_knows(vault):
    make("Marcus Aurelius", kind="historical")
    make("Samir Haddad")
    assert P.mentioned_profiles("Marcus Rashford scored twice") == []
    assert [p.name for _, p in P.mentioned_profiles("marcus aurelius wrote it")] == ["Marcus Aurelius"]
    assert [p.name for _, p in P.mentioned_profiles("Samir called")] == ["Samir Haddad"]


def test_a_name_inside_a_file_name_or_address_is_not_a_mention(vault):
    """The only two false loads on 336 real messages: sam.md and his email address."""
    make("Sam")
    for text in ("update the sam.md portrait", "my email: sam.rivera@example.com",
                 "write to karim@example.com"):
        assert P.mentioned_profiles(text) == [], text
    assert P.mentioned_profiles("Sam. That's me.")        # end of sentence still counts
    assert P.mentioned_profiles("ask Sam, he knows")


OBSIDIAN_EDITED = """---
type: person-profile
name: Karim
kind: known
relation: Best friend
aliases:
  - Kiki
  - K
created: 2026-09-20
updated: 2026-09-20
tags:
  - person
  - family-friend
birthday: 1998-04-02
---
# Karim

Some prose Sam wrote at the top. See [[Lyon]].

## Current situation
- Opened his own bakery in Lyon. — Sam, 2026-09-20

## My own notes
Hand-written section, with a [[Sofia]] link.

---
*Related: [[Sam]] · [[Sofia]]*
"""


def test_obsidian_properties_edits_survive_an_update(vault):
    """Obsidian's Properties editor rewrites lists as YAML blocks. Before, the aliases
    were lost (so "Kiki" no longer loaded Karim) and the next update wrote invalid
    frontmatter; his own tag was reset to [person]."""
    (vault / "people" / "Karim.md").write_text(OBSIDIAN_EDITED, encoding="utf-8")
    p = P.parse_profile(OBSIDIAN_EDITED)
    assert p.aliases == ["Kiki", "K"] and p.tags == ["family-friend"]
    assert [x.name for _, x in P.mentioned_profiles("Kiki texted me")] == ["Karim"]
    r = run(P.person_profile("update", name="Karim", edits=[
        {"section": "Values and beliefs", "text": "Hates waste", "source": "owner"}]))
    assert r.startswith("Confirmed"), r
    text = (vault / "people" / "Karim.md").read_text(encoding="utf-8")
    meta = text.split("---")[1]
    assert "aliases: [Kiki, K]" in meta and "tags: [person, family-friend]" in meta
    assert "birthday: 1998-04-02" in meta and "  - " not in meta      # valid, no orphans
    for kept in ("Some prose Sam wrote at the top. See [[Lyon]].", "## My own notes",
                 "Hand-written section, with a [[Sofia]] link.", "[[Sofia]]*", "Hates waste"):
        assert kept in text, kept


def test_the_turn_block_names_sources_without_urls_or_dates(vault):
    make("Marcus Aurelius", kind="historical", edits=[
        {"section": "Snapshot", "text": "Roman emperor and Stoic",
         "source": "https://en.wikipedia.org/wiki/Marcus_Aurelius"}])
    make("Karim", edits=[{"section": "Shared history", "text": "Met at uni", "source": "owner"},
                         {"section": "Personality and temperament", "text": "Probably shy",
                          "source": "inference"}])
    block = P.turn_block("Marcus Aurelius and Karim", False, now=1.0)
    assert "Roman emperor and Stoic — en.wikipedia.org" in block and "https://" not in block
    assert "Met at uni — Sam" in block and "Probably shy — my inference" in block
    assert TODAY not in block
    # the file itself keeps the full source
    assert "(https://en.wikipedia.org/wiki/Marcus_Aurelius)" in (
        vault / "people" / "MarcusAurelius.md").read_text(encoding="utf-8")


def test_an_at_mention_still_counts(vault):
    make("Karim")
    assert [p.name for _, p in P.mentioned_profiles("@Karim says hi")] == ["Karim"]


def test_a_second_wording_of_the_same_fact_is_not_added(vault):
    make("Karim", edits=[{"section": "Relationships", "text": "Has a girlfriend named Sofia, a pastry chef.",
                          "source": "owner"}])
    r = run(P.person_profile("update", name="Karim", edits=[
        {"section": "Relationships", "text": "Girlfriend is Sofia, a pastry chef.", "source": "owner"}]))
    assert r.startswith("Error: nothing changed") and "already there" in r
    ok = run(P.person_profile("update", name="Karim", edits=[
        {"section": "Relationships", "text": "His brother Yanis lives in Lille.", "source": "owner"}]))
    assert ok.startswith("Confirmed")


def test_distinct_facts_stay_below_the_duplicate_threshold():
    """The closest distinct pair measured on real profiles (0.30)."""
    a = "From ages 15 to 20, Huang worked the graveyard shift at a Denny's restaurant as a dishwasher"
    b = "Huang's LinkedIn listing of his Denny's jobs as waiter, dishwasher and busboy from 1978"
    assert P._overlap(a, b) < P._DUPLICATE_OVERLAP


def test_a_pronoun_carries_over_the_person_named_just_before(vault):
    make("Samir Haddad")
    prev = ["my cousin Samir just moved to Montreal", "he loves it so far"]
    block = P.turn_block("honestly I think he'll end up staying in Canada", True,
                         channel="c", now=5.0, recent=prev)
    assert block.startswith("[Person profile: Samir Haddad") and "offer once" not in block
    # not when he is not talking about anyone, and not from further back than two messages
    assert P.turn_block("what's the weather", False, channel="c2", now=5.0, recent=prev) == ""
    far = ["Samir called", "unrelated", "also unrelated"]
    assert "Person profile: Samir" not in P.turn_block("he said hi", True, channel="c3", now=5.0, recent=far)


def test_a_new_person_can_be_offered_within_the_half_hour(vault):
    """Live: the aunt named 8 messages into a family conversation could never be offered."""
    first = P.turn_block("my dad Rachid and my brother Adam argued again", True, "d", now=1000.0)
    assert "offer once" in first
    assert P.turn_block("dad thinks Adam is naive", True, "d", now=1100.0) == ""        # nobody new
    assert P.turn_block("my mom hates conflict", True, "d", now=1200.0) == ""           # nobody named
    assert "offer once" in P.turn_block("my aunt Nora called me after", True, "d", now=1300.0)
    # the cap: no third offer in the window, even for someone new
    assert P.turn_block("my cousin Lea texted too", True, "d", now=1400.0) == ""
    assert "offer once" in P.turn_block("my cousin Lea texted", True, "d", now=1000.0 + P._OFFER_WINDOW_S)


def test_a_profiled_or_declined_name_is_not_someone_new(vault):
    make("Samir Haddad")
    run(P.person_profile("decline", name="Lina"))
    assert "offer once" in P.turn_block("my brother called", True, "e", now=1000.0)
    assert P.turn_block("my sister Lina came by", True, "e", now=1100.0) == ""       # declined
    assert P.turn_block("Samir and I talked, he says hi", True, "e", now=1200.0).startswith(
        "[Person profile: Samir")                                                  # profiled: loads


def test_names_in_takes_only_people_introduced_by_relation():
    assert P.names_in("my dad Rachid and my brother Adam argued") == {"Rachid", "Adam"}
    assert P.names_in("my aunt Nora called me after") == {"Nora"}
    assert P.names_in("my best friend Karim's bakery") == {"Karim"}
    # a place or a name with no relation is not someone new (live: "Berlin")
    assert P.names_in("Adam is moving to Berlin next month") == frozenset()
    assert P.names_in("my sister just got engaged") == frozenset()


def test_a_dash_inside_an_entry_does_not_become_a_second_source(vault):
    make("Karim", edits=[{"section": "Things to remember", "text": "Allergic to hazelnuts — funny for a baker",
                          "source": "owner"}])
    text = (vault / "people" / "Karim.md").read_text(encoding="utf-8")
    assert "- Allergic to hazelnuts - funny for a baker — Sam, 2026-09-26" in text


def test_an_unedited_old_template_is_upgraded_and_an_edited_one_kept(vault, monkeypatch):
    import hashlib
    old = "---\ntype: person-profile-template\n---\n## Snapshot\n<!--\napplies: all\n-->\n"
    monkeypatch.setattr(P, "_OLD_DEFAULTS", frozenset({hashlib.sha256(old.encode()).hexdigest()}))
    path = vault / "people" / P.TEMPLATE_FILE
    path.write_text(old, encoding="utf-8")
    P.load_template()
    assert path.read_text(encoding="utf-8") == P.DEFAULT_TEMPLATE
    edited = old + "\n## My section\n<!--\napplies: known\n-->\n"
    path.write_text(edited, encoding="utf-8")
    assert [s.title for s in P.load_template()] == ["Snapshot", "My section"]
    assert path.read_text(encoding="utf-8") == edited


def test_the_current_default_is_not_listed_as_old():
    assert P._text_hash(P.DEFAULT_TEMPLATE) not in P._OLD_DEFAULTS


def test_an_attribution_typed_into_the_text_is_not_doubled(vault):
    make("Karim", edits=[{"section": "Current situation",
                          "text": "First week at the bakery was a good one. — Sam, 2026-09-27",
                          "source": "owner"}])
    text = (vault / "people" / "Karim.md").read_text(encoding="utf-8")
    assert "- First week at the bakery was a good one. — Sam, 2026-09-26" in text
    assert text.count("Sam, 2026") == 1

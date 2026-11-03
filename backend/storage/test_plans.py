"""P3: the plan record (backend/storage/plans.py) -- versions, blocks and their event trail."""
import ast
import re
from pathlib import Path

import pytest

from backend.storage import db, plans

WEEK = "2026-10-05"
H = 3600.0
T0 = 1_790_000_000.0            # an arbitrary UTC instant


def blocks(*spec):
    """(kind, hours-after-T0, hours-long) -> block dicts."""
    return [{"kind": k, "start": T0 + s * H, "end": T0 + (s + n) * H} for k, s, n in spec]


def make(spec=(("gym", 0, 1), ("work", 2, 4)), *, rules=None, by="owner", note="", parent=None, now=T0):
    return plans.create_version(WEEK, rules or {"work": {"days": "mon-fri"}}, blocks(*spec),
                                created_by=by, note=note, parent_id=parent, now=now)


# --- versions ------------------------------------------------------------------------------

def test_a_first_version_is_a_draft_with_all_its_blocks():
    pid = make()
    plan = plans.get(pid)
    assert (plan["week_start"], plan["version"], plan["state"]) == (WEEK, 1, plans.DRAFT)
    assert plan["created_by"] == "owner" and plan["parent_id"] is None
    assert [(b["kind"], b["status"]) for b in plan["blocks"]] == [("gym", "planned"), ("work", "planned")]
    assert plan["rules"] == {"work": {"days": "mon-fri"}}
    assert all(b["uid"] and b["event_id"] is None for b in plan["blocks"])


def test_versions_count_up_per_week_and_weeks_do_not_share_a_count():
    a, b = make(), make()
    other = plans.create_version("2026-10-12", {}, blocks(("gym", 0, 1)), created_by="owner", now=T0)
    assert [plans.get(i)["version"] for i in (a, b, other)] == [1, 2, 1]


def test_a_new_draft_discards_the_older_unapplied_one_but_not_the_applied_plan():
    first = make()
    plans.mark_applied(first)
    second = make()
    third = make()
    states = {v["id"]: v["state"] for v in plans.versions(WEEK)}
    assert states == {first: plans.APPLIED, second: plans.DISCARDED, third: plans.DRAFT}
    assert plans.applied(WEEK)["id"] == first and plans.draft(WEEK)["id"] == third
    assert plans.current(WEEK)["id"] == first, "the plan the week stands on is the applied one"


def test_current_falls_back_to_the_proposal_and_is_none_for_an_empty_week():
    assert plans.current(WEEK) is None
    pid = make()
    assert plans.current(WEEK)["id"] == pid


def test_applying_a_version_supersedes_the_previous_one_and_discards_other_drafts():
    v1 = make()
    plans.mark_applied(v1, now=T0 + 10)
    v2 = make(parent=v1)
    v3 = make(parent=v1)                       # discards v2
    assert plans.mark_applied(v3, now=T0 + 20)
    rows = {v["id"]: v for v in plans.versions(WEEK)}
    assert rows[v1]["state"] == plans.SUPERSEDED and rows[v1]["superseded_at"] == T0 + 20
    assert rows[v2]["state"] == plans.DISCARDED
    assert rows[v3]["state"] == plans.APPLIED and rows[v3]["applied_at"] == T0 + 20


def test_a_discarded_or_superseded_version_cannot_be_applied():
    v1 = make()
    v2 = make()                                # v1 discarded
    assert plans.mark_applied(v1) is False
    plans.mark_applied(v2)
    v3 = make()
    plans.mark_applied(v3)                     # v2 superseded
    assert plans.mark_applied(v2) is False
    assert plans.applied(WEEK)["id"] == v3


def test_applying_twice_keeps_the_first_applied_time():
    v = make()
    plans.mark_applied(v, now=T0 + 5)
    assert plans.mark_applied(v, now=T0 + 99)
    assert plans.get(v)["applied_at"] == T0 + 5


def test_discard_only_touches_a_draft():
    v = make()
    assert plans.discard(v) is True and plans.get(v)["state"] == plans.DISCARDED
    w = make()
    plans.mark_applied(w)
    assert plans.discard(w) is False and plans.get(w)["state"] == plans.APPLIED


def test_undone_is_set_once():
    a, b, c = make(), make(), make()
    assert plans.mark_undone(a, c) is True
    assert plans.mark_undone(a, b) is False
    assert plans.get(a)["undone_by"] == c


def test_weeks_with_applied_plans():
    plans.mark_applied(make())
    other = plans.create_version("2026-10-12", {}, blocks(("gym", 0, 1)), created_by="owner", now=T0)
    plans.create_version("2026-10-19", {}, blocks(("gym", 0, 1)), created_by="owner", now=T0)
    plans.mark_applied(other)
    assert plans.weeks_with_applied_plans() == [WEEK, "2026-10-12"]
    assert plans.weeks_with_applied_plans("2026-10-12") == ["2026-10-12"]


# --- blocks --------------------------------------------------------------------------------

def test_blocks_carry_their_uid_and_state_across_versions():
    v1 = make()
    first = plans.get(v1)["blocks"]
    carried = [{"uid": b["uid"], "kind": b["kind"], "start": b["start_at"], "end": b["end_at"],
                "event_id": "ev" + b["uid"], "status": plans.SCHEDULED} for b in first]
    v2 = plans.create_version(WEEK, {}, carried, created_by="owner", parent_id=v1, now=T0 + 1)
    second = plans.get(v2)["blocks"]
    assert [b["uid"] for b in second] == [b["uid"] for b in first]
    assert all(b["event_id"] == "ev" + b["uid"] and b["status"] == "scheduled" for b in second)


def test_only_the_parts_that_change_after_planning_can_be_updated():
    pid = make()
    uid = plans.get(pid)["blocks"][0]["uid"]
    assert plans.set_block(pid, uid, event_id="abc12", status=plans.SCHEDULED)
    assert plans.set_block(pid, uid, live_start=T0 + 5, live_end=T0 + 6, status=plans.MOVED)
    row = plans.blocks_of(pid)[0]
    assert (row["event_id"], row["status"], row["live_start"]) == ("abc12", "moved", T0 + 5)
    assert (row["start_at"], row["end_at"]) == (T0, T0 + H), "what was planned never changes"
    with pytest.raises(ValueError, match="start_at"):
        plans.set_block(pid, uid, start_at=T0 + 9)
    with pytest.raises(ValueError):
        plans.set_block(pid, uid, kind="run")
    assert plans.blocks_of(pid)[0]["start_at"] == T0


def test_updating_a_block_of_another_version_does_not_touch_this_one():
    v1 = make()
    uid = plans.get(v1)["blocks"][0]["uid"]
    v2 = plans.create_version(WEEK, {}, [{"uid": uid, "kind": "gym", "start": T0, "end": T0 + H}],
                              created_by="owner", now=T0)
    plans.set_block(v2, uid, status=plans.SCHEDULED)
    assert plans.blocks_of(v1)[0]["status"] == "planned"


def test_a_block_row_has_its_paris_date():
    # 2026-10-05 23:30 UTC is already the 6th in Paris (UTC+2)
    start = 1_791_243_000.0
    pid = plans.create_version(WEEK, {}, [{"kind": "gym", "start": start, "end": start + H}],
                               created_by="owner", now=start)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    expect = datetime.fromtimestamp(start, ZoneInfo("Europe/Paris")).date().isoformat()
    assert plans.blocks_of(pid)[0]["local_date"] == expect
    assert plans.get(pid)["local_date"] == expect


# --- events --------------------------------------------------------------------------------

def test_new_blocks_get_a_proposed_event_once_and_carried_blocks_do_not():
    v1 = make(note="week of the 5th")
    first = plans.get(v1)["blocks"]
    assert [(e["event"], e["actor"], e["reason"]) for e in plans.events(plan_id=v1)] == \
        [("proposed", "owner", "week of the 5th")] * 2
    carried = [{"uid": b["uid"], "kind": b["kind"], "start": b["start_at"], "end": b["end_at"]}
               for b in first] + [{"kind": "run", "start": T0 + 20 * H, "end": T0 + 21 * H}]
    v2 = plans.create_version(WEEK, {}, carried, created_by="owner", parent_id=v1, now=T0 + 1)
    assert len(plans.events(plan_id=v2)) == 1, "only the new block is proposed"
    assert len(plans.events(week_start=WEEK)) == 3


def test_events_are_appended_in_order_and_filterable():
    pid = make()
    uid = plans.get(pid)["blocks"][0]["uid"]
    plans.add_event(uid, pid, plans.CREATED, "jarvis", new=(T0, T0 + H), now=T0 + 5)
    plans.add_event(uid, pid, plans.SEEN_MOVED, "owner", old=(T0, T0 + H), new=(T0 + 3 * H, T0 + 4 * H),
                    reason="  moved   in the calendar ", now=T0 + 9)
    trail = plans.events(block_uid=uid)
    assert [e["event"] for e in trail] == ["proposed", "created", "seen_moved"]
    moved = trail[-1]
    assert (moved["actor"], moved["old_start"], moved["new_start"]) == ("owner", T0, T0 + 3 * H)
    assert moved["reason"] == "moved in the calendar", "short text, whitespace collapsed"
    assert [e["event"] for e in plans.events(plan_id=pid)] == ["proposed", "proposed", "created", "seen_moved"]
    assert plans.events(block_uid="nobody") == []


def test_an_unknown_actor_is_refused():
    pid = make()
    with pytest.raises(ValueError):
        plans.add_event("x", pid, plans.CREATED, "the intern")
    with pytest.raises(ValueError):
        plans.create_version(WEEK, {}, blocks(("gym", 0, 1)), created_by="the intern")


def test_generation_counts_the_events_written_for_a_block():
    pid = make()
    uid = plans.get(pid)["blocks"][0]["uid"]
    assert plans.generation(uid) == 0
    plans.add_event(uid, pid, plans.CREATED, "jarvis")
    plans.add_event(uid, pid, plans.WRITE_FAILED, "jarvis")
    assert plans.generation(uid) == 1, "a failed write does not use up an id"
    plans.add_event(uid, pid, plans.REMOVED, "jarvis")
    plans.add_event(uid, pid, plans.RESTORED, "jarvis")
    assert plans.generation(uid) == 2


# --- when storage is not there ---------------------------------------------------------------

def test_without_a_database_everything_degrades_quietly(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert plans.create_version(WEEK, {}, blocks(("gym", 0, 1)), created_by="owner") is None
    assert plans.get(1) is None and plans.blocks_of(1) == [] and plans.versions(WEEK) == []
    assert plans.current(WEEK) is None and plans.mark_applied(1) is False
    assert plans.set_block(1, "x", status="planned") is False
    assert plans.add_event("x", 1, plans.CREATED, "jarvis") is None
    assert plans.events() == [] and plans.generation("x") == 0
    assert plans.weeks_with_applied_plans() == [] and plans.mark_undone(1, 2) is False


def test_a_failed_write_records_nothing_at_all(monkeypatch):
    """One transaction: a block that cannot be stored must not leave half a version behind."""
    bad = blocks(("gym", 0, 1)) + [{"kind": "run", "start": "not a time", "end": 1.0}]
    assert plans.create_version(WEEK, {}, bad, created_by="owner") is None
    assert plans.versions(WEEK) == []
    assert plans.events(week_start=WEEK) == []


# --- structure -----------------------------------------------------------------------------

BACKEND = Path(__file__).resolve().parents[1]
_HISTORY_WRITE = re.compile(r"\b(UPDATE\s+block_events|DELETE\s+FROM\s+block_events|"
                            r"INSERT\s+OR\s+REPLACE\s+INTO\s+block_events|REPLACE\s+INTO\s+block_events)\b",
                            re.IGNORECASE)


def _string_constants(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_nothing_in_the_backend_updates_or_deletes_block_events():
    """`block_events` is the history: facts are appended, never rewritten (life-log rule)."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if path.name.startswith("test_") or ".venv" in path.parts:
            continue
        for text in _string_constants(path):
            if _HISTORY_WRITE.search(text):
                offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_the_history_guard_is_not_vacuous(tmp_path):
    planted = tmp_path / "bad.py"
    planted.write_text('def f(conn):\n    conn.execute("UPDATE block_events SET reason = 1")\n'
                       '    conn.execute("  delete   from block_events")\n', encoding="utf-8")
    hits = [t for t in _string_constants(planted) if _HISTORY_WRITE.search(t)]
    assert len(hits) == 2


# --- the proposal that is waiting ------------------------------------------------------------

def test_newest_draft_is_the_latest_waiting_proposal_across_weeks():
    make()
    later = plans.create_version("2026-10-12", {}, blocks(("gym", 0, 1)), created_by="owner", now=T0 + 50)
    assert plans.newest_draft("2026-09-28")["id"] == later


def test_newest_draft_ignores_applied_replaced_and_discarded_versions():
    v1 = make()
    plans.mark_applied(v1)
    assert plans.newest_draft("2026-09-28") is None
    v2 = make(parent=v1)
    v3 = make(parent=v1)                                    # discards v2
    assert plans.newest_draft("2026-09-28")["id"] == v3
    plans.discard(v3)
    assert plans.newest_draft("2026-09-28") is None


def test_newest_draft_respects_the_first_week_and_the_age_limit():
    make(now=T0)
    assert plans.newest_draft("2026-10-12") is None, "a proposal for an earlier week is not asked about"
    assert plans.newest_draft("2026-09-28", since_ts=T0 + 1) is None, "too old"
    assert plans.newest_draft("2026-09-28", since_ts=T0) is not None


def test_newest_draft_carries_its_blocks():
    pid = make()
    draft = plans.newest_draft("2026-09-28")
    assert draft["id"] == pid and [b["kind"] for b in draft["blocks"]] == ["gym", "work"]


def test_latest_live_prefers_a_newer_proposal_over_the_applied_plan_it_builds_on():
    v1 = make()
    plans.mark_applied(v1)
    assert plans.latest_live(WEEK)["id"] == v1
    v2 = make(parent=v1)
    assert plans.latest_live(WEEK)["id"] == v2, "the proposal made on top of the plan is the one being worked on"
    plans.discard(v2)
    assert plans.latest_live(WEEK)["id"] == v1
    assert plans.latest_live("2030-01-07") is None


# --- P4: outcomes, settings and the two views --------------------------------------------------

def _applied(spec=(("gym", 0, 1), ("work", 2, 4)), **kw):
    pid = make(spec, **kw)
    plans.mark_applied(pid)
    return pid, {b["kind"]: b["uid"] for b in plans.blocks_of(pid)}


def test_an_outcome_is_appended_as_a_fact_and_the_latest_one_answers():
    pid, uid = _applied()
    assert plans.record_outcome(uid["gym"], pid, "partly", "checkin", reason="stopped early", now=T0 + 10)
    assert plans.record_outcome(uid["gym"], pid, "done", "checkin", reason="finished after all", now=T0 + 20)
    got = plans.outcomes([uid["gym"], uid["work"]])
    assert set(got) == {uid["gym"]}, "a block nobody reported on has no outcome"
    assert got[uid["gym"]] == {"outcome": "done", "reason": "finished after all", "at": T0 + 20, "actor": "checkin"}
    trail = [e["event"] for e in plans.events(block_uid=uid["gym"]) if e["event"].startswith("outcome_")]
    assert trail == ["outcome_partly", "outcome_done"], "history is kept, not rewritten"


def test_an_unknown_outcome_word_or_actor_is_refused():
    pid, uid = _applied()
    with pytest.raises(ValueError):
        plans.record_outcome(uid["gym"], pid, "finished", "checkin")
    with pytest.raises(ValueError):
        plans.record_outcome(uid["gym"], pid, "done", "somebody")


def test_outcomes_of_nothing_is_empty_and_never_raises():
    assert plans.outcomes([]) == {}
    assert plans.outcomes(["no-such-block"]) == {}


def test_an_outcome_word_never_collides_with_the_calendar_events():
    pid, uid = _applied()
    plans.add_event(uid["gym"], pid, plans.EVENT_MOVED, "jarvis")
    plans.record_outcome(uid["gym"], pid, "moved", "checkin", reason="did it tomorrow")
    assert plans.outcomes([uid["gym"]])[uid["gym"]]["outcome"] == "moved"
    assert plans.generation(uid["gym"]) == 0


def test_settings_round_trip_and_the_newest_value_replaces_the_old():
    assert plans.get_setting("auto_move") is None and plans.get_setting("auto_move", []) == []
    assert plans.set_setting("auto_move", ["gym"])
    assert plans.get_setting("auto_move") == ["gym"]
    assert plans.set_setting("auto_move", [])
    assert plans.get_setting("auto_move", ["x"]) == []
    plans.set_setting("checkin_time", "21:30")
    assert plans.get_setting("checkin_time") == "21:30"


def test_the_outcome_view_puts_planned_next_to_what_happened():
    pid, uid = _applied((("gym", 0, 1), ("gym", 26, 1), ("work", 2, 4)))
    gyms = [b for b in plans.blocks_of(pid) if b["kind"] == "gym"]
    plans.record_outcome(gyms[0]["uid"], pid, "done", "checkin", reason="good session", now=T0 + 100)
    rows = {r["uid"]: r for r in plans.block_outcomes(week_start=WEEK)}
    assert len(rows) == 3
    first = rows[gyms[0]["uid"]]
    assert (first["kind"], first["outcome"], first["reason"]) == ("gym", "done", "good session")
    assert first["planned_start"] == gyms[0]["start_at"] and first["week_start"] == WEEK
    assert first["removed"] == 0 and first["times_moved"] == 0
    assert rows[gyms[1]["uid"]]["outcome"] is None, "never asked is not the same as unknown"


def test_a_block_that_was_never_applied_is_not_in_the_view():
    make()                                                   # a draft only
    assert plans.block_outcomes() == []


def test_the_view_uses_the_last_version_that_held_the_block_and_counts_its_moves():
    pid, uid = _applied((("gym", 0, 1),))
    second = plans.create_version(WEEK, {"work": {"days": "mon-fri"}},
                                  [{"uid": uid["gym"], "kind": "gym", "start": T0 + 5 * H, "end": T0 + 6 * H}],
                                  created_by="jarvis", parent_id=pid, now=T0 + 50)
    plans.mark_applied(second)
    plans.add_event(uid["gym"], second, plans.EVENT_MOVED, "jarvis", now=T0 + 51)
    plans.add_event(uid["gym"], second, plans.SEEN_MOVED, "owner", now=T0 + 52)
    (row,) = plans.block_outcomes()
    assert row["planned_start"] == T0 + 5 * H, "the newest plan's time, not the first"
    assert row["times_moved"] == 2


def test_a_removed_block_is_flagged_and_a_restored_one_is_not():
    pid, uid = _applied((("gym", 0, 1), ("gym", 26, 1)))
    a, b = [x["uid"] for x in plans.blocks_of(pid)]
    plans.add_event(a, pid, plans.CREATED, "jarvis", now=T0 + 1)
    plans.add_event(a, pid, plans.REMOVED, "jarvis", now=T0 + 2)
    plans.add_event(b, pid, plans.CREATED, "jarvis", now=T0 + 1)
    plans.add_event(b, pid, plans.REMOVED, "jarvis", now=T0 + 2)
    plans.add_event(b, pid, plans.RESTORED, "jarvis", now=T0 + 3)
    rows = {r["uid"]: r for r in plans.block_outcomes()}
    assert rows[a]["removed"] == 1 and rows[b]["removed"] == 0


def test_adherence_counts_how_each_kind_ended_per_week_and_leaves_out_removed_blocks():
    pid, _ = _applied((("gym", 0, 1), ("gym", 26, 1), ("gym", 50, 1), ("gym", 74, 1), ("work", 2, 4)))
    gyms = [b["uid"] for b in plans.blocks_of(pid) if b["kind"] == "gym"]
    plans.record_outcome(gyms[0], pid, "done", "checkin", now=T0 + 100)
    plans.record_outcome(gyms[1], pid, "skipped", "checkin", now=T0 + 100)
    plans.record_outcome(gyms[2], pid, "unknown", "system", now=T0 + 100)
    plans.add_event(gyms[3], pid, plans.REMOVED, "jarvis", now=T0 + 101)
    rows = {r["kind"]: r for r in plans.adherence()}
    g = rows["gym"]
    assert (g["week_start"], g["planned"], g["done"], g["skipped"], g["unknown"], g["unreported"]) == \
           (WEEK, 3, 1, 1, 1, 0)
    assert rows["work"]["planned"] == 1 and rows["work"]["unreported"] == 1
    assert plans.adherence(since_week="2030-01-07") == []
    assert len(plans.adherence(until_week=WEEK)) == 2


def test_slips_are_the_skips_only_with_their_kind_date_and_reason():
    pid = make((("gym", 0, 1), ("gym", 26, 1), ("work", 2, 4)))
    plans.mark_applied(pid)
    gyms = [b["uid"] for b in plans.blocks_of(pid) if b["kind"] == "gym"]
    plans.record_outcome(gyms[0], pid, "skipped", "checkin", reason="tired", now=T0 + 100)
    plans.record_outcome(gyms[1], pid, "done", "checkin", now=T0 + 100)
    (s,) = plans.slips(since_date="2000-01-01")
    assert (s["uid"], s["kind"], s["reason"]) == (gyms[0], "gym", "tired") and s["date"]
    assert plans.slips(since_date="2100-01-01") == []

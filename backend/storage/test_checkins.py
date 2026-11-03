"""P4: the evening check-in record (backend/storage/checkins.py)."""
import ast
import re
from pathlib import Path

from backend.storage import checkins, db

DAY = "2026-10-06"
T0 = 1_790_000_000.0


def test_a_days_row_is_created_once():
    assert checkins.claim(DAY, ["u1", "u2"], now=T0) is True
    assert checkins.claim(DAY, ["u9"], now=T0 + 5) is False, "a second cycle or a restart cannot send it twice"
    row = checkins.get(DAY)
    assert row["block_uids"] == ["u1", "u2"] and row["created_at"] == T0
    assert row["sent_at"] is None and row["replied_at"] is None and row["closed_at"] is None


def test_sent_is_recorded_once():
    checkins.claim(DAY, [], now=T0)
    assert checkins.mark_sent(DAY, now=T0 + 1) is True
    assert checkins.mark_sent(DAY, now=T0 + 99) is False
    assert checkins.get(DAY)["sent_at"] == T0 + 1


def test_a_reply_keeps_his_words_and_what_was_made_of_them():
    checkins.claim(DAY, ["u1"], now=T0)
    parsed = [{"uid": "u1", "kind": "gym", "outcome": "done", "reason": ""}]
    assert checkins.record_reply(DAY, "gym done,   stopped work at 4", parsed, now=T0 + 60)
    row = checkins.get(DAY)
    assert row["reply"] == "gym done, stopped work at 4" and row["parsed"] == parsed
    assert row["replied_at"] == T0 + 60


def test_a_second_reply_is_appended_bounded_and_the_first_time_stands():
    checkins.claim(DAY, ["u1", "u2"], now=T0)
    checkins.record_reply(DAY, "gym done", [{"uid": "u1", "outcome": "done"}], now=T0 + 60)
    checkins.record_reply(DAY, "and I skipped the reading", [{"uid": "u2", "outcome": "skipped"}], now=T0 + 900)
    row = checkins.get(DAY)
    assert row["reply"] == "gym done | and I skipped the reading"
    assert row["replied_at"] == T0 + 60, "when he first answered"
    assert [p["uid"] for p in row["parsed"]] == ["u1", "u2"]
    checkins.record_reply(DAY, "x" * 3000, [], now=T0 + 1000)
    assert len(checkins.get(DAY)["reply"]) <= 800


def test_a_reply_for_a_day_with_no_check_in_is_refused():
    assert checkins.record_reply("2030-01-01", "gym done", [], now=T0) is False


def test_one_follow_up_question_a_day():
    checkins.claim(DAY, [], now=T0)
    assert checkins.note_followup(DAY, "What about the reading?", now=T0 + 10) is True
    assert checkins.note_followup(DAY, "And the walk?", now=T0 + 20) is False
    row = checkins.get(DAY)
    assert row["follow_up"] == "What about the reading?" and row["followup_at"] == T0 + 10


def test_closing_happens_once():
    checkins.claim(DAY, [], now=T0)
    assert checkins.close(DAY, now=T0 + 5) is True
    assert checkins.close(DAY, now=T0 + 9) is False
    assert checkins.get(DAY)["closed_at"] == T0 + 5


def test_waiting_is_the_newest_sent_and_still_open_check_in():
    assert checkins.waiting() is None
    checkins.claim("2026-10-05", ["a"], now=T0)
    checkins.mark_sent("2026-10-05", now=T0 + 1)
    checkins.claim(DAY, ["b"], now=T0 + 100)                       # claimed, not sent yet
    assert checkins.waiting()["local_date"] == "2026-10-05"
    checkins.mark_sent(DAY, now=T0 + 101)
    assert checkins.waiting()["local_date"] == DAY
    assert checkins.waiting(since_ts=T0 + 500) is None, "too old to be waiting for a reply"
    checkins.close(DAY, now=T0 + 200)
    assert checkins.waiting()["local_date"] == "2026-10-05"


def test_unclosed_lists_earlier_days_that_were_never_finished_off():
    checkins.claim("2026-10-04", [], now=T0)                       # claimed, never delivered
    checkins.claim("2026-10-05", [], now=T0)
    checkins.mark_sent("2026-10-05", now=T0)
    checkins.close("2026-10-05", now=T0 + 1)
    checkins.claim(DAY, [], now=T0)
    assert [r["local_date"] for r in checkins.unclosed(before_date=DAY)] == ["2026-10-04"]
    assert [r["local_date"] for r in checkins.unclosed(before_date="2026-10-04")] == []


def test_recent_lists_newest_first():
    for d in ("2026-10-03", "2026-10-05", "2026-10-04"):
        checkins.claim(d, [], now=T0)
    assert [r["local_date"] for r in checkins.recent(2)] == ["2026-10-05", "2026-10-04"]


def test_every_function_survives_an_unavailable_database(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: None)
    assert checkins.claim(DAY, [], now=T0) is False
    assert checkins.get(DAY) is None and checkins.waiting() is None
    assert checkins.mark_sent(DAY) is False and checkins.close(DAY) is False
    assert checkins.record_reply(DAY, "x", []) is False and checkins.note_followup(DAY, "q") is False
    assert checkins.unclosed(before_date=DAY) == [] and checkins.recent() == []


def test_a_check_in_row_is_never_deleted_and_never_rewritten_wholesale():
    """Structural: the module only ever INSERTs, and UPDATEs a field that is still empty (or the
    appended reply/parsed pair)."""
    src = Path(__file__).with_name("checkins.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    text = "\n".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert not re.search(r"\bDELETE\s+FROM\b", text, re.I)
    updates = re.findall(r"UPDATE checkins SET [^\n]*", text)
    assert len(updates) == 4, updates          # sent, reply, follow-up, closed
    for stmt in updates:
        assert "IS NULL" in stmt or "COALESCE" in stmt or "reply = ?" in stmt, stmt

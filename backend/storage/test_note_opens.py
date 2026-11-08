"""Which notes the phone opened, once per note per local day."""
from datetime import datetime
from backend import localtime

from backend.storage import note_opens, operations

ZONE = localtime.ZONE


def at(day, hour=10):
    return datetime(2026, 10, day, hour, tzinfo=ZONE).timestamp()


def rows():
    return operations._ensure().execute("SELECT folder, name, local_date, ts FROM note_opens").fetchall()


def test_two_opens_the_same_local_day_are_one_row_with_the_first_ts():
    note_opens.record("topics", "a.md", now=at(5, 9))
    note_opens.record("topics", "a.md", now=at(5, 20))
    assert [(r[2], r[3]) for r in rows()] == [("2026-10-05", at(5, 9))]


def test_the_next_day_is_a_second_row():
    note_opens.record("topics", "a.md", now=at(5))
    note_opens.record("topics", "a.md", now=at(6))
    assert len(rows()) == 2


def test_last_opened_is_latest_per_name_and_ignores_other_folders():
    note_opens.record("topics", "a.md", now=at(5))
    note_opens.record("topics", "a.md", now=at(7))
    note_opens.record("topics", "b.md", now=at(6))
    note_opens.record("letters", "c.md", now=at(8))
    assert note_opens.last_opened("topics") == {"a.md": at(7), "b.md": at(6)}


def test_first_opened_is_the_earliest_or_none():
    assert note_opens.first_opened() is None
    note_opens.record("topics", "a.md", now=at(7))
    note_opens.record("letters", "c.md", now=at(3))
    assert note_opens.first_opened() == at(3)


def test_unavailable_storage_is_silent(monkeypatch):
    monkeypatch.setattr(operations, "_ensure", lambda: None)
    note_opens.record("topics", "a.md")
    assert note_opens.last_opened("topics") == {}
    assert note_opens.first_opened() is None

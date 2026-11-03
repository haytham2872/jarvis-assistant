"""The life-log conventions (P0.4): a UTC timestamp and a Paris date on every row, short
text, and append-only event tables."""
from datetime import datetime, timezone

from backend.storage import lifelog


def _utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def test_the_paris_date_is_not_the_utc_date_around_midnight():
    """22:30 UTC on 29 September is 00:30 on the 30th in Paris: the day a "per day" query
    must count it under, whatever the server's clock says."""
    assert lifelog.local_date(_utc(2026, 9, 29, 22, 30)) == "2026-09-30"
    assert lifelog.local_date(_utc(2026, 9, 29, 21, 30)) == "2026-09-29"


def test_the_paris_date_follows_the_clock_change():
    """Winter: Paris is UTC+1, so the 30th begins at 23:00 UTC on the 29th."""
    assert lifelog.local_date(_utc(2026, 12, 15, 22, 59)) == "2026-12-15"
    assert lifelog.local_date(_utc(2026, 12, 15, 23, 0)) == "2026-12-16"


def test_stamp_returns_the_pair_every_row_carries():
    ts, day = lifelog.stamp(_utc(2026, 9, 29, 22, 30))
    assert (ts, day) == (_utc(2026, 9, 29, 22, 30), "2026-09-30")


def test_stamp_defaults_to_now():
    ts, day = lifelog.stamp()
    assert abs(ts - datetime.now(timezone.utc).timestamp()) < 5
    assert day == lifelog.local_date(ts)


def test_short_collapses_whitespace_and_bounds_length():
    assert lifelog.short("  gym  done,\n\nstopped work\tat 4 ") == "gym done, stopped work at 4"
    long = "word " * 200
    out = lifelog.short(long)
    assert len(out) <= lifelog.MAX_TEXT and out.endswith("…")
    assert lifelog.short(None) == "" and lifelog.short("") == ""


def test_short_respects_a_custom_limit():
    assert lifelog.short("abcdefghij", limit=5) == "abcd…"
    assert lifelog.short("abcde", limit=5) == "abcde"

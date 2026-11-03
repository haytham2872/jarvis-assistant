"""The process runs on Paris time, whatever the host says (P0 of the control roadmap).

The real effect needs `time.tzset()`, which Windows lacks; the desk machine is already on
Paris time. So the behaviour is tested with fakes everywhere, plus one subprocess test that
runs only where tzset exists (the Linux server, CI) and proves the real thing end to end.
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import localtime

ROOT = Path(__file__).resolve().parent.parent


def test_windows_is_left_alone(monkeypatch):
    """No tzset means no IANA-aware TZ: setting TZ=Europe/Paris there would turn local
    time into UTC, on the one machine that is already right."""
    monkeypatch.delattr(time, "tzset", raising=False)
    monkeypatch.setenv("TZ", "SOMETHING")
    assert localtime.apply() is False
    assert os.environ["TZ"] == "SOMETHING"


def test_tz_is_set_and_tzset_called(monkeypatch):
    calls = []
    monkeypatch.setattr(time, "tzset", lambda: calls.append(os.environ.get("TZ")), raising=False)
    monkeypatch.delenv("TZ", raising=False)
    localtime.apply()
    assert calls == ["Europe/Paris"]


class _Aware:
    def __init__(self, offset):
        self._offset = offset

    def utcoffset(self):
        return self._offset


class _Now:
    def __init__(self, local_offset, zone_offset):
        self._local, self._zone = local_offset, zone_offset

    def astimezone(self, tz=None):
        return _Aware(self._zone if tz is not None else self._local)


def _fake_datetime(local_offset, zone_offset):
    class _DT:
        @staticmethod
        def now():
            return _Now(local_offset, zone_offset)
    return _DT


def test_reports_success_only_when_local_time_really_is_paris(monkeypatch):
    monkeypatch.setattr(time, "tzset", lambda: None, raising=False)
    two = timedelta(hours=2)
    monkeypatch.setattr(localtime, "datetime", _fake_datetime(two, two))
    assert localtime.apply() is True


def test_a_host_without_zone_data_is_reported_not_trusted(monkeypatch, caplog):
    """tzset() does not fail on an unknown zone; it silently falls back to UTC. Without
    the comparison, a server missing tzdata would believe it had fixed the clock."""
    monkeypatch.setattr(time, "tzset", lambda: None, raising=False)
    monkeypatch.setattr(localtime, "datetime", _fake_datetime(timedelta(0), timedelta(hours=2)))
    with caplog.at_level("WARNING"):
        assert localtime.apply() is False
    assert "did not take effect" in caplog.text


def test_never_raises(monkeypatch):
    def boom():
        raise OSError("no zoneinfo")
    monkeypatch.setattr(time, "tzset", boom, raising=False)
    assert localtime.apply() is False


def test_importing_the_package_applies_it():
    """Every entry point imports `backend`, so this one line is what makes the fix hold
    for uvicorn, `python -m backend.tools.backup` and the terminal client alike."""
    source = (ROOT / "backend" / "__init__.py").read_text(encoding="utf-8")
    assert "localtime" in source and ".apply()" in source


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs a platform with time.tzset (Linux)")
def test_a_utc_process_reads_paris_time_after_importing_backend():
    """The 30 September finding, end to end: a process started on a UTC host reads Paris
    time after `import backend`, including the calendar date around midnight."""
    code = (
        "import backend, datetime; "
        "n = datetime.datetime.now().astimezone(); "
        "print(int(n.utcoffset().total_seconds()))"
    )
    env = dict(os.environ, TZ="UTC", PYTHONPATH=str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    from zoneinfo import ZoneInfo
    expected = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Paris")).utcoffset()
    assert int(out.stdout.strip()) == int(expected.total_seconds())


def _zone_in_a_fresh_process(value):
    code = "from backend import localtime; print(localtime.TIMEZONE, localtime.ZONE.key)"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    if value is None:
        env.pop("JARVIS_TIMEZONE", None)
    else:
        env["JARVIS_TIMEZONE"] = value
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, cwd=str(ROOT / "backend"))
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_the_owners_zone_comes_from_the_setting():
    assert _zone_in_a_fresh_process("America/New_York") == ["America/New_York", "America/New_York"]


def test_an_unknown_zone_falls_back_to_utc():
    assert _zone_in_a_fresh_process("Mars/Olympus") == ["UTC", "UTC"]

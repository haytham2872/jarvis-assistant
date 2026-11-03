"""Mobile view caches must not carry one test's calendar into another test's sources."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_overnight_sources(tmp_path, monkeypatch):
    """Hub parts now read the vault too: no mobile test may inherit a real night."""
    from backend.tools import vault
    from backend.brain import background, chronos, research
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(vault, "_VAULT", root)
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", root / "schedule.md")
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(research, "_QUEUE_FILE", root / "research-queue.md")
    monkeypatch.setattr(research, "_PACE_FILE", tmp_path / "research.json")
    monkeypatch.setattr(background, "_tasks", {})


@pytest.fixture(autouse=True)
def _isolated_calendar_cache():
    from backend.mobile import today
    today.clear_calendar_cache()
    yield
    today.clear_calendar_cache()

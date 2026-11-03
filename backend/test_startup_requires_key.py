"""A01: startup refuses to accept work without the provider's key.

The first real message used to discover a missing OPENROUTER_API_KEY, after startup had
reported healthy. lifespan() checks before anything else starts.
"""
import asyncio

import pytest

from backend import main
from backend.providers import gateway


def test_startup_stops_before_any_work_without_the_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    started = []
    monkeypatch.setattr(main._runs, "reconcile_interrupted", lambda *a, **k: started.append(1))

    async def enter():
        async with main.lifespan(main.app):
            pass

    with pytest.raises(gateway.ProviderNotConfigured, match="OPENROUTER_API_KEY"):
        asyncio.run(enter())
    assert started == []          # nothing after the check ran

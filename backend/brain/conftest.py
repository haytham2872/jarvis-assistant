"""Test rails for backend/brain.

`planday._pending_proposals` is a module-level list that `week_plan report` fills and the async
wrapper drains a moment later. A test that reports a skip through the synchronous `W.run` never
drains it, and the leftover made a later test see two proposals instead of one (found only in the
full run). Clear it around every test, and only if the module was imported at all.
"""
import sys

import pytest


def _clear():
    mod = sys.modules.get("backend.brain.planday")
    if mod is not None:
        mod._pending_proposals.clear()


@pytest.fixture(autouse=True)
def _no_leftover_reschedule_proposals():
    _clear()
    yield
    _clear()

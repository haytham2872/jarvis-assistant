"""Tests for backend/brain/self_model.py's live-reload loop.

Live incident (2026-08-28): the digest was only ever built once, at process
startup -- a dev session that edits CLAUDE.md while the backend keeps
running left JARVIS serving a self-model frozen at launch time, with no way
to notice new sections/tools added mid-session. run_loop()/_recheck_cycle()
put _warmup_worker() (already idempotent -- a hash match is a cheap no-op,
a hash mismatch does a real rebuild) on a periodic timer via pacing.py's
shared scaffold, instead of it only ever running once. No real CLAUDE.md
file or cache file is touched -- _warmup_worker itself is monkeypatched.
"""

import asyncio

import pytest

from backend.brain import self_model as sm


def run(coro):
    return asyncio.run(coro)


def test_recheck_cycle_calls_warmup_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(sm, "_warmup_worker", lambda: calls.append(1))
    run(sm._recheck_cycle())
    assert calls == [1]


def test_recheck_cycle_runs_in_executor_not_the_event_loop(monkeypatch):
    """_warmup_worker does blocking file I/O -- must not run directly on
    the event loop thread."""
    import threading
    seen_thread = {}

    def fake_warmup():
        seen_thread["name"] = threading.current_thread().name

    monkeypatch.setattr(sm, "_warmup_worker", fake_warmup)
    main_thread_name = threading.current_thread().name
    run(sm._recheck_cycle())
    assert seen_thread["name"] != main_thread_name


def test_run_loop_calls_recheck_cycle_repeatedly(monkeypatch):
    calls = []

    async def fake_sleep(_):
        pass

    async def fake_recheck():
        calls.append(1)
        if len(calls) >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sm, "_recheck_cycle", fake_recheck)

    with pytest.raises(asyncio.CancelledError):
        run(sm.run_loop())
    assert len(calls) == 3


def test_run_loop_uses_the_configured_interval(monkeypatch):
    seen = []

    async def fake_sleep(seconds):
        seen.append(seconds)
        if len(seen) >= 1:
            raise asyncio.CancelledError()

    async def fake_recheck():
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sm, "_recheck_cycle", fake_recheck)

    with pytest.raises(asyncio.CancelledError):
        run(sm.run_loop())
    assert seen == [sm._RECHECK_INTERVAL_S]


def test_recheck_cycle_does_not_swallow_warmup_worker_exceptions(monkeypatch):
    """_recheck_cycle() itself must let an exception from _warmup_worker
    propagate rather than catching it -- survival-past-a-bad-cycle is
    pacing.run_periodic's job (already covered generically by
    test_pacing.py::test_run_periodic_survives_ordinary_exception), not
    something _recheck_cycle should duplicate or, worse, mask by catching
    it silently before pacing.run_periodic ever sees it."""
    def broken_warmup():
        raise RuntimeError("CLAUDE.md temporarily locked by another process")

    monkeypatch.setattr(sm, "_warmup_worker", broken_warmup)
    with pytest.raises(RuntimeError, match="temporarily locked"):
        run(sm._recheck_cycle())


def test_run_loop_wires_recheck_cycle_as_the_on_cycle_callback(monkeypatch):
    """Confirms run_loop() actually hands pacing.run_periodic the real
    _recheck_cycle (not a look-alike), so pacing's own tested
    survive-a-bad-cycle contract genuinely applies here."""
    captured = {}

    async def fake_run_periodic(*, name, check_interval_s, on_cycle):
        captured["name"] = name
        captured["interval"] = check_interval_s
        captured["on_cycle"] = on_cycle

    monkeypatch.setattr(sm.pacing, "run_periodic", fake_run_periodic)
    run(sm.run_loop())

    assert captured["name"] == "self_model"
    assert captured["interval"] == sm._RECHECK_INTERVAL_S
    assert captured["on_cycle"] is sm._recheck_cycle


# ---------------------------------------------------------------------------
# _warmup_worker: cache-hit cheapness, and the evolution.md handover
#
# These used to assert that a rebuild wrote an entry to evolution.md.
# `backend/brain/evolution.py` owns that file now (git-derived, one entry per
# batch of commits), and the writer that lived here was deleted rather than
# left dormant -- so what is pinned below is the rebuild behaviour itself,
# plus a guard that this module stays out of evolution.md entirely.
# ---------------------------------------------------------------------------

def _rebuild_counter(monkeypatch, tmp_path):
    """Point self_model at a tmp CLAUDE.md and count real digest rebuilds."""
    claude_md = tmp_path / "CLAUDE.md"
    cache_file = tmp_path / ".self_model_cache.json"
    monkeypatch.setattr(sm, "_CLAUDE_MD", claude_md)
    monkeypatch.setattr(sm, "_CACHE_FILE", cache_file)
    monkeypatch.setattr(sm, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sm, "_digest", {})
    monkeypatch.setattr(sm, "_ready", False)

    builds = []
    real_build = sm._build_digest_from_claudemd

    def counted():
        builds.append(1)
        return real_build()

    monkeypatch.setattr(sm, "_build_digest_from_claudemd", counted)
    return claude_md, builds


def test_warmup_worker_is_a_cache_hit_when_claude_md_is_unchanged(monkeypatch, tmp_path):
    """The mechanism the periodic loop leans on: a tick with no CLAUDE.md
    change must not rebuild the digest. This is what makes a 5-minute
    re-check cheap rather than a repeated full parse."""
    claude_md, builds = _rebuild_counter(monkeypatch, tmp_path)
    claude_md.write_text("### Active personality and self-knowledge\nsome content\n", encoding="utf-8")

    sm._warmup_worker()
    assert sm.is_ready()
    assert len(builds) == 1, "the first call must actually build"

    sm._warmup_worker()   # the periodic loop's next tick, nothing changed
    assert len(builds) == 1, "an unchanged CLAUDE.md must not rebuild the digest"


def test_warmup_worker_rebuilds_on_a_real_claude_md_change(monkeypatch, tmp_path):
    """A mid-session CLAUDE.md edit is exactly what the live-reload loop
    exists to notice without a restart -- and the live digest must carry the
    new text afterwards, not just report that it rebuilt."""
    claude_md, builds = _rebuild_counter(monkeypatch, tmp_path)
    claude_md.write_text("### Active personality and self-knowledge\noriginal content\n", encoding="utf-8")

    sm._warmup_worker()
    assert len(builds) == 1
    assert "original content" in sm.get_section("personality")

    claude_md.write_text(
        "### Active personality and self-knowledge\nCOMPLETELY DIFFERENT content now\n", encoding="utf-8")
    sm._warmup_worker()

    assert len(builds) == 2, "a real CLAUDE.md change must trigger a real rebuild"
    section = sm.get_section("personality")
    assert "COMPLETELY DIFFERENT content now" in section
    assert "original content" not in section, "the stale digest survived the rebuild"


def test_warmup_worker_never_touches_the_evolution_log(monkeypatch, tmp_path):
    """The handover guard. evolution.py is the only writer now; a second one
    here would be the competing implementation A10 exists to prevent, and
    would interleave section-name diffs into a file of prose entries."""
    claude_md, _ = _rebuild_counter(monkeypatch, tmp_path)
    claude_md.write_text("### Active personality and self-knowledge\noriginal\n", encoding="utf-8")

    evo = tmp_path / "jarvis-vault" / "self" / "evolution.md"
    evo.parent.mkdir(parents=True, exist_ok=True)
    evo.write_text("# Evolution Log\n\n## 2026-09-22\nwritten by evolution.py\n",
                   encoding="utf-8")
    before = evo.read_text(encoding="utf-8")

    sm._warmup_worker()                                     # first build
    claude_md.write_text("### Active personality and self-knowledge\nchanged\n", encoding="utf-8")
    sm._warmup_worker()                                     # real change

    assert evo.read_text(encoding="utf-8") == before, (
        "self_model wrote to evolution.md, which evolution.py now owns")


def test_the_retired_evolution_writer_is_gone_not_dormant():
    """Deleted deliberately: a dormant writer is one wiring mistake away from
    two writers on one file, and it was the weaker source -- it only fired
    when a backend happened to be running at the moment CLAUDE.md changed."""
    assert not hasattr(sm, "_write_evolution_entry")
    assert not hasattr(sm, "_load_any_cache")


def test_evolution_module_is_the_writer():
    """Names the replacement, so this file points at its successor."""
    from backend.brain import evolution

    assert hasattr(evolution, "run_loop")
    assert hasattr(evolution, "run_catchup")

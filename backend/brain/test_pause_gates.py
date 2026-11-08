"""P2: what each part of JARVIS does while Sam has paused him.

The acceptance line from the roadmap: *every paused loop does nothing within one cycle
(tested per loop)*. That is `test_a_paused_loop_does_nothing_within_one_cycle`, run against
the real `run_loop` of research, rumination, watches and forecasts, plus the parts that are
not a plain loop: reminders and the brief and scheduled and night tasks (chronos), background
jobs, and profile research. The switch itself is test_pause.py.
"""
import ast
import asyncio
import datetime
import importlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.brain import background, chronos, pacing, pause, research
from backend.brain.chronos import _TZ
from backend.storage import pauses

BACKEND = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def paris(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=_TZ)


def skips():
    row = pauses.recent(1)[0]
    return [(s["kind"], s["label"]) for s in pauses.skips(row["id"])]


# ===========================================================================================
# The scaffold every paced loop shares
# ===========================================================================================

def _drive(monkeypatch, pausable, cycles=3):
    """Run run_periodic for `cycles` cycles with sleep faked; returns how many times the
    body ran."""
    ran, slept = [], []

    async def fake_sleep(_):
        slept.append(1)
        if len(slept) > cycles:
            raise asyncio.CancelledError()

    async def body():
        ran.append(1)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(pacing.run_periodic(name="t", check_interval_s=1, on_cycle=body, pausable=pausable))
    return len(ran)


def test_work_loops_skip_every_cycle_under_any_pause(monkeypatch):
    assert _drive(monkeypatch, "work") == 3
    pauses.start(pauses.WORK)
    assert _drive(monkeypatch, "work") == 0
    pauses.start(pauses.ALL)
    assert _drive(monkeypatch, "work") == 0


def test_an_all_loop_ignores_a_plain_pause_but_not_pause_all(monkeypatch):
    pauses.start(pauses.WORK)
    assert _drive(monkeypatch, "all") == 3
    pauses.start(pauses.ALL)
    assert _drive(monkeypatch, "all") == 0


def test_a_loop_that_is_not_pausable_never_stops(monkeypatch):
    pauses.start(pauses.ALL)
    assert _drive(monkeypatch, None) == 3


def test_a_loop_picks_up_again_the_cycle_after_resume(monkeypatch):
    seen = []

    async def fake_sleep(_):
        seen.append(len(seen))
        if len(seen) == 3:
            pauses.end("resume")                 # /resume arrives between cycles 2 and 3
        if len(seen) > 4:
            raise asyncio.CancelledError()

    ran = []

    async def body():
        ran.append(len(seen))

    pauses.start(pauses.WORK)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run(pacing.run_periodic(name="t", check_interval_s=1, on_cycle=body, pausable="work"))
    assert ran == [3, 4]


# ===========================================================================================
# The real loops
# ===========================================================================================

WORK_LOOPS = ["backend.brain.research", "backend.brain.rumination",
              "backend.brain.watches", "backend.brain.forecasts"]


@pytest.mark.parametrize("modname", WORK_LOOPS)
@pytest.mark.parametrize("scope", [pauses.WORK, pauses.ALL])
def test_a_paused_loop_does_nothing_within_one_cycle(modname, scope, monkeypatch, tmp_path):
    mod = importlib.import_module(modname)
    if modname.endswith("research"):
        monkeypatch.setattr(mod, "_PACE_FILE", tmp_path / "pace.json")
    cycles, slept = [], []

    async def spy():
        cycles.append(1)
    monkeypatch.setattr(mod, "_cycle", spy)

    async def fake_sleep(_):
        slept.append(1)
        if len(slept) > 3:
            raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    pauses.start(scope)
    with pytest.raises(asyncio.CancelledError):
        run(mod.run_loop())
    assert len(slept) == 4 and cycles == [], f"{modname} worked while paused"

    # the control: the same loop, resumed, cycles every time
    pauses.end("resume")
    slept.clear()
    with pytest.raises(asyncio.CancelledError):
        run(mod.run_loop())
    assert cycles == [1, 1, 1]


def _run_periodic_calls():
    """{relative path: [(name-or-None, pausable-or-None)]} for every call in the backend."""
    found = {}
    for path in BACKEND.rglob("*.py"):
        if path.name.startswith("test_") or path.name == "pacing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run_periodic"):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            name = kw["name"].value if isinstance(kw.get("name"), ast.Constant) else None
            pausable = kw["pausable"].value if "pausable" in kw else None
            found.setdefault(path.relative_to(BACKEND).as_posix(), []).append((name, pausable))
    return found


def test_every_paced_loop_has_been_classified():
    """A new loop has to decide: hold on /pause, or be housekeeping that never stops. This
    fails until it is written into one of the two lists, so a loop cannot be added that
    quietly ignores the off switch."""
    pausable = {"brain/research.py", "brain/rumination.py", "brain/watches.py",
                "brain/forecasts.py", "brain/skill_nightly.py",
                "brain/goal_runner.py"}   # G1a: no new goal session starts while paused
    # chronos checks the pause inside _cycle: reminders and the brief only stop for /pause all
    # planloop the same way: the evening check-in stops for /pause all, the calendar look and
    # the weekly review for any pause, and closing finished days never stops
    # portfolio the same way: a close missed during a pause is recorded as skipped and fetched
    # after /resume, so the loop must keep running to notice the resume
    custom = {"brain/chronos.py", "brain/planloop.py", "brain/portfolio.py"}
    housekeeping = {"brain/backup_loop.py", "brain/canary.py", "brain/evolution.py",
                    "brain/pause.py", "brain/self_model.py", "brain/semindex.py",
                    "brain/session_close.py", "tools/approval_flow.py"}
    found = _run_periodic_calls()
    assert set(found) == pausable | custom | housekeeping
    for path in pausable:
        assert [p for _, p in found[path]] == ["work"], f"{path} must pass pausable='work'"
    for path in custom | housekeeping:
        assert [p for _, p in found[path]] == [None], f"{path} must not be pausable"
    assert "pause.is_paused" in Path(BACKEND / "brain/chronos.py").read_text(encoding="utf-8")
    assert "pause.is_paused" in Path(BACKEND / "brain/planloop.py").read_text(encoding="utf-8")
    assert "pause.is_paused" in Path(BACKEND / "brain/portfolio.py").read_text(encoding="utf-8")


# ===========================================================================================
# Chronos: reminders, the brief, scheduled tasks, night tasks
# ===========================================================================================

@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(chronos, "_STATE_FILE", tmp_path / "chronos.json")
    monkeypatch.setattr(chronos, "_SCHEDULE_FILE", tmp_path / "schedule.md")
    monkeypatch.setattr(chronos, "_busy_check", None)
    return tmp_path


@pytest.fixture
def dms(monkeypatch):
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(chronos, "_notifier", notifier)
    return sent


@pytest.fixture
def clock(monkeypatch):
    now = {"t": paris(2026, 9, 30, 8, 5)}
    monkeypatch.setattr(chronos, "now_local", lambda: now["t"])
    return now


@pytest.fixture
def brief(monkeypatch):
    sent = []

    async def fake(now):
        sent.append(now)
        return True
    monkeypatch.setattr(chronos, "_send_brief", fake)
    return sent


def _reminder_due(message="call the bank"):
    chronos._write_schedule([chronos._format_reminder_line(paris(2026, 9, 30, 8, 0), "once",
                                                           message)])


def test_a_plain_pause_still_delivers_reminders_and_the_brief(files, dms, clock, brief):
    _reminder_due()
    pauses.start(pauses.WORK)
    run(chronos._cycle())
    assert any("call the bank" in m for m in dms), "he asked for this reminder at a time"
    assert brief == [clock["t"]]


def test_pause_all_holds_reminders_and_the_brief_and_loses_neither(files, dms, clock, brief):
    _reminder_due()
    pauses.start(pauses.ALL)
    run(chronos._cycle())
    assert dms == [] and brief == []
    assert "call the bank" in "\n".join(chronos._read_schedule()), "a held reminder stays queued"

    pauses.end("resume")                          # ...and arrives, late, as soon as he resumes
    run(chronos._cycle())
    assert any("call the bank" in m for m in dms)
    assert brief == [clock["t"]]


def _queue_night_task(task="research the ECB rate path for the morning"):
    chronos._write_schedule([chronos._format_night_line("2026-09-29", "", task)])


@pytest.fixture
def night(monkeypatch):
    ran = []

    async def fake_execute(prompt):
        ran.append(prompt)
        return "findings"

    async def fake_vault_write(filename, content, folder):
        return f"Confirmed written to jarvis-vault/{folder}/{filename}"
    monkeypatch.setattr("backend.brain.background.execute_task", fake_execute)
    monkeypatch.setattr("backend.tools.vault.vault_write", fake_vault_write)
    return ran


def test_a_pause_holds_night_tasks_until_resume(files, dms, clock, brief, night):
    clock["t"] = paris(2026, 10, 1, 3, 0)         # inside the 02:00-07:00 window
    _queue_night_task()
    pauses.start(pauses.WORK)
    run(chronos._cycle())
    assert night == []
    assert chronos._parse_night_line(chronos._read_schedule()[-1]), "the task stays queued"

    pauses.end("resume")
    run(chronos._cycle())
    assert len(night) == 1


def test_a_night_task_stopped_mid_run_is_put_back_and_listed(files, clock, monkeypatch):
    clock["t"] = paris(2026, 10, 1, 3, 0)
    _queue_night_task("research the ECB rate path for the morning")
    original = chronos._read_schedule()[-1]
    pauses.start(pauses.WORK)

    async def stopped(prompt):
        raise pause.Paused()
    monkeypatch.setattr("backend.brain.background.execute_task", stopped)
    run(chronos._run_night_task(clock["t"]))

    assert original in chronos._read_schedule(), "the very same line is queued again"
    assert ("night_task_stopped", "research the ECB rate path for the morning") in skips()
    assert chronos._load_state().get("night_results") in (None, []), "no fake result was stored"


def test_a_night_task_that_fails_is_still_consumed_not_kept_forever(files, clock, monkeypatch):
    clock["t"] = paris(2026, 10, 1, 3, 0)
    _queue_night_task()

    async def broken(prompt):
        raise RuntimeError("model down")

    async def fake_vault_write(filename, content, folder):
        return "Confirmed"
    monkeypatch.setattr("backend.brain.background.execute_task", broken)
    monkeypatch.setattr("backend.tools.vault.vault_write", fake_vault_write)
    run(chronos._run_night_task(clock["t"]))
    assert not [n for n in map(chronos._parse_night_line, chronos._read_schedule()) if n]


def _task_line(due, task="look up NVDA's live share price and report it with its source",
               repeat="daily"):
    return chronos._format_task_line(due, repeat, "", "Europe/Paris", task)


@pytest.fixture
def started(monkeypatch):
    calls = []

    async def start(label, brief_text, skill="", occurrence=""):
        calls.append(label)
        return ""
    monkeypatch.setattr(background, "start_scheduled", start)
    return calls


def test_a_scheduled_run_during_pause_all_is_skipped_recorded_and_not_delayed(
        files, dms, clock, started):
    clock["t"] = paris(2026, 9, 30, 15, 31)
    chronos._write_schedule([_task_line(paris(2026, 9, 30, 15, 30))])
    pauses.start(pauses.ALL)
    run(chronos._fire_due_tasks(clock["t"]))

    assert started == [], "a price 'at the open' is wrong an hour late: it is skipped, not delayed"
    assert dms == [], "nothing is reported as missed while he paused it himself"
    assert skips() == [("scheduled_task", "look up NVDA's live share price and report it with its source (Wed 15:30)")]
    rest = chronos._read_schedule()
    assert any("2026-10-01 15:30" in l for l in rest), "a daily task moves on to tomorrow"
    assert any("task skipped (paused)" in l for l in rest), "the trail says why it did not run"


@pytest.mark.parametrize("skill", ["", "market-check"])
def test_a_plain_pause_does_not_stop_a_scheduled_task(files, dms, clock, started, skill):
    clock["t"] = paris(2026, 9, 30, 15, 31)
    chronos._write_schedule([chronos._format_task_line(
        paris(2026, 9, 30, 15, 30), "daily", "", "Europe/Paris", "NVDA research", skill)])
    pauses.start(pauses.WORK)
    run(chronos._fire_due_tasks(clock["t"]))
    assert len(started) == 1, "/pause holds my own work, not what the owner scheduled"
    assert pauses.skips(pauses.recent(1)[0]["id"]) == []


def test_pause_all_lists_the_skipped_scheduled_task_for_resume(files, dms, clock, started):
    clock["t"] = paris(2026, 9, 30, 15, 31)
    chronos._write_schedule([_task_line(paris(2026, 9, 30, 15, 30))])
    pauses.start(pauses.ALL)
    run(chronos._fire_due_tasks(clock["t"]))
    assert started == [] and [k for k, _ in skips()] == ["scheduled_task"]
    pauses.end("resume")
    assert "skipped scheduled check" in pause._stretch_text(pauses.recent(1)[0])




def test_the_same_run_without_a_pause_starts(files, dms, clock, started):
    clock["t"] = paris(2026, 9, 30, 15, 31)
    chronos._write_schedule([_task_line(paris(2026, 9, 30, 15, 30))])
    run(chronos._fire_due_tasks(clock["t"]))
    assert len(started) == 1


def test_a_run_waiting_for_a_free_slot_is_skipped_if_a_pause_starts(files, dms, clock, monkeypatch):
    clock["t"] = paris(2026, 9, 30, 15, 31)
    chronos._write_schedule([_task_line(paris(2026, 9, 30, 15, 30))])
    busy = ["Error: 3 background tasks are already running"]

    async def start(label, brief_text, skill="", occurrence=""):
        return busy[0]
    monkeypatch.setattr(background, "start_scheduled", start)
    run(chronos._fire_due_tasks(clock["t"]))
    assert len(chronos._load_state().get("due_task_runs", [])) == 1

    pauses.start(pauses.ALL)
    clock["t"] = paris(2026, 9, 30, 15, 32)
    run(chronos._fire_due_tasks(clock["t"]))
    assert len(chronos._load_state().get("due_task_runs", [])) == 0 and dms == []
    assert [k for k, _ in skips()] == ["scheduled_task"]


def test_the_cycle_records_scheduled_skips_but_starts_nothing(files, dms, clock, brief, started):
    clock["t"] = paris(2026, 9, 30, 15, 31)
    chronos._write_schedule([_task_line(paris(2026, 9, 30, 15, 30))])
    pauses.start(pauses.ALL)
    run(chronos._cycle())
    assert started == [] and len(skips()) == 1


# ===========================================================================================
# Background jobs
# ===========================================================================================

@pytest.fixture
def jobs(monkeypatch):
    monkeypatch.setattr(background, "_tasks", {})
    monkeypatch.setattr(background, "_next_id", 1)


def test_a_new_background_job_is_refused_while_paused(jobs):
    pauses.start(pauses.WORK)
    result = run(background.run_in_background("research the ECB's next meeting in depth"))
    assert result.startswith("Error:") and "/pause" in result and "on hold" in result
    assert background._tasks == {}


def test_every_kind_of_new_job_goes_through_the_same_refusal(jobs):
    pauses.start(pauses.WORK)
    for coro in (background._start("t", "b"),
                 background._start("t", "b", kind="web_research"),
                 background._start("t", "b", deep=True, notify=True)):
        task, error = run(coro)
        assert task is None and "/pause" in error


def test_a_job_admitted_while_paused_waits_and_starts_on_resume(jobs, monkeypatch):
    started = []

    async def worker(t):
        started.append(t.id)
    monkeypatch.setattr(background, "_worker", worker)
    queued = background.BgTask(id=1, description="compare gyms", status="queued")
    background._tasks[1] = queued

    async def main():
        pauses.start(pauses.WORK)
        background._schedule_queued()
        assert queued.status == "queued" and queued._handle is None

        pauses.end("resume")
        background.resume_queued()
        assert queued.status == "running"
        await queued._handle
    run(main())
    assert started == [1]


def _tool_call_response(text="thinking"):
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="vault_list", arguments="{}"))
    msg = SimpleNamespace(content=text, tool_calls=[tc])
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=msg)])


def test_a_compat_job_stops_at_the_next_step_after_a_pause(jobs, monkeypatch):
    """The model call in flight finishes, its tools run, and the next call never starts."""
    from backend.brain import engine as claude_mod
    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            pauses.start(pauses.WORK)                 # /pause arrives while this call is out
            return _tool_call_response()

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(claude_mod, "_get_compat_client", lambda: client)
    monkeypatch.setattr(claude_mod, "_to_openai_tools", lambda tools: [])

    async def fake_exec(name, tool_input, invalid=None):
        return "ok"
    monkeypatch.setattr(background, "_bg_exec", fake_exec)

    with pytest.raises(pause.Paused):
        run(background._run_compat_loop("do a long thing"))
    assert len(calls) == 1


def test_a_job_stopped_by_the_pause_is_cancelled_listed_and_never_reported_as_a_failure(
        jobs, monkeypatch):
    async def runner(text, **kw):
        pauses.start(pauses.WORK)
        raise pause.Paused()
    monkeypatch.setattr(background, "_run_compat_loop", runner)

    async def main():
        pauses.end("resume")                      # _start refuses while paused
        t, error = await background._start("compare gyms near the office", "brief")
        assert not error
        await t._handle
        return t
    t = run(main())
    assert t.status == "cancelled" and "Stopped by /pause" in t.summary
    assert t.announced, "the resume message lists it; no next-turn notice as well"
    assert skips() == [("job_stopped", "#1 compare gyms near the office")]


def test_pausing_cancels_a_web_research_job_and_leaves_agent_jobs_to_stop_themselves(
        jobs, monkeypatch):
    async def main():
        pauses.start(pauses.WORK)
        report = background.BgTask(id=1, description="what did the ECB decide", status="running",
                                   kind="web_research")
        agent = background.BgTask(id=2, description="compare gyms", status="running")
        for t in (report, agent):
            t._handle = asyncio.create_task(asyncio.sleep(30))
            background._tasks[t.id] = t
        assert background.pause_running() == 1
        await asyncio.sleep(0)
        try:
            await report._handle
        except asyncio.CancelledError:
            pass
        assert report._handle.cancelled()
        assert not agent._handle.cancelled()
        agent._handle.cancel()
    run(main())
    assert skips() == [("job_stopped", "web research #1: what did the ECB decide")]


# ===========================================================================================
# Profile research
# ===========================================================================================

def test_profile_research_does_not_start_while_paused():
    from backend.brain import people_research
    pauses.start(pauses.WORK)
    result = people_research.start("Marcus Aurelius")
    assert result.startswith("Error:") and "/pause" in result
    assert people_research.running() == []


@pytest.fixture
def profile_vault(tmp_path, monkeypatch):
    from backend.brain import people as P, people_research as R
    from backend.tools import vault as V
    root = tmp_path / "jarvis-vault"
    (root / "people").mkdir(parents=True)
    monkeypatch.setattr(V, "_VAULT", root)
    monkeypatch.setattr(V, "_index_note_async", lambda *a, **k: None)
    monkeypatch.setattr(P, "_today", lambda: "2026-09-26")

    async def no_page(url):
        return ""
    monkeypatch.setattr(R, "_read_page", no_page)
    R._pending.clear()
    R._running.clear()
    return root


def _fill_with(monkeypatch, on_llm_call):
    """Stub search and the model; `on_llm_call(n)` runs at the start of the n-th model call
    (1-based), which is where a /pause typed mid-job would land."""
    from backend.brain import people_research as R
    from backend.brain.test_people_research import EVIDENCE
    calls = {"search": [], "llm": 0}

    async def fake_search(q):
        calls["search"].append(q)
        return EVIDENCE

    async def fake_llm(prompt, max_tokens=0):
        calls["llm"] += 1
        on_llm_call(calls["llm"])
        return json.dumps({"sections": {}})
    monkeypatch.setattr(R, "_search", fake_search)
    monkeypatch.setattr(research, "_llm", fake_llm)
    return calls


def test_a_profile_fill_stops_between_batches_and_says_what_is_saved(profile_vault, monkeypatch):
    from backend.brain import people as P, people_research as R
    run(P.person_profile("create", name="Marcus Aurelius", kind="historical"))
    calls = _fill_with(monkeypatch, lambda n: pauses.start(pauses.WORK) if n == 1 else None)
    summary = run(R.fill("Marcus Aurelius"))
    assert calls["llm"] == 1, "the batch in flight finishes; the next one never starts"
    assert "paused" in summary and "saved" in summary
    assert skips() == [("job_stopped", "profile research on Marcus Aurelius")]


def test_a_profile_fill_stops_between_targeted_passes(profile_vault, monkeypatch):
    from backend.brain import people as P, people_research as R
    run(P.person_profile("create", name="Marcus Aurelius", kind="historical"))
    general = len(R._batches([s for s in P.load_template()
                              if s.applies_to("historical") and s.research]))
    calls = _fill_with(monkeypatch,
                       lambda n: pauses.start(pauses.WORK) if n == general + 1 else None)
    summary = run(R.fill("Marcus Aurelius"))
    assert calls["llm"] == general + 1
    assert len(calls["search"]) == 2 + 1, "the second targeted search must not run"
    assert "paused" in summary


def test_the_same_fill_without_a_pause_runs_every_pass(profile_vault, monkeypatch):
    from backend.brain import people as P, people_research as R
    run(P.person_profile("create", name="Marcus Aurelius", kind="historical"))
    calls = _fill_with(monkeypatch, lambda n: None)
    summary = run(R.fill("Marcus Aurelius"))
    assert "paused" not in summary
    assert len(calls["search"]) == 2 + R._MAX_TARGETED


# ===========================================================================================
# Tools that set something up must not promise a run the pause is holding
# ===========================================================================================

def test_setting_up_a_reminder_says_so_only_under_pause_all(files, clock):
    assert "paused" not in run(chronos.schedule_reminder("add", "2026-10-02 09:00", "call the bank"))
    pauses.start(pauses.WORK)
    assert "paused" not in run(chronos.schedule_reminder("add", "2026-10-02 10:00", "email Sarah"))
    pauses.start(pauses.ALL)
    reply = run(chronos.schedule_reminder("add", "2026-10-02 11:00", "water the plants"))
    assert reply.startswith("Confirmed") and "paused everything I do on my own" in reply


def test_timed_night_and_research_tools_say_the_pause_holds_them(files, tmp_path, monkeypatch, clock):
    monkeypatch.setattr(research, "_QUEUE_FILE", tmp_path / "research-queue.md")
    pauses.start(pauses.WORK)
    replies = [
        run(chronos.schedule_night_task("research the ECB rate path for the morning brief")),
        run(research.research_queue("add", "Albert Camus")),
    ]
    for reply in replies:
        assert "paused my own work" in reply and "/resume" in reply, reply


def test_a_scheduled_task_is_only_noted_under_pause_all(files, clock):
    task = "look up NVDA's live share price and report it with its source"
    pauses.start(pauses.WORK)
    assert "paused" not in run(chronos.schedule_task("add", "2026-10-02 15:30", task, "once")),         "a plain pause does not hold it"
    pauses.start(pauses.ALL)
    reply = run(chronos.schedule_task("add", "2026-10-03 15:30", task, "once"))
    assert "paused everything I do on my own" in reply and "/resume" in reply


def test_a_watch_says_the_pause_holds_it(monkeypatch):
    from backend.brain import watches
    args = dict(action="add", subject="NVDA", condition="closes below $180", method="web",
                query="NVDA close", every="hour")
    before = run(watches.watch_action(**args))
    assert before.startswith("Confirmed") and "paused" not in before
    pauses.start(pauses.WORK)
    after = run(watches.watch_action(**{**args, "subject": "TSLA", "query": "TSLA close"}))
    assert after.startswith("Confirmed") and "paused my own work" in after


# ===========================================================================================
# /status
# ===========================================================================================

def test_status_shows_the_pause_only_while_it_is_in_force():
    from backend.brain import runtime_status
    assert "PAUSED" not in runtime_status.runtime_status_text()
    pauses.start(pauses.WORK, reason="on holiday")
    text = runtime_status.runtime_status_text()
    assert "PAUSED since" in text and "on holiday" in text
    pauses.end("resume")
    assert "PAUSED" not in runtime_status.runtime_status_text()


# ===========================================================================================
# Unprompted messages while paused
# ===========================================================================================

def test_a_research_finding_is_not_dmed_while_paused_and_the_gap_still_applies():
    now = 1_000_000.0
    assert research._dm_hold_reason(now, now - 3 * 3600) == ""
    assert research._dm_hold_reason(now, now - 60) == "min gap"
    pauses.start(pauses.WORK)
    assert research._dm_hold_reason(now, now - 3 * 3600) == "paused"
    assert research._dm_hold_reason(now, now - 60) == "paused", "the pause is the reason he sees"


def test_the_research_pass_asks_before_it_sends():
    """The notify branch is deep inside _tick (no test drives it whole -- see A06), so this
    pins that it decides through _dm_hold_reason and records why a finding was held."""
    import inspect
    src = inspect.getsource(research._tick)
    assert "hold = _dm_hold_reason(now, last_notify)" in src
    assert "if not hold:" in src and '(DM held: {hold})' in src


def test_a_profile_fill_stopped_by_the_pause_sends_no_message_of_its_own(monkeypatch):
    from backend.brain import people_research as R
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(R, "_notifier", notifier)
    R._pending.clear()

    async def stopped(name):
        return f"Profile research on {name} {R._PAUSED_TEXT}; what it had found is saved."
    monkeypatch.setattr(R, "fill", stopped)
    run(R._run("marcus", "Marcus Aurelius"))
    assert sent == [] and R._pending == []


def test_a_finished_profile_fill_still_reports(monkeypatch):
    from backend.brain import people_research as R
    sent = []

    async def notifier(text):
        sent.append(text)
        return True
    monkeypatch.setattr(R, "_notifier", notifier)

    async def done(name):
        return f"Researched {name}: 12 new entries with sources."
    monkeypatch.setattr(R, "fill", done)
    run(R._run("marcus", "Marcus Aurelius"))
    assert sent == ["Researched Marcus Aurelius: 12 new entries with sources."]


# The first version of this fix changed only chronos, and its test stubbed
# start_scheduled -- so under a plain /pause the real background._start still refused the
# run. These go through the real admission, the queue and the per-step stop.

def test_the_real_admission_takes_a_scheduled_run_under_a_plain_pause(jobs, monkeypatch):
    async def worker(t):
        return None
    monkeypatch.setattr(background, "_worker", worker)
    pauses.start(pauses.WORK)
    task, error = run(background._start("report NVDA's close", "b", kind="scheduled"))
    assert error == "" and task is not None
    task, error = run(background._start("look into the ECB", "b"))
    assert task is None and "/pause" in error            # ordinary jobs stay held


def test_pause_all_holds_the_scheduled_run_at_admission(jobs):
    pauses.start(pauses.ALL)
    task, error = run(background._start("report NVDA's close", "b", kind="scheduled"))
    assert task is None and "/pause" in error


def test_a_running_scheduled_job_is_not_stopped_by_a_plain_pause(monkeypatch):
    pauses.start(pauses.WORK)
    token = background._job_kind.set("scheduled")
    try:
        background._stop_if_paused()                      # does not raise
    finally:
        background._job_kind.reset(token)
    with pytest.raises(pause.Paused):
        background._stop_if_paused()                      # an ordinary job does stop


def test_the_queue_starts_a_scheduled_run_but_holds_others_under_a_plain_pause(jobs, monkeypatch):
    started = []

    async def worker(t):
        started.append(t.id)
    monkeypatch.setattr(background, "_worker", worker)
    background._tasks[1] = background.BgTask(id=1, description="ECB deep dive", status="queued")
    background._tasks[2] = background.BgTask(id=2, description="NVDA close", status="queued",
                                             kind="scheduled")
    pauses.start(pauses.WORK)

    async def go():
        background._schedule_queued()
        await asyncio.sleep(0)
    run(go())
    assert started == [2]
    assert background._tasks[1].status == "queued"

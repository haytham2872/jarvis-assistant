"""backend/brain/turn_signals.py -- what a message needs, decided by Jev, not keywords.

Every turn, one call to Jev (TypeSafe's classifier, served by OpenRouter's decisions
endpoint) answers all the per-turn questions at once: should this go to the thinking
tier, which self-model sections to load, does it ask for JARVIS's opinion, what's new,
his track record, his runtime state, and which reminders to add (save/schedule,
correction, forecast, "you already told me"). It replaces a dozen hand-written keyword
lists that missed paraphrases and fired on shared words.

Measured before wiring (289 real messages + 36 hard cases, reference labels from Claude
Sonnet using the same definitions -- see CLAUDE.md "Jev turn signals"): the thinking tier
fired on 112 messages instead of 179 while missing the same 17 truly complex ones;
save/schedule requests caught 26 of 30 instead of 3; wrongly loaded self-model sections
6 instead of 52; hard cases 35/37 instead of 23/37.

How it stays fast and safe:

* **One call, started first.** `start()` fires the request at the top of chat(), so it
  runs alongside the embedding and memory retrieval. Latency is a flat ~0.26s
  regardless of how many questions are asked (measured), so everything goes in one call.
* **Only the tier waits for it,** and the tier is only needed after retrieval, just
  before the model call. Retrieval depth keeps its existing gate, because it has to be
  decided before retrieval starts.
* **A hard deadline.** If Jev has not answered by `DEADLINE_S` after the turn started, or
  fails in any way, `resolve()` returns None and every decision falls back to the old
  keyword/embedding logic -- exactly today's behaviour, never an error.
* **Thresholds are measured, not guessed** -- chosen on held-out halves of the real set.
* **Keywords still count where they never fire wrongly.** The save/schedule, correction,
  forecast and recall lists had zero false fires on the real messages, so for those four
  a keyword hit OR a Jev hit counts: recall for free.

Pinned model version and disable switch live in router.py (JEV_MODEL, jev_enabled()).
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DEADLINE_S = 0.8          # from start(); measured p50 0.26s, p90 0.34s, idle-reconnect 0.5s
_HTTP_TIMEOUT_S = 2.0     # the request itself; the deadline above is what the turn waits

# (question, true-criterion, false-criterion) -- the exact wording that was measured.
QUESTIONS: dict[str, tuple[str, str, str]] = {
    "portfolio": (
        _owner.say("Is «owner» asking about «his» own investment holdings, portfolio value or P&L, "
        "recording a trade, or reviewing an investment thesis?"),
        _owner.say("«He» asks about «his» holdings, trades, portfolio or investment thesis"),
        _owner.say("«He» asks general market news or something unrelated to «his» own investment record"),
    ),
    "complex": (
        _owner.say("Does this message need careful multi-step reasoning, planning, debugging, analysis, "
        "comparison, or a detailed look at how JARVIS works or what «he» remembers?"),
        "It needs real reasoning or analysis: planning, debugging, comparing options, explaining "
        "how or why something works, investigating JARVIS's own workings or memory",
        "A quick reply is enough: small talk, a thank-you, a simple fact, a short confirmation, "
        "or a simple command",
    ),
    "persistence": (
        _owner.say("Is «owner» asking JARVIS to save, record, schedule, remind, queue, note down, or watch "
        "something so it lasts beyond this reply?"),
        _owner.say("«He» asks for something to be written, saved, scheduled, set as a reminder, added to a "
        "list or queue, or watched for later"),
        _owner.say("«He» is not asking for anything to be stored or scheduled (asking what was saved, or "
        "talking about saving in general, does not count)"),
    ),
    "correction": (
        _owner.say("Is «owner» telling JARVIS how to behave from now on, correcting something JARVIS keeps "
        "getting wrong, or asking for a standing rule?"),
        "A lasting instruction or preference for future behaviour, a correction of a repeated "
        "mistake, or a request to make something a rule",
        "A one-off request for this reply, ordinary feedback, or a correction of a single fact "
        "with no lasting instruction",
    ),
    "forecast": (
        _owner.say("Is «owner» asking for a probability, the odds, or a prediction about a future outcome "
        "that could be checked later?"),
        _owner.say("«He» asks how likely something is, what the odds are, or whether some future event will "
        "happen"),
        _owner.say("«He» is not asking for a prediction of a checkable future outcome"),
    ),
    "recall_precision": (
        _owner.say("Does «owner» refer back to a specific thing «he» said or gave earlier (a number, a name, "
        "a detail) and expect JARVIS to recall or use it?"),
        _owner.say("«He» points back to something specific «he» already said or provided, like 'the amount I "
        "gave you' or 'like I said'"),
        _owner.say("«He» does not point back to a specific earlier detail of «his» own"),
    ),
    "runtime": (
        _owner.say("Is «owner» asking about JARVIS's current live operating state: uptime, health, cost or "
        "spend, running background tasks, which model is running, memory health?"),
        _owner.say("«He» asks how JARVIS is running right now, what it costs, its status or health"),
        _owner.say("«He» is not asking about JARVIS's live operating state"),
    ),
    "opinion": (
        _owner.say("Is «owner» asking for JARVIS's own view, stance or belief on a subject, or whether "
        "JARVIS has changed «his» mind?"),
        _owner.say("«He» asks what JARVIS «himself» thinks or believes about something"),
        _owner.say("«He» asks for facts, actions, or information rather than JARVIS's own view"),
    ),
    "calibration": (
        _owner.say("Is «owner» asking how well JARVIS knows or predicts «him», or about JARVIS's track "
        "record or accuracy at predictions?"),
        _owner.say("«He» asks how well JARVIS knows «him», predicts «him», or how accurate JARVIS's predictions "
        "have been"),
        _owner.say("«He» is not asking about JARVIS's accuracy or how well JARVIS knows «him»"),
    ),
    # Added with person profiles (2026-09-26), measured separately on 336 real messages
    # (Claude-labelled with this definition): only 8 are about someone he knows. A false
    # fire costs one "offer a profile?" nudge, rate-limited and still judged by the model.
    "known_person": (
        _owner.say("Is «owner» talking about a specific person «he» knows personally -- a friend, family "
        "member, partner, colleague or acquaintance -- and saying something about them?"),
        _owner.say("«He» talks about a particular person in «his» own life: what they did, said, are like, "
        "or what is going on with them"),
        _owner.say("No particular person from «his» own life is being talked about (public figures, "
        "JARVIS, «himself» only, or a passing name with nothing said about them do not count)"),
    ),
    # G3 (2026-10-06): an offer of a goal, never permission to start one. Wording from
    # docs/archive/GOALS-ROADMAP.md section 12, measured with scripts/measure_goal_signal.py.
    "goal": (
        _owner.say("Is «owner» asking for work that takes more than one sitting: following up over days, "
        "waiting on something, keeping watch, or more research than one answer?"),
        _owner.say("«He» asks JARVIS to take on an ongoing objective with linked work, follow-up or research "
        "beyond one reply: keep looking, keep chasing, track something through an event, work "
        "on it over days and come back"),
        "An ordinary question, a quick price, a simple reminder or alert, one standalone "
        "scheduled check, one-off research or a long comparison that fits in one answer, or "
        "discussing or planning something without asking JARVIS to carry it out over time",
    ),
    "whats_new": (
        _owner.say("Is «owner» asking what is new or changed in JARVIS, when JARVIS got a capability, or "
        "about recent updates to JARVIS?"),
        _owner.say("«He» asks about JARVIS's recent changes, updates, new features, or when «he» got something"),
        _owner.say("«He» is not asking about changes or updates to JARVIS «himself»"),
    ),
}

SECTIONS: dict[str, str] = {
    "none": _owner.say("The message is not about how JARVIS «himself» works"),
    "router": "Which AI model JARVIS runs on, model routing, providers, thinking tier",
    "memory": _owner.say("JARVIS's memory: recall, facts, sessions, sleep, letters, rumination, vault notes, "
              "consolidation, predictions about «owner»"),
    "claude": "JARVIS's conversation pipeline: system prompt, tool loop, planning, steering",
    "personality": "JARVIS's identity, character, nature, self-reflection, mood",
    "tts": "JARVIS's speaking voice and text-to-speech",
    "stt": _owner.say("JARVIS hearing «owner»: microphone, transcription, voice notes"),
    "wake": "The wake word",
    "tools": "JARVIS's tools and capabilities: web search, email, calendar, Spotify, Notion, "
             "vault tools, watches, reminders, code graph, Discord commands",
    "main": "JARVIS's server: Discord bridge, startup, shutdown, background loops, deployment",
    "frontend": "The desktop interface and neural sphere",
    "scripts": "Setup and launch scripts",
}
_SECTION_Q = _owner.say("Which part of JARVIS's own design is «owner» asking about, if any?")

# Chosen on held-out halves of the 289 real messages (see module docstring).
THRESHOLDS: dict[str, float] = {
    "complex": 0.4, "persistence": 0.6, "correction": 0.7, "forecast": 0.5,
    "recall_precision": 0.5, "runtime": 0.5, "opinion": 0.6, "calibration": 0.6,
    "whats_new": 0.4, "known_person": 0.65, "portfolio": 0.5,
    # goal: measured 2026-10-06 (scripts/measure_goal_signal.py): halves chose 0.95 and
    # 0.91; the lower ships because a false fire only puts an offer in front of the model.
    "goal": 0.91,
}
SECTION_MIN_P = 0.3
MAX_SECTIONS = 2
# Keyword lists that never fired wrongly on the real set: a hit there also counts.
KEYWORD_OR = ("persistence", "correction", "forecast", "recall_precision", "known_person")
# known_person is the exception: its relation-word list ("my sister") fired wrongly 2
# times in 336, but OR'd with Jev at 0.65 it catches 5 of 8 against Jev's 3 of 8 alone,
# and a false fire only puts a rate-limited offer nudge in front of the model.


@dataclass
class Signals:
    """Final per-turn decisions. `probabilities` keeps Jev's raw numbers for logging."""
    flags: dict[str, bool]
    sections: list[str]
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_s: float = 0.0

    def get(self, name: str) -> bool:
        return bool(self.flags.get(name, False))


def _request_body(text: str) -> dict:
    from backend.brain.router import JEV_MODEL
    questions = {k: {"type": "noul", "instructions": q, "criteria": {"true": t, "false": f}}
                 for k, (q, t, f) in QUESTIONS.items()}
    questions["sections"] = {"type": "choice", "instructions": _SECTION_Q, "criteria": SECTIONS}
    return {"model": JEV_MODEL,
            "state": _owner.say(f"«Owner's» message to «his» assistant JARVIS:\n{text}"),
            "questions": questions}


def _keyword_hits(text: str) -> dict[str, bool]:
    from backend.brain import personality as p
    from backend.brain import people
    return {
        "persistence": p.is_persistence_request(text),
        "correction": p.is_correction_request(text),
        "forecast": p.is_forecast_request(text),
        "recall_precision": p.is_recall_precision_request(text),
        "known_person": people.is_known_person_mention(text),
    }


def interpret(answers: dict, text: str) -> Signals:
    """Jev's raw answers -> final decisions. Pure. Raises on a malformed answer, which
    resolve() turns into the fallback."""
    probs = {k: float(answers[k]["noul"]) for k in QUESTIONS}
    flags = {k: probs[k] >= THRESHOLDS[k] for k in QUESTIONS}
    kw = _keyword_hits(text)
    for k in KEYWORD_OR:
        flags[k] = flags[k] or kw[k]
    choice = answers["sections"].get("probabilities") or {}
    ranked = sorted(((s, float(p)) for s, p in choice.items() if s in SECTIONS and s != "none"),
                    key=lambda sp: -sp[1])
    sections = [s for s, p in ranked[:MAX_SECTIONS] if p >= SECTION_MIN_P]
    return Signals(flags=flags, sections=sections, probabilities=probs)


async def _call(text: str) -> tuple[dict, float, dict]:
    from backend.brain import usage
    from backend.brain.router import JEV_MODEL, JEV_URL, OPENROUTER
    from backend.providers import gateway
    t0 = time.perf_counter()
    r = await gateway.get_http_client().post(
        JEV_URL, json=_request_body(text), timeout=_HTTP_TIMEOUT_S,
        headers={"Authorization": f"Bearer {OPENROUTER.api_key}", **OPENROUTER.default_headers})
    r.raise_for_status()
    data = r.json()
    u = data.get("usage") or {}
    usage.record(JEV_MODEL, int(u.get("input_tokens") or 0), 0, cost=u.get("cost"))
    return data["answers"], time.perf_counter() - t0, u


def _record(latency_s: float, usage_: dict | None, finish: str) -> None:
    """G0: one llm_calls row per Jev call (purpose "jev"), so the server can report its
    latency, cost and deadline misses. Written by resolve() once the turn's decision is
    made, never inside the window it measures (a first write that created the table took
    longer than a test's whole deadline). Logging only: nothing routes on it. Never raises."""
    try:
        from backend.brain.router import JEV_MODEL
        from backend.storage import llm_calls
        u = usage_ or {}
        llm_calls.record(
            model=JEV_MODEL, started_at=time.time() - max(0.0, latency_s), purpose="jev",
            effort="none",
            usage={"input_tokens": u.get("input_tokens"), "output_tokens": 0,
                   "cost": u.get("cost")} if usage_ is not None else None,
            finish_reason=finish, outcome="answer" if finish == "stop" else "empty")
    except Exception:
        pass


async def warm() -> None:
    """Open the pooled connection before the first turn needs it. Measured in fresh
    processes: the first classification took 0.32-0.53s cold and 0.26-0.30s after this
    (the same as a warm one). A GET of one model listing: no classification, no cost.
    Never raises."""
    from backend.brain.router import jev_enabled, OPENROUTER
    from backend.providers import gateway
    if not jev_enabled():
        return
    try:
        await gateway.get_http_client().get(
            OPENROUTER.base_url.rstrip("/") + "/models", params={"limit": 1},
            timeout=_HTTP_TIMEOUT_S)
    except Exception as e:
        log.debug("turn_signals: warm-up skipped (%s)", e)


def start(text: str) -> asyncio.Task | None:
    """Fire the classification for this turn. None when Jev is disabled or unconfigured."""
    from backend.brain.router import jev_enabled
    if not (text or "").strip() or not jev_enabled():
        return None
    try:
        task = asyncio.create_task(_call(text))
    except RuntimeError:          # no running loop
        return None
    # If the turn dies before resolve() reads it, the failure is still consumed rather
    # than surfacing later as "Task exception was never retrieved".
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


async def resolve(task: asyncio.Task | None, text: str, started_at: float) -> Signals | None:
    """Wait for the call until DEADLINE_S after `started_at`; None means "use the old
    logic". Never raises. A late answer is abandoned, not waited for."""
    if task is None:
        return None
    remaining = DEADLINE_S - (time.perf_counter() - started_at)
    try:
        answers, latency, u = await asyncio.wait_for(asyncio.shield(task), max(0.0, remaining))
        _record(latency, u, "stop")
        signals = interpret(answers, text)
        signals.latency_s = latency
        log.debug("turn_signals: %s sections=%s in %.2fs",
                  sorted(k for k, v in signals.flags.items() if v), signals.sections, latency)
        return signals
    except asyncio.TimeoutError:
        task.cancel()
        _record(time.perf_counter() - started_at, None, "deadline")
        log.info("turn_signals: Jev missed the %.1fs deadline -- keyword fallback this turn",
                 DEADLINE_S)
    except Exception as e:
        _record(time.perf_counter() - started_at, None, "error")
        log.warning("turn_signals: Jev unavailable (%s) -- keyword fallback this turn",
                    str(e)[:160])
    return None

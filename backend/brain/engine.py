from backend import owner as _owner
import os
import re
import time
import json
import uuid
import asyncio
import logging
from typing import Callable, Awaitable, Any

from backend.brain.router import (
    route, route_tier, is_complex, is_complex_result as router_is_complex_result,
    provider as _provider, with_provider_prefs,
)
from backend.brain import intent
from backend.storage import operations as _ops
from backend.storage import llm_calls as _llm_calls
from backend.tools import verbatim as _verbatim
from backend.tools import validation as _validation
from backend.tools import gateway as _gateway
from backend.tools import policy as _policy
from backend.tools import approval_flow as _approval_flow
from backend.context import origin as _origin
from backend.context import deadlines as _deadlines
from backend.context.budget import CHARS_PER_TOKEN
from backend.brain import rerank as _rerank
from backend.brain.memory import (
    recall, ingest as _ingest_mem, correct_memory,
    RecallResult as _RecallResult,
)
from backend.brain.mood import after_turn, persist_mood, get_mood
from backend.brain.personality import build_system_prompt, is_persistence_request
from backend.brain.threads import record_thread, assign_session, SessionDecision
from backend.brain.consolidation import consolidate_session
from backend.tools.registry import TOOLS
from backend.tools.browser import web_browse, fetch_page
from backend.tools.gmail import gmail_action
from backend.tools.calendar import calendar_action
from backend.tools.spotify import spotify_action
from backend.tools.notion import notion_action
from backend.tools.codegraph import code_graph
from backend.tools.vault import (
    vault_write, vault_read,
    vault_list as _vault_list,
    vault_share,
    read_queue as _read_queue,
    read_owner as _read_owner,
    read_latest_letter as _read_latest_letter,
    read_relevant_topic as _read_relevant_topic,
    vault_write_queue,
    memory_search,
    memory_timeline,
)
from backend.brain import background
from backend.brain import research
from backend.brain import usage
from backend.brain import runtime_status
from backend.brain import chronos
from backend.brain import predictions
from backend.brain import steering
from backend.brain import escalation
from backend.brain import tool_markup as _tool_markup
from backend.brain import rumination
from backend.brain import watches
from backend.brain import pause as _pause
from backend.brain import instructions
from backend.brain import forecasts
from backend.brain import attachments as _attachments
from backend.brain import skills as _skills
from backend.brain import skill_checks as _skill_checks
from backend.brain import weekplan
from backend.brain import portfolio as _portfolio
from backend.brain import people
from backend.brain import people_research
from backend.brain import turn_signals
from backend.brain import turn_input as _turn_input
from backend.brain import discord_context
from backend.brain import goals as _goals
from backend.brain import goal_runner as _goal_runner
from backend.brain import promise_check as _promise_check
from backend.brain import tool_record as _tool_record
from backend.brain.self_model import full_digest, is_ready as _self_model_ready
from backend.tasks import spawn

log = logging.getLogger(__name__)

# Every channel ("local" = voice/UI/terminal, "discord" = the phone bridge)
# tracks its own active session and pending thread-write task, so a Discord
# burst and a desk conversation never stomp on each other's bookkeeping. All
# turns across all channels still run single-flight (one shared pipeline lock
# in main.py) -- this is about which conversation a turn belongs to, not
# about running two turns at once (chat()'s per-turn tool-stat globals, below,
# are only safe under that single-flight guarantee).
_active_session_ids:   dict[str, str] = {}
_pending_thread_tasks: dict[str, asyncio.Task] = {}

# Sticky window for "deep" context assembly (memory recall, vault letter, topic
# and skill search). is_complex_result() only sees the current message, so a
# short follow-up right after a complex turn ("go deeper on that", "why")
# loses all continuity even though it's clearly still part of the same deep
# exchange. Once a turn classifies as complex, the next _DEEP_STICKY_TURNS
# turns also get deep context regardless of their own classification.
# Per-channel so a deep desk turn doesn't leak stickiness into a Discord reply.
_DEEP_STICKY_TURNS = 2
_deep_streaks: dict[str, int] = {}


def _resolve_deep(user_text: str, channel: str, intent_result=None) -> bool:
    if router_is_complex_result(user_text, intent_result):
        _deep_streaks[channel] = _DEEP_STICKY_TURNS
        return True
    streak = _deep_streaks.get(channel, 0)
    if streak > 0:
        _deep_streaks[channel] = streak - 1
        return True
    return False


# Session ids pinned by force_new_session() for the *next* turn on a channel,
# bypassing assign_session()'s gap check entirely -- see force_new_session.
_forced_session: dict[str, str] = {}


def force_new_session(channel: str) -> None:
    """Force the next turn on `channel` to start a brand-new session.

    Used by the Discord "/new" command. Closes out whatever session is
    currently active on this channel (fires its consolidation exactly like a
    natural gap-boundary would, so the old burst still gets a letter/vault
    update instead of being silently orphaned), then pins a freshly minted
    session id so the next assign_session() call is bypassed -- otherwise, if
    you type your next message right away, the *normal* gap check would see
    no gap at all and just reuse the old session, defeating the point of
    asking for a new one.
    """
    old_id = _active_session_ids.get(channel)
    if old_id:
        log.info("force_new_session(%s): closing session %s", channel, old_id[:8])
        spawn(_close_session_after_pending_write(channel, old_id))
    _forced_session[channel] = str(uuid.uuid4())


async def _close_session_after_pending_write(channel: str, session_id: str) -> None:
    """Await this channel's in-flight thread write, then consolidate.

    A02 acceptance: *"`/new` during a slow tool call cannot cross-contaminate
    sessions."* The turn itself was already safe -- it records under
    `decision.session_id`, the id captured at ITS start, so its exchange can never land
    in the fresh session. The ordering was not: consolidation fired immediately while
    the turn was still mid-tool-call, so the exchange was written into a session whose
    letter had already been produced. `consolidate_session` is idempotent on that
    letter, so it never ran again and the exchange **never reached the vault at all**.
    Reproduced: consolidation ran with 0 threads, then the exchange landed.

    `flush_session` already had this ordering for shutdown; `/new` is the same
    boundary reached a different way, so it gets the same bound (5s) and the same
    degradation -- a write that does not finish in time is logged and consolidation
    proceeds, because a late exchange is better than a session that is never closed.
    """
    task = _pending_thread_tasks.get(channel)
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except Exception as e:
            log.warning("force_new_session(%s): thread task did not complete cleanly: %s",
                        channel, e)
    await consolidate_session(session_id)


_session_flushed: bool = False


async def flush_session() -> None:
    """Await every channel's last pending thread write, then close its session.

    Called at graceful shutdown so the checkpoint + letter are written to
    Qdrant BEFORE the sleep marker goes in. Safe to call if no session has
    started yet on any channel. Idempotent — a second call in the same
    process is a no-op. Consolidates every channel that has an active
    session, not just one -- a run where you both talked at the desk and
    texted from your phone has two independent sessions to close.
    """
    global _session_flushed
    if _session_flushed:
        log.debug("flush_session: already flushed — skipping")
        return
    _session_flushed = True
    # Stop any still-running background tasks — subprocess children (e.g. a
    # code_graph call) get cleaned up via their CancelledError handlers.
    background.cancel_all()
    people_research.cancel_all()     # a fill keeps what it already wrote
    for channel, task in list(_pending_thread_tasks.items()):
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except Exception as e:
                log.warning("flush_session(%s): thread task did not complete cleanly: %s", channel, e)
    for channel, session_id in list(_active_session_ids.items()):
        if session_id:
            log.info("flush_session(%s): consolidating session %s …", channel, session_id[:8])
            await consolidate_session(session_id)


# Sentence boundary detection used for real-time TTS streaming
_ABBREV_RE = re.compile(
    r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|e\.g|i\.e|U\.S|U\.K|approx|dept|est|ext|"
    r"fig|incl|min|max|No|Vol|St|Ave|Blvd)\.",
    re.IGNORECASE,
)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _pop_sentences(buf: str) -> tuple[list[str], str]:
    """Peel completed sentences off the streaming buffer.

    Returns (complete_sentences, remainder).  Protects common abbreviations so
    'Dr. Smith' doesn't split at the period.
    """
    protected = _ABBREV_RE.sub(lambda m: m.group()[:-1] + "\x00", buf)
    parts = _SENT_SPLIT_RE.split(protected)
    if len(parts) <= 1:
        return [], buf
    complete  = [p.replace("\x00", ".").strip() for p in parts[:-1] if p.strip()]
    remainder = parts[-1].replace("\x00", ".")
    return complete, remainder


WS_Sender = Callable[[dict], Awaitable[None]]

async def _get_self_model() -> str:
    if not _self_model_ready():
        return "Self-model digest is still warming up — try again in a moment."
    text = full_digest()
    return text if text else "Self-model digest is empty."


# ---------------------------------------------------------------------------
# Step 1b (2026-09-08) -- the clarifying question, outbound half of mid-turn
# steering. Implemented as a genuine tool call rather than a marker JARVIS
# emits mid-stream: text streams to TTS/UI as it arrives (_pop_sentences),
# so anything already spoken can't be retroactively un-said if a marker
# turned out to be part of it -- a tool call is a naturally interceptable
# seam the streaming architecture already has, with no surgery needed on
# _run_stream_openai/_run_stream. Registered normally in registry.py so the
# model knows it exists; kept OFF background._ALLOWED_TOOLS so autonomous
# paths (research, chronos night tasks) can never reach it -- research.py's
# _llm, consolidation.py, and rumination.py are single-prompt completions
# with no tools param at all, so they're excluded by construction, not by
# this gate.
# ---------------------------------------------------------------------------

# Bounded wait for a reply before proceeding on assumption -- named constant,
# not a literal, per the plan ("tune once you have seen it in use").
_CLARIFY_WAIT_SECONDS = 75.0

# DI hook, same pattern as _plan_notifier below (and background.set_broadcaster
# / research.set_notifier): engine.py must not import discord_bridge directly.
# Kept separate from _plan_notifier despite both resolving to the same
# discord_bridge.send_to_owner in main.py -- every other notifier in this
# codebase (research/backup_loop/canary/chronos) gets its own named hook onto
# that one function too.
_clarify_notifier: Callable[[str], Awaitable[bool]] | None = None


def set_clarify_notifier(fn: Callable[[str], Awaitable[bool]]) -> None:
    """Injected from main.py: async fn(text) -> bool that DMs the owner."""
    global _clarify_notifier
    _clarify_notifier = fn


async def _ask_clarifying_question(question: str) -> str:
    """Ask the owner one short question mid-turn and wait briefly for a reply.

    Always notifies via Discord DM regardless of which channel the running
    turn is on -- it's the only fire-and-forget async channel available (the
    desk UI has no push-notification equivalent, and TTS can't pause
    mid-utterance to wait on an answer). The reply, wherever it arrives from,
    lands on the "discord" steering channel (see main.py's
    run_pipeline_from_discord: any Discord message that arrives while the
    pipeline is busy is pushed there) -- so this always waits on "discord"
    specifically, not on the running turn's own channel, even for a
    desk-originated turn.

    Never raises: an unconfigured/failed DM, or a timeout with no reply,
    both degrade to a plain string telling the model to proceed on its own
    best assumption rather than blocking the turn indefinitely.
    """
    global _turn_clarify_asked
    if _turn_clarify_asked:
        return (
            "Error: you already asked one clarifying question this turn -- "
            "proceed with your best judgment instead of asking again."
        )
    _turn_clarify_asked = True

    if _clarify_notifier is None:
        return _owner.say("Can't reach «owner» right now (Discord isn't configured) -- proceed with your best assumption.")
    # A07: watermark BEFORE the question goes out. Anything already queued was sent
    # before the owner could have seen the question, so it cannot be the reply -- it
    # stays queued and steers the work in flight instead.
    asked_after = steering.latest_seq(steering.DM)
    try:
        sent = await _clarify_notifier(f"Quick question: {question}")
    except Exception as e:
        log.debug("engine: clarifying-question DM failed: %s", e)
        sent = False
    if not sent:
        return "Couldn't send the question (Discord unavailable) -- proceed with your best assumption."

    answer = await steering.await_reply(steering.DM, _CLARIFY_WAIT_SECONDS,
                                        after_seq=asked_after)
    if answer is None:
        return (
            _owner.say(f"No reply within {int(_CLARIFY_WAIT_SECONDS)}s -- proceed with your best assumption, "
            "and say what you assumed in your final answer so «owner» can correct it if you guessed wrong.")
        )
    _policy.note_words(answer)            # P1: his reply is his word for a later "he asked" check
    return _owner.say(f"«Owner» replied: {answer}")


TOOL_DISPATCH = {
    "web_browse":            web_browse,
    "fetch_page":            fetch_page,
    "gmail_action":          gmail_action,
    "calendar_action":       calendar_action,
    "spotify_action":        spotify_action,
    "notion_action":         notion_action,
    "vault_write":       vault_write,
    "vault_read":        vault_read,
    "vault_send":        _verbatim.vault_send,
    "vault_list":        _vault_list,
    "vault_share":       vault_share,
    "vault_write_queue": vault_write_queue,
    "skill":             _skills.skill,
    "memory_search":     memory_search,
    "correct_memory":    correct_memory,
    "memory_timeline":   memory_timeline,
    "get_self_model":        _get_self_model,
    "code_graph":            code_graph,
    "run_in_background":      background.run_in_background,
    "web_research":           background.web_research,
    "check_background_tasks": background.check_background_tasks,
    "research_queue":         research.research_queue,
    "schedule_reminder":      chronos.schedule_reminder,
    "schedule_night_task":    chronos.schedule_night_task,
    "schedule_task":          chronos.schedule_task,
    "watch_action":           watches.watch_action,
    "standing_instruction":   instructions.standing_instruction,
    "forecast":               forecasts.forecast,
    "person_profile":         people.person_profile,
    "week_plan":              weekplan.week_plan,
    "portfolio":              _portfolio.portfolio,
    "ask_clarifying_question": _ask_clarifying_question,
    "goal":                   _goals.goal,
}


# F1 (2026-09-08) removed the screen/mouse-control tools that used to be withheld
# on the Discord channel (computer_action, take_screenshot, read_screen_text) --
# JARVIS is now server-resident with no screen or input devices on any channel,
# so there is nothing left to restrict here. Discord has full tool parity with
# the desk channel. Kept as a function (rather than every caller reading TOOLS
# directly) so a future channel-specific restriction has one place to land.
def tools_for_channel(channel: str) -> list[dict]:
    """The tool schema list a channel's model calls are allowed to see. `goal` is left out
    while goals are off (JARVIS_GOALS, OpenRouter mode): a tool he cannot use would only be
    offered and refused. Stable for the life of the process, so the cached prefix holds."""
    hidden = set() if _goal_runner.configured() else {"goal"}
    return [t for t in TOOLS if t["name"] not in hidden] if hidden else TOOLS


# Hard ceiling on tool-loop iterations per turn (both providers). A model stuck
# re-calling a failing tool would otherwise loop forever, burning tokens with no
# bound. 12 is generous — real multi-step tasks rarely exceed 6-7 loop turns.
_MAX_TOOL_ITERATIONS = 12

_LOOP_CAP_MESSAGE = (
    "I had to stop there — that task took more tool steps than I allow myself "
    "in one turn. Ask me to continue and I'll pick it up from where I left off."
)

_REASONING_LEAK_FALLBACK = (
    "Sorry, I didn't get a real answer back that time — can you ask again?"
)

# The recovery call after a thinking-tier turn ends with no answer (see _chat_compat).
_DIRECT_ANSWER_NUDGE = (
    _owner.say("[System note -- not from «owner»: your previous attempt produced no answer (it ran out "
    "of room, or went round in circles, while still thinking). Answer «his» last message now, "
    "directly and completely, from what you already have. If something in it makes no sense, "
    "ask «him» about it in one short sentence. Do not narrate your process or describe what "
    "you are about to do.]"))

# 20 October 2026: a voice note transcribed as "St. Wam" sent one call into six and a half
# minutes of reasoning with no answer, until the server was restarted. A call whose reasoning
# repeats a passage word for word, or runs past _THINK_LIMIT_S with no answer and no tool
# call, is stopped (finish_reason "loop") and gets the same one direct-answer call as a
# thought cut off at the output limit.
_LOOP_WINDOW = 150         # chars of the newest reasoning looked for earlier in it
_LOOP_REPEATS = 3          # ...found this many times in all = going round in circles
_LOOP_CHECK_EVERY = 1500   # chars of new reasoning between checks
_THINK_LIMIT_S = 180.0     # the largest measured call took ~2 minutes (30,591 tokens)


def _reasoning_repeats(reasoning: str) -> bool:
    tail = reasoning[-_LOOP_WINDOW:]
    return len(tail) == _LOOP_WINDOW and reasoning.count(tail) >= _LOOP_REPEATS
_NO_ANSWER_FALLBACK = (
    "I ran out of room working through that before I could give you an answer. Ask me "
    "again, or narrow it down and I'll answer that part first.")
# H4: what the running turn on each channel is doing, for the Discord progress watchdog
# (discord_bridge). Written by _chat_compat; read by progress_state(). One turn runs at a
# time (the pipeline lock), so one record per channel is enough; `started` lets a reader
# ignore a record left by an earlier turn.
turn_progress: dict[str, dict] = {}


def progress_state(channel: str, since: float) -> tuple[str, float | None]:
    """(one plain progress line, when the turn last told the owner something itself).

    The second value is the plan notice's time, so the watchdog can keep its own lines
    at least two minutes away from it."""
    minutes = int(max(0.0, time.time() - since) // 60)
    when = f"{minutes} min in" if minutes >= 1 else "under a minute in"
    p = turn_progress.get(channel)
    if not p or p["started"] < since - 1:
        return f"Still working on it ({when}).", None
    announced = p.get("announced_at")
    if p["synthesizing"]:
        return f"Writing it up now ({when}).", announced
    if p["plan"]:
        n = min(p["step"] + 1, len(p["plan"]))
        label = p["plan"][n - 1][:80]
        return f"Still on it: step {n} of {len(p['plan'])}, {label} ({when}).", announced
    k = p["tool_calls"]
    if k:
        return (f"Still on it: {k} lookup{'s' if k != 1 else ''} done so far ({when}).",
                announced)
    return f"Still thinking this one through ({when}).", announced


# H1: the last call of a turn that must end now -- its output budget is nearly spent, or
# this is the last tool step it is allowed. Tools are withheld on that call, so it can
# only answer (live-verified on Together, Novita and Parasail: a request with tool calls
# in its history and no tools offered is accepted and answered).
_SYNTHESIZE_NOW_NUDGE = (
    _owner.say("[System note -- not from «owner»: this is your last step for this request. Answer «his» "
    "message now from what you already have. If something is still unchecked, say so in a "
    "sentence rather than stopping short. Do not describe what you would do next.]"))
# H1: an answer cut off by the output limit is finished by one more call, not sent broken.
_CONTINUE_ANSWER_NUDGE = (
    _owner.say("[System note -- not from «owner»: your answer above was cut off by the output limit. "
    "Continue it exactly where it stopped, mid-sentence if needed, starting with a space if "
    "the cut fell between two words. Do not repeat anything "
    "already written and do not start over.]"))

# Progress a capped tool loop leaves behind, so a literal "Continue" can
# actually honor what _LOOP_CAP_MESSAGE promises instead of blindly
# restarting. Before this, the message was a false promise: the tool-call
# transcript (which searches were tried, which results came back) lives only
# in that one _chat_compat call's local `oai_messages`
# and is discarded the moment chat() returns -- _append_history only keeps
# the flat final (user_text, full_response) pair per turn, never the
# sub-turn tool exchanges. Live symptom (2026-09-03): a Discord research task
# hit the cap, "Continue" re-ran the exact same web searches with no memory
# of having already tried them, hit the cap again identically, and would
# have kept doing so indefinitely. Delivered exactly once into the next
# turn's volatile system block via personality._WHERE_LEFT_OFF_FRAME, same
# pop-once pattern as background.pop_completed_notices()/
# research.pop_deferred_mentions()/chronos.pop_pending_mentions().
_pending_progress: dict[str, str] = {}

_PROGRESS_FRAME = (
    "[You hit your tool-step limit on the previous turn before finishing. "
    "This is what you'd actually already tried — use it to pick up for real "
    "instead of repeating the same attempts. Don't mention this note itself, "
    "just continue naturally from where it says you left off.]"
)


def pop_pending_progress(channel: str) -> str:
    """Format-and-clear the progress note left by a capped tool loop for
    `channel`, '' if there is none. Delivered exactly once, same contract as
    background.pop_completed_notices()."""
    note = _pending_progress.pop(channel, "")
    return f"{_PROGRESS_FRAME}\n\n{note}" if note else ""


# Anti-confabulation guard (2026-09-08 incident: JARVIS told the owner a night
# task was queued and a queue entry was recorded when neither had actually
# happened -- "Both done" with no matching tool call that turn). Streaming
# TTS means a false claim already spoken this turn can't be un-said (see
# personality.py's is_persistence_request() for the pre-turn half: a nudge
# injected into THIS turn's prompt before it streams). This is the other
# half -- caught after the fact and corrected on the very NEXT turn, same
# delivered-once contract as _pending_progress above and
# background.pop_completed_notices()/chronos.pop_pending_mentions().
#
# Deliberately narrow: only the tools that actually persist something
# The owner was told about, not every tool JARVIS has.
_PERSISTENCE_TOOLS = frozenset({
    "schedule_night_task", "schedule_reminder", "schedule_task", "vault_write",
    "vault_write_queue", "skill", "notion_action", "watch_action", "portfolio",
    "standing_instruction", "forecast", "correct_memory", "person_profile", "week_plan",
    "goal",
})

_pending_confab_check: dict[str, str] = {}


# Claims in the REPLY that only a successful tool call can make true. The guard above
# keys on the owner's wording, so it misses a save he asked for in any other words --
# live (2026-09-23): asked to make a Spotify habit a standing rule, JARVIS replied
# "Confirmed -- the rule is saved as a standing instruction" with no tool call at all;
# /rules was empty, and when pressed he invented a tool error ("No standing instructions
# database found") that exists nowhere in the code. The store itself worked. So the
# claim is checked against what actually succeeded this turn, before the reply goes
# out: on Discord the reply is only sent once chat() returns, so a false "Confirmed"
# never reaches him.
_CLAIM_PATTERNS: dict[str, re.Pattern] = {
    "portfolio:record_trade": re.compile(
        r"\b(recorded|saved|logged|added|stored)\b[^.\n]{0,45}\b(your |the |this |my )?(position|holding|trade)s?\b"
        r"|\b(position|holding|trade)s?\b[^.\n]{0,30}\b(is|are|was|were|has been|have been) (saved|recorded|logged|stored)\b", re.I),
    "portfolio:thesis": re.compile(
        r"\b(saved|recorded|revised|closed|added|stored)\b[^.\n]{0,35}\b(thesis|theses)\b"
        r"|\b(thesis|theses)\b[^.\n]{0,25}\b(is|was|has been|are|were) (saved|recorded|revised|closed|added|stored)\b", re.I),
    "portfolio:add_condition": re.compile(
        r"\b(saved|recorded|added|stored)\b[^.\n]{0,35}\bcondition\b"
        r"|\bcondition\b[^.\n]{0,25}\b(is|was|has been) (saved|recorded|added|stored)\b", re.I),
    "portfolio:correct_trade": re.compile(
        r"\b(cancelled|canceled|voided|corrected|fixed|removed|deleted)\b[^.\n]{0,40}\b(position|holding|trade|entry)\b"
        r"|\b(trade|entry)\b[^.\n]{0,25}\b(is|was|has been) (cancelled|canceled|voided|corrected|fixed|removed)\b", re.I),
    "portfolio:confirm_split": re.compile(
        r"\b(recorded|confirmed|applied|logged|added)\b[^.\n]{0,35}\bsplit\b"
        r"|\bsplit\b[^.\n]{0,25}\b(is|was|has been) (recorded|confirmed|applied|logged)\b", re.I),
    "correct_memory": re.compile(
        r"\b(false (fact|memory|claim)|memory correction|misattributed claim)\b"
        r"[^.\n]{0,50}\b(suppressed|saved|corrected|fixed|won't resurface)\b"
        r"|\b(suppressed|saved|corrected|fixed)\b[^.\n]{0,35}"
        r"\b(false (fact|memory|claim)|memory correction|misattributed claim)\b", re.I),
    "standing_instruction": re.compile(
        r"\b(saved|stored|logged|recorded|added)\b[^.\n]{0,40}\b(standing (rules?|instructions?)|as a (standing )?rule|the rule)\b"
        r"|\b(standing (rule|instruction)|the rule)\b[^.\n]{0,30}\b(is|has been|was) (saved|stored|logged|recorded|in place)\b"
        r"|\bstanding (rules?|instructions?)\b[^.\n]{0,15}\b(saved|stored|logged|recorded|added|in place)\b",
        re.I),
    # A subject closed to research. Live (20 October), with no tool call either time: "I've now
    # closed it properly" and "I've marked it closed so it can never be picked up again".
    "research_queue:close": re.compile(
        r"\bI(?:'ve| have)?\s+(?:now\s+|just\s+|already\s+)?(?:closed|vetoed)\s+"
        # (not a bare "closed that": "I've closed that loop" -- 27 September, about an email)
        r"(?:it|(?:the|that|this) (?:note|subject|topic|research))\b"
        r"|\bmarked (?:it|that|this|the \w+(?: note)?)\s+(?:as\s+)?closed\b"
        r"|\b(?:note|subject|topic)\b[^.\n]{0,25}\b(?:is|was|has been)\s+(?:now\s+)?(?:closed|marked closed)\b",
        re.I),
    # A skill "saved" or "retired" needs that action to have succeeded this turn: opening one
    # with view is reading it, not saving it.
    "skill:save": re.compile(
        r"\b(saved|stored|recorded|wrote|written|made|added|kept)\b[^.\n]{0,30}\b(as|into|to) (a|the|your|my) skills?\b"
        r"|\b(saved|wrote|created|added) (a|the|that|this|it as a) (new )?skill\b"
        r"|\bskill\b[^.\n]{0,30}\b(is|was|has been|is now) (saved|written|created|stored|in place)\b", re.I),
    "skill:patch": re.compile(
        r"\b(updated|changed|edited|tweaked|amended)\b[^.\n]{0,30}\bskill\b"
        r"|\bskill\b[^.\n]{0,30}\b(is|was|has been|is now) (updated|changed|edited)\b", re.I),
    "skill:retire": re.compile(
        r"\bretired\b[^.\n]{0,30}\bskill\b"
        r"|\bskill\b[^.\n]{0,30}\b(is|was|has been) retired\b", re.I),
    # G1a: "I've started a goal" needs a goal start that succeeded this turn.
    "goal:start": re.compile(
        r"\b(started|launched|kicked off|set up|created|begun)\b[^.\n]{0,30}\bgoal\b"
        r"|\bgoal\b[^.\n]{0,30}\b(is|has been|was) (started|running|underway|live|set up)\b", re.I),
    "watch_action": re.compile(
        r"\bwatch\b[^.\n]{0,25}\b(is |has been )?(set|saved|added|live|active|in place)\b"
        r"|\bset (up )?a watch\b", re.I),
    "schedule_reminder": re.compile(
        r"\breminder\b[^.\n]{0,25}\b(is |has been )?(set|scheduled|saved)\b"
        r"|\bscheduled (a|the|your) reminder\b", re.I),
    "forecast": re.compile(
        r"\b(logged|recorded|tracked|saved)\b[^.\n]{0,30}\b(as a|the|this|that|it as a) forecast\b"
        r"|\bforecast\b[^.\n]{0,20}\b(is |has been )?(logged|recorded|on the record)\b", re.I),
    "person_profile": re.compile(
        r"\b(added|saved|recorded|noted|put|wrote|updated|filed)\b[^.\n]{0,40}"
        r"\b(to|in|on|into) (his|her|their|the|[A-Z][\w-]*'s) profile\b"
        r"|\b(started|created|opened|made) (a|the|his|her|their) profile\b"
        # live: "Done, Sofia is in his profile now under Relationships." slipped through
        r"|(\b(is|are|goes|went)|'s) (now |already )?(in|on|into) (his|her|their|the|[A-Z][\w-]*'s) profile\b"
        r"|\bprofile\b[^.\n]{0,25}\b(is |has been )?(created|updated|saved|started)\b", re.I),
    "schedule_task": re.compile(
        r"\b(scheduled|timed) (task|check|run|report)s?\b[^.\n]{0,30}\b(set|scheduled|saved|in place|live|queued)\b"
        r"|\b(checks|tasks|runs|reports|routines)\b[^.\n]{0,15}\b(are|is|have been|has been) (all )?(now )?"
        r"(scheduled|set( up)?|in place|queued|lined up)\b"
        r"|\b(scheduled|set up|lined up) (\w+ ){0,3}(checks|tasks|runs|reports|routines)\b"
        # a routine is a scheduled task that follows a skill
        r"|\broutine\b[^.\n]{0,25}\b(is |has been )?(set|set up|scheduled|in place|live)\b"
        r"|\b(set up|scheduled|made) (it |that |this )?(as )?(a|the|your) routine\b", re.I),
    # Replayed live (5 October): "the five portfolio closes are queued for tonight at 22:20"
    # (describing the ledger's own close fetches) was called a night-task claim, and the
    # correction pass apologised for an error that was not one. Only "I queued it" counts.
    "schedule_night_task": re.compile(
        r"\bnight task\b[^.\n]{0,25}\b(is |has been )?(queued|scheduled|saved)\b"
        r"|\bqueued (it|that|this|them) for tonight\b"
        r"|\bI(?:'ve| have)? (?:just )?queued\b[^.\n]{0,40}\bfor tonight\b", re.I),
    # P1: an email "sent" must have gone through a real send this turn. Keyed per action
    # ("gmail_action:send_email", recorded by _exec_tool) because reading mail also succeeds
    # as gmail_action. A send that became an approval request is NOT a send, and "message"
    # is left out on purpose: "I sent you a message" is usually the Discord reply itself.
    "gmail_action:send_email": re.compile(
        r"\b(I|I've|I have|I just|just|already)\s+(emailed|e-mailed|mailed)\b"
        r"|\b(sent|emailed|e-mailed|mailed)\b[^.\n]{0,20}\b(the|your|that|this|an?) (email|e-mail|mail)\b"
        r"|\b(email|e-mail|mail)\b[^.\n]{0,25}\b(is|was|has been|went|got) (sent|out|delivered)\b"
        r"|\b(email|e-mail) (sent|delivered)\b"
        r"|\b(email|e-mail|mail|it)(?:'s| is) on (its|the) way\b", re.I),
    # P3: "it's in your calendar" needs a write that succeeded: applying the plan, changing or
    # removing a block, undoing, or a direct calendar event. Reading or proposing is not writing.
    "week_plan:write": re.compile(
        r"\b(put|added|wrote|written|applied|saved|scheduled|blocked|booked)\b[^.\n]{0,40}"
        r"\b(in|into|on|to) (your|the|his|her|their) calendar\b"
        r"|\b(plan|week|gym|blocks?)\b[^.\n]{0,30}\b(is|are|has been|have been|now)\b[^.\n]{0,12}"
        r"\b(in|on) (your|the|his|her|their) calendar\b"
        # ("Moved. Thursday's gym now runs ..." -- the verb can end its own sentence)
        r"|\b(moved|rescheduled|shifted|cancelled|canceled|removed|deleted)\b[^.\n]{0,30}"
        r"\b(gym|workout|session|work block|block)\b"
        r"|\b(moved|rescheduled|shifted|cancelled|canceled|removed|deleted)\b[.:,]\s+(?:\w+'s\s+|the\s+|your\s+)?"
        r"(gym|workout|session|work block|block)\b"
        # live (2026-09-30): "Undone. Thursday's gym is back to 17:30" with no undo call, 4 of 5 times
        r"|\b(undone|undid)\b"
        r"|\b(reverted|rolled back|put back|restored)\b[^.\n]{0,40}\b(gym|workout|block|plan|week|calendar|version)\b"
        r"|\b(gym|workout|session|block)\b[^.\n]{0,30}\bis back (at|to|on)\b", re.I),
    # P4: "marked the gym as done" needs a report that succeeded: an outcome is a fact in the record,
    # and only the tool writes it.
    "week_plan:report": re.compile(
        r"\b(marked|logged|recorded|noted)\b[^.\n]{0,40}\b(as (?:done|skipped|partly|missed|moved|unknown)"
        r"|done|skipped|partly)\b", re.I),
    # H4: a note "sent" must have gone through vault_send, not been retyped.
    "vault_send": re.compile(
        r"\b(sent|posted|dropped|attached)\b[^.\n]{0,40}\b(the|your|that|this) "
        r"(note|file|queue|schedule|research trail)\b"
        # ("sent from/by/via ..." says where something came from, not that it was just sent)
        r"|\b(note|file)\b[^.\n]{0,25}\b(is |has been )?(sent(?!\s+(from|by|via)\b)|attached|"
        r"in (the|this) (chat|conversation))\b",
        re.I),
}
# "I sent it" says nothing about WHAT was sent (a note, a Discord message, an email), so it only
# counts as an email claim when the reply is plainly about an email.
_WEAK_SENT = re.compile(r"\b(I|I've|I have|I just|just|already)\s+sent\s+(it|that|this|them)\b"
                        r"|\bsent (just now|a (moment|minute) ago)\b", re.I)
_EMAIL_WORD = re.compile(r"\b(e-?mail|mail)\b", re.I)
# A profile decline. Live (2026-09-26, keyword fallback): "not interested, don't make a
# profile for him" got "I've recorded the decline so I won't ask again" with no tool call.
# The "won't ask again" form only counts in a reply that mentions a profile.
_DECLINE_CLAIM = re.compile(
    r"\b(?P<recorded>recorded|logged|saved|noted|filed) (the|your|that|this) decline\b"
    r"|\b(won't|will not|never|not going to) (ask|offer|suggest)\b[^.\n]{0,30}\bagain\b", re.I)
# "The tool came back: …" in a turn where no tool ran at all is an invented result.
_TOOL_REPORT = re.compile(
    r"\b(the tool|the (add|save|list|write) (call|handler)|tool call|verbatim return)\b"
    r"[^.\n]{0,20}\b(came back|returned|replied|said|says|emitted|was)\b", re.I)
# "I checked" in a turn where no tool ran is a check that did not happen. Live (20 October):
# "I checked, and you're right -- there is no sent email. Nothing in the mailbox", and "read from
# the tools rather than remembered: three rules", both with no tool call that turn.
_CHECK_CLAIM = re.compile(
    r"\bI(?:'ve| have)?\s+(?:just\s+|now\s+|already\s+|also\s+)?(?:double-)?"
    r"(?:checked|verified|looked (?:at|in|into|through))\b"
    r"|(?:^|[.!?]\s+)(?:Checked|Verified)\b"
    r"|\b(?:read|checked)\s+(?:from|with|in|against)\s+the\s+tools?\b", re.I | re.M)
_NEGATION = re.compile(r"\b(not|never|no longer|nothing)\b|n't\b", re.I)
# "The ledger is empty" says what the ledger holds, so it is checked against the ledger itself
# (_unbacked_claims, `ledger`). Live (5 October): "good night" got "the ledger is empty ... say the
# five lines again" one turn after the same conversation quoted his SPCX position from the ledger.
# Nothing read it that turn; the likely source is a note left by the 3 October session, whose reply
# had wrongly said nothing was recorded (consolidation wrote such notes in 5 of 5 replays).
_LEDGER_DENIAL = re.compile(
    r"(\b(ledger|portfolio|book)\b[^.\n]{0,15}(\bis|'s) (still |now |currently )?(empty|blank)\b"
    r"|\bnothing (is |was )?(recorded |stored )?in (the|your|my) (ledger|portfolio)\b"
    r"|\bno (positions|holdings|trades)\b[^.\n]{0,30}\b(were|was|are|have been|got) "
    r"(recorded|saved|stored|in the ledger)\b"
    r"|\b(positions|holdings|trades|lines)\b[^.\n]{0,30}\b(were never|were not|weren't|are not|aren't|"
    r"was not|wasn't|never got|never made it)\b[^.\n]{0,15}\b(recorded|saved|stored|in(to)? the ledger)\b"
    # replayed (5 October): "the five positions are still unrecorded", "the five lines still
    # aren't in the ledger", "the five lines when you're up -- one sentence, and they're in"
    r"|\b(positions|holdings|trades|lines)\b[^.\n]{0,20}\b(still )?unrecorded\b"
    r"|\b(send|give|say|resend|re-send)\b[^.\n]{0,15}\b(the |those |all )?(five |5 )?(lines|positions|holdings)\b"
    r"[^.\n]{0,15}\bagain\b"
    r"|\b(the )?(five|5) lines\b[^.\n]{0,25}\b(when you're up|in the morning|tomorrow)\b)"
    # "nothing in the ledger for TTWO" is about one stock, which the holdings list cannot settle
    r"(?!\s+(for|about|on|under)\s)",
    re.I)
_LEDGER_READS = frozenset({"portfolio:show", "portfolio:history", "portfolio:context"})
# Actions that only read. Their success is recorded as "tool:action" (for _STATE_READS), never
# as the bare tool name that backs a claim of having saved, set or scheduled something.
_READ_ACTIONS = frozenset({
    "list", "show", "read", "view", "status", "history", "gaps", "record", "stats", "search",
    "list_events", "list_inbox", "search_emails", "read_email", "free_busy", "list_tasks",
    "now_playing", "recent", "liked", "top", "devices"})
# A promise or an offer is not a claim that something was saved. Live (2026-09-27): "I said
# I'd put it on his profile -- that was premature" drew the correction note.
_INTENT = re.compile(r"\b(I'd|I'll|I will|I would|I can|I could|shall I|should I|want me to|"
                     r"let me|going to)\b", re.I)
# Reporting what was said before is not saying it again. Live (3 October): "I told you the five
# positions were recorded. They were not." was itself called a claim, and drew the note.
# ("I was about to tell you the ledger was empty. It isn't." -- a corrected reply, 5 October)
_REPORTED = re.compile(r"\b(I told you|I said|I claimed|I wrote|I'd said|I had said|"
                       r"about to (tell you|say))\b", re.I)
# "a message about scheduled runs", "the recorded trades": a participle used as an adjective
# names a thing, it does not claim having just done it (live, 3 October). Only when the match
# starts with the participle and has no state verb: "your reminder is set" stays a claim.
_NOUN_BEFORE = re.compile(r"\b(about|of|the|your|his|her|my|any|those|these|their|its|for|with|on)\s+$", re.I)
_PARTICIPLE_START = re.compile(r"^(\w+ed|set|lined)\b", re.I)
_STATE_VERB = re.compile(r"\b(is|are|was|were|has been|have been|now|went|got)\b", re.I)

# While the owner has me paused, replies naturally say what is on hold ("scheduled and night
# tasks are on hold"). Live (2026-09-30): 2 of 5 answers to "are you still doing your own
# research?" were called claims that something was scheduled, and one was rewritten into
# "Correction before anything else...". A sentence that says something is held is describing
# the pause, not claiming a save -- only while a pause is actually in force.
_HOLD_WORDS = re.compile(
    r"\b(on hold|paused?|held|hold|resume|stopped|stops?|waits?|waiting|"
    r"won't (?:run|fire|start|check)|not (?:running|firing|checking|starting))\b", re.I)
_SENTENCE_END = re.compile(r"[.!?\n]")
# "no scheduled tasks", "without any checks": the thing is said to be ABSENT. A bare "no" is
# not in _NEGATION on purpose ("No problem -- the reminder is set" is still a claim), so only
# a "no" directly in front of the matched words counts.
_ABSENT = re.compile(r"\b(no|zero|without)\s+(?:any\s+)?$", re.I)


def _in_quotes(text: str, pos: int) -> bool:
    """True when `pos` sits inside quotation marks on its line. Quoting what was said is
    reporting it: live (20 October), JARVIS explained that his earlier '"I've now closed it
    properly"' had no tool call behind it, the check read the quote as a new claim, and the
    correction pass that followed redid the whole turn."""
    line = text[text.rfind("\n", 0, pos) + 1:pos]
    return line.count('"') % 2 == 1 or line.count("\u201c") > line.count("\u201d")


def _sentence_around(text: str, m: "re.Match") -> str:
    """The sentence (or line) that contains match `m`."""
    start = max((text.rfind(c, 0, m.start()) for c in ".!?\n"), default=-1) + 1
    end = _SENTENCE_END.search(text, m.end())
    return text[start:end.start() if end else len(text)]


# "I've set the reminder for 9": the patterns above want a noun before the verb, a spoken
# sentence is held on the bare first-person verb as well.
_DONE_CLAIM = re.compile(
    r"\bI(?:'ve| have)?\s+(?:just\s+|now\s+|already\s+)?(?:set|scheduled|saved|recorded|logged|"
    r"stored|added|sent|emailed|created|booked|queued|closed|noted|updated|deleted|removed|"
    r"cancel+ed|moved|marked)\b", re.I)


def speakable(sentence: str) -> bool:
    """May this sentence be spoken as it is written, before the reply check has run? False
    when any claim pattern (_CLAIM_PATTERNS, a bare "I sent it" about an email, a decline,
    a ledger denial) matches anywhere in it: that sentence waits for the checked answer.
    Conservative on purpose -- none of the checks' exemptions apply, a held sentence is
    only spoken a little later."""
    text = sentence or ""
    if _DONE_CLAIM.search(text) or any(p.search(text) for p in _CLAIM_PATTERNS.values()):
        return False
    if _EMAIL_WORD.search(text) and _WEAK_SENT.search(text):
        return False
    return not (_DECLINE_CLAIM.search(text) or _LEDGER_DENIAL.search(text))


# Tools that only REPORT what already exists. After one of them succeeds, a reply that describes
# the schedule ("the night task is queued", "the NVDA watch is live") is describing what it just
# read, not claiming to have set anything up. Live (2026-09-30, P2): the activity view says
# "night task queued for the quiet hours", the model repeated it, the check called its own
# report a lie in a third of the replies, and the correction pass told the owner "I've now queued
# it properly" -- for a task that was already there. Not applied when he asked to save
# something: a request to schedule must still be backed by the tool that schedules.
_STATE_READS: dict[str, frozenset[str]] = {
    "portfolio:show": frozenset({"portfolio:record_trade", "portfolio:thesis", "portfolio:add_condition",
                                 "portfolio:correct_trade", "portfolio:confirm_split"}),
    "portfolio:history": frozenset({"portfolio:record_trade", "portfolio:thesis", "portfolio:add_condition",
                                    "portfolio:correct_trade", "portfolio:confirm_split"}),
    # The ledger block put in front of him this turn (chat() adds this marker). Live, 3 October:
    # asked "how many NVDA shares do I have now?", JARVIS answered from it, "the split was
    # recorded", and the check called that an unbacked claim.
    "portfolio:context": frozenset({"portfolio:record_trade", "portfolio:thesis", "portfolio:add_condition",
                                    "portfolio:correct_trade", "portfolio:confirm_split"}),
    "check_background_tasks": frozenset({"schedule_night_task", "schedule_reminder",
                                         "schedule_task", "watch_action", "forecast"}),
    # Describing the plan after reading it ("the gym is in your calendar on Thursday") is not a claim.
    "week_plan:show": frozenset({"week_plan:write", "week_plan:report"}),
    "week_plan:sync": frozenset({"week_plan:write"}),
    # The skills list is in every prompt, so "you have a stock-research skill saved" reads it.
    "skill:list": frozenset({"skill:save", "skill:patch", "skill:retire", "schedule_task"}),   # it shows routines
    "skill:catalog": frozenset({"skill:save", "skill:patch", "skill:retire"}),
    "research_queue:status": frozenset({"research_queue:close"}),
}

# What else backs a claim whose pattern is keyed by a made-up name: a plan write, or a direct edit.
_ALSO_BACKED_BY: dict[str, frozenset[str]] = {
    "portfolio:thesis": frozenset({"portfolio:set_thesis", "portfolio:revise_thesis",
                                  "portfolio:close_thesis"}),
    # A correction records the trade that replaces the wrong one, and confirming a split records
    # it as a trade ("Recorded: the split is trade #3" -- live, 3 October, flagged before this);
    # a split can also be entered by hand.
    "portfolio:record_trade": frozenset({"portfolio:correct_trade", "portfolio:confirm_split"}),
    "portfolio:confirm_split": frozenset({"portfolio:record_trade"}),
    "week_plan:write": frozenset({"week_plan:apply", "week_plan:change", "week_plan:remove",
                                  "week_plan:undo", "calendar_action:create_event",
                                  "calendar_action:update_event", "calendar_action:delete_event",
                                  # its "undone" pattern: undoing a skill change is an undo too
                                  "skill:undo"}),
    # a learned skill is saved by the learn action itself
    "skill:save": frozenset({"skill:learn"}),
    "skill:patch": frozenset({"skill:save", "skill:learn"}),
    "research_queue:close": frozenset({"portfolio:close_thesis", "goal:cancel"}),
}

_CLAIM_CHECK_FRAME = (
    _owner.say("[System check -- not from «owner». Your draft reply {problems}. Calls that succeeded "
    "this turn: {done}; their results stand, so do not repeat or re-check them. If «he» asked for it in "
    "this message, do it now -- call the tool -- and answer from what it returns. If it may "
    "have been done in an earlier turn, do not redo it: check with the tool that reads it "
    "(portfolio show, check_background_tasks, standing_instruction list, week_plan show) and "
    "answer from what that shows -- a refused redo does not mean it is missing. If you cannot "
    "confirm it either way, say plainly that you could not. Do not repeat the earlier claim, "
    "and never describe a tool result that is neither in this turn nor in the tool record of "
    "earlier turns; what that record shows stands, so do not take it back. Keep the rest of your draft as it "
    "was and change only what this check names. Reply to «owner» directly; do not mention "
    "this check.]")
)
# The correction pass fixes one sentence, so it is a short loop, not a new turn: live (20
# October) it ran the full 12 steps on the thinking tier, re-verifying work the turn had done.
_CORRECTION_STEPS = 3


# Scoped to THIS turn: the thing may well exist from an earlier one, and a note saying flatly
# that it was NOT saved told the owner his recorded holdings were missing (live, 3 October).
_VOICE_NOTE = (
    _owner.say("[This message was a voice note, transcribed automatically (English and French). Names, "
    "figures and amounts can be misheard. When what you do depends on one -- a share count, a "
    "price, a time, a recipient -- say the figure back in your reply, and ask first if it "
    "looks unlikely. A word that makes no sense where it stands was probably misheard: ask "
    "«him» what «he» meant in one short sentence rather than working out what it could be.]")
)

# M3: the phone reads this reply aloud. Spoken answers that run long, or carry lists and
# tables, are tiring to listen to (the full text stays on his screen either way).
_SPOKEN_NOTE = (
    _owner.say("[Your reply will be read aloud on «his» phone. Answer the way you would say it: briefly, "
    "in plain sentences, the answer first. Put the answer in your first sentence and keep that "
    "sentence short, so «he» hears it at once. Leave out lists, tables, links and code unless «he» "
    "asked for them; they stay readable on «his» screen.]")
)

_UNBACKED_NOTE = ("\n\n(Correction from my own system check: nothing in this turn confirmed what "
                  "I said was saved, set, scheduled or sent. If it was done earlier, /portfolio, "
                  "/activity or /rules shows it; if not, ask me again and I'll do it properly.)")

# What "nothing was ..." says for a claim about something other than a save.
_CLAIM_NOTHING = {"gmail_action:send_email": "so nothing was sent this turn",
                  "research_queue:close": "so nothing was closed this turn",
                  "week_plan:write": "so nothing was written to the calendar this turn",
                  "week_plan:report": "so nothing was recorded this turn"}


async def _enforce_backed_claims(reply: str, messages: list, rerun, channel: str,
                                 asked_to_save: bool = False, paused: bool = False,
                                 earlier: frozenset[str] = frozenset(),
                                 earlier_tools: bool = False) -> str:
    """One correction pass when the reply claims something no tool call backs.

    `rerun(messages)` runs the provider loop again (same model, tools, channel) and
    returns the new reply, which replaces the old one before it is delivered or stored.
    Bounded to one pass: a claim still unbacked afterwards is logged, not looped on.
    `asked_to_save`: his message read as a request to save or schedule something.
    `paused`: the owner has paused my own work, so sentences about what is held are not claims.
    `earlier`: what succeeded in earlier turns of this session (see _session_ok).
    `earlier_tools`: the tool record of earlier turns was in front of him (tool_record.py).
    """
    def _ledger_for(text):
        return _ledger_holdings() if _LEDGER_DENIAL.search(text or "") else None
    problems = _unbacked_claims(reply, _turn_tools_ok, _turn_tool_calls, asked_to_save, paused, earlier,
                                ledger=_ledger_for(reply), earlier_tools=earlier_tools)
    if not problems:
        return reply
    log.warning("engine: unbacked claim on channel=%s -- %s", channel, "; ".join(problems))
    retry = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": _CLAIM_CHECK_FRAME.format(
            problems="; and ".join(problems),
            done=", ".join(sorted(_turn_tools_ok)) or "none")},
    ]
    try:
        corrected = await rerun(retry) or ""
    except Exception as e:
        log.warning("engine: claim correction pass failed (%s)", e)
        corrected = ""
    if not corrected.strip():
        return reply + _note_for(problems)
    still = _unbacked_claims(corrected, _turn_tools_ok, _turn_tool_calls, asked_to_save, paused, earlier,
                             ledger=_ledger_for(corrected), earlier_tools=earlier_tools)
    if still:
        log.warning("engine: claim still unbacked after correction -- %s", "; ".join(still))
        return corrected + _note_for(still)
    return corrected


# A reply that says something he told the owner about a tool was not real. Live (28 October): "I
# told you twice earlier that the tool had refused your words, and that was not true. Both were
# my assertions, not the tool's return" -- both refusals were in the receipts. Replayed with the
# tool record in front of him, 1 reply in 9 still did it ("my earlier attempts ... did not
# actually run"), so such a sentence is checked against the record before it goes out. Matched
# per sentence together with the one before it ("I told you the ledger wanted X. That was wrong").
_RETRACTION = re.compile(
    r"\b(was|were|is|are)(?:n't| not)\s+(?:actually\s+)?(true|real|accurate)\b"
    r"|\b(that|this|it|both|each|which) (was|were) wrong\b"
    r"|\bmy (own )?(assertions?|invention|story)\b"
    r"|\bnot (the|a) tool'?s?\s+(return|result|answer|words)\b"
    r"|\b(did not|didn't|never) (actually |really )?(run|happen|execute|go through|get made|reach (it|the tool))\b"
    r"|\bnever (actually |really )?(received|ran|happened)\b"
    r"|\b(I|I'd|I had|I have) (invented|made up|fabricated|imagined|confabulated)\b"
    r"|\b(invented|made up|fabricated|confabulated) (them|those|it|that|the (refusals?|results?|errors?))\b"
    r"|\bturned (that|it|this) into a story\b", re.I)
_TOOL_NOUN = re.compile(r"\b(tool|ledger|check|refus\w*|calls?|attempts?|records?|saved?|errors?|"
                        r"receipts?|went through|rejected|declined)\b", re.I)
# About an earlier turn: "The backdate did not go through" reports this turn's own refusal.
_EARLIER = re.compile(r"\b(earlier|before|previous(ly)?|told you|I said|I wrote|I claimed|twice|last (time|turn|"
                      r"message|reply)|the first (one|two|three|attempts?))\b", re.I)

_RECORD_CHECK_FRAME = (
    _owner.say("[System check -- not from «owner». Your draft says that something you told «him» earlier about "
    "a tool was not real: {sentence!r}. Check it against the tool record of earlier turns in your "
    "context, which is written by code from the receipts. If the record shows that call and what "
    "it returned, what you told «him» was true: do not take it back. Correct only what the record "
    "contradicts, such as a cause you guessed at. Keep the rest of your draft as it was. Reply to "
    "«Owner» directly; do not mention this check.]")
)


def _retraction(reply: str) -> str:
    """The first sentence of `reply` that says an earlier tool report was not real, '' if none."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", reply or "") if s.strip()]
    for n, sentence in enumerate(sentences):
        if _RETRACTION.search(sentence) and not _in_quotes(sentence, _RETRACTION.search(sentence).start()):
            window = " ".join(sentences[max(0, n - 1):n + 1])
            if _TOOL_NOUN.search(window) and _EARLIER.search(window):
                return sentence
    return ""


async def _enforce_record(reply: str, messages: list, rerun, channel: str) -> str:
    """One correction pass when the reply takes back an earlier report about a tool while the tool
    record of earlier turns was in front of him (tool_record.py). Bounded to one pass."""
    sentence = _retraction(reply)
    if not sentence:
        return reply
    log.warning("engine: reply takes back an earlier tool report on channel=%s -- %r", channel, sentence[:200])
    retry = messages + [{"role": "assistant", "content": reply},
                        {"role": "user", "content": _RECORD_CHECK_FRAME.format(sentence=sentence[:300])}]
    try:
        corrected = await rerun(retry) or ""
    except Exception as e:
        log.warning("engine: record check pass failed (%s)", e)
        return reply
    return corrected if corrected.strip() else reply


_LEDGER_NOTE = ("\n\n(Correction from my own system check: what I said about the portfolio ledger "
                "being empty does not match the ledger; /portfolio shows what it holds.)")


def _note_for(problems: list[str]) -> str:
    """The line added when a correction pass did not fix the reply: about the ledger when that is
    all that is wrong, since the usual note speaks of saves."""
    if problems and all("portfolio ledger" in p for p in problems):
        return _LEDGER_NOTE
    return _UNBACKED_NOTE


def _ledger_holdings() -> list[str] | None:
    """What the portfolio ledger holds right now ("NVDA 1.14339682", ...): [] when it holds
    nothing, None when it cannot be read."""
    try:
        from decimal import Decimal
        from backend.storage import portfolio as _store
        return [f"{p['symbol']} {p['shares']}" for p in _store.positions()
                if Decimal(str(p.get("shares") or 0)) > 0]
    except Exception as exc:
        log.debug("claim check: ledger unreadable (%s)", exc)
        return None


def _ledger_denials(text: str) -> list["re.Match"]:
    """Claims in `text` that the portfolio ledger is empty or lacks his positions, leaving out
    reported speech ("I told you it was empty") and conditionals ("if it is empty")."""
    out = []
    for m in _LEDGER_DENIAL.finditer(text or ""):
        lead = text[max(0, m.start() - 25):m.start()]
        if not (_in_quotes(text, m.start()) or _REPORTED.search(lead) or re.search(r"\b(if|whether)\b", lead, re.I)):
            out.append(m)
    return out


_MEMORY_SEPARATOR = "\n\n---\n\n"      # how recall joins its past exchanges (memory.recall)


def _drop_contradicted_memory(rec, letter: str):
    """(rec, letter) without the recalled memory that says the ledger is empty while it holds
    positions; each such piece is also suppressed from later recall with a correction, so the
    memory is fixed at its source instead of the reply being fixed every time.

    Live (5 October): "good night" recalled the 3 October goodnight, whose reply had wrongly
    said nothing was in the ledger, and JARVIS repeated it as current. Replayed with that
    exchange recalled, 4 replies in 5 were harmful with no change. With the memory frames
    saying memory is not state, 1 in 5; with the ledger also put beside the memory, 3 drafts
    in 5 still said it: a memory in his own voice outweighed the code-read ledger. So memory
    the ledger contradicts does not reach him at all. Each item is dropped whole, because the
    sentences around the claim carry it too ("They were not."). The correction keeps a past
    exchange, fact or vault passage out of all later recall; a letter file stays as it is until
    the next consolidation writes a newer one."""
    texts = (rec.exchange, rec.vault_passage, rec.facts, letter)
    if not any(_ledger_denials(t) for t in texts):
        return rec, letter
    ledger = _ledger_holdings()
    if not ledger:                     # empty (the claim is true) or unreadable
        return rec, letter
    from dataclasses import replace
    from backend.brain.memory import USER_ID
    from backend.storage import fact_corrections, lifelog
    note = (f"The portfolio ledger held {len(ledger)} positions on {lifelog.local_date()} "
            f"({', '.join(h.split()[0] for h in ledger)}), so a past reply saying it was empty "
            "or missing them was wrong.")
    known = fact_corrections.list_for_user(USER_ID)
    dropped: list[str] = []

    def keep(item: str) -> bool:
        claims = _ledger_denials(item)
        if not claims:
            return True
        for m in claims:
            claim = _sentence_around(item, m).strip()
            if len(fact_corrections._normal(claim)) >= 12 and not fact_corrections.suppresses(claim, known):
                if fact_corrections.add(USER_ID, claim[:400], note) is not None:
                    known.append({"claim": claim, "terms": []})
        dropped.append(item[:80].replace("\n", " "))
        return False

    exchange = _MEMORY_SEPARATOR.join(c for c in rec.exchange.split(_MEMORY_SEPARATOR) if keep(c)) \
        if rec.exchange else ""
    facts = "\n".join(l for l in rec.facts.splitlines() if keep(l)) if rec.facts else ""
    passage = rec.vault_passage if not rec.vault_passage or keep(rec.vault_passage) else ""
    kept_letter = letter if not letter or keep(letter) else ""
    log.warning("memory: %d recalled item(s) said the ledger was empty while it holds %d "
                "positions; left out and suppressed from later recall: %s",
                len(dropped), len(ledger), " | ".join(dropped))
    return replace(rec, exchange=exchange, facts=facts, vault_passage=passage), kept_letter


def _strip_contradicted(text: str, by_line: bool = False) -> str:
    """`text` (the queue, his portrait) without the lines (by_line) or sentences that say the
    ledger is empty while it holds positions, for this turn. These are files the next session's
    consolidation rewrites, under its rule not to record what a tool keeps; until then the
    stale line stays in the file but out of the prompt."""
    if not _ledger_denials(text):
        return text
    ledger = _ledger_holdings()
    if not ledger:
        return text
    if by_line:
        kept = "\n".join(l for l in text.splitlines() if not _ledger_denials(l))
    else:
        kept = text
        for m in reversed(_ledger_denials(text)):
            sentence = _sentence_around(text, m)
            kept = kept.replace(sentence, "", 1)
    log.warning("memory: a note said the ledger was empty while it holds %d positions; that part "
                "was left out of the prompt", len(ledger))
    return kept


def _unbacked_claims(reply: str, tools_ok: set[str], tool_calls: int,
                     asked_to_save: bool = False, paused: bool = False,
                     earlier: frozenset[str] = frozenset(),
                     ledger: list[str] | None = None, earlier_tools: bool = False) -> list[str]:
    """Problems with `reply` given what actually succeeded this turn (and, when his message
    asked nothing to be saved, earlier in this session). Pure. `ledger` is what the portfolio
    ledger holds (None: not read), for a reply that says it is empty. `earlier_tools`: the
    tool record of earlier turns was in front of him, so a tool result he describes in a
    turn that ran no tool can be one of those."""
    problems = []
    described: set[str] = set()
    # A turn that recorded or corrected trades changed the ledger itself: "the ledger was empty,
    # so these went in as openings" is about before (the 3 October probe, replayed, drew a
    # correction pass for exactly that).
    recorded = set(tools_ok) & {"portfolio:record_trade", "portfolio:correct_trade",
                                "portfolio:confirm_split"}
    for m in ([] if recorded else _ledger_denials(reply)):
        # The ledger is the source of truth, so the claim is checked against it, not against
        # what was read: an empty ledger makes it true, positions make it wrong whatever the
        # turn read. Only when the ledger cannot be read does a read this turn decide.
        if ledger:
            problems.append(f"says {m.group(0)!r}, but the portfolio ledger holds {len(ledger)} "
                            f"position(s) right now: {', '.join(ledger[:8])}. Say what it holds; a "
                            "note or memory that says it is empty or missing is out of date")
        elif ledger is None and not (set(tools_ok) & _LEDGER_READS):
            problems.append(f"says {m.group(0)!r} about the portfolio ledger, but nothing read the "
                            "ledger this turn: check it with portfolio show and answer from what it "
                            "shows (a note that says it is empty may be out of date)")
        break
    if not asked_to_save:
        # A save he asks for now must be backed by this turn; describing one is not.
        tools_ok = set(tools_ok) | set(earlier)
        for reader, covered in _STATE_READS.items():
            if reader in tools_ok:
                described |= covered
    for tool, pattern in _CLAIM_PATTERNS.items():
        # A scheduled task also sends a message at its time, so "reminder set" is backed by it.
        if tool in tools_ok or tool in described or (
                tool == "schedule_reminder" and "schedule_task" in tools_ok) or (
                _ALSO_BACKED_BY.get(tool, frozenset()) & tools_ok):
            continue
        for m in pattern.finditer(reply or ""):
            lead = reply[max(0, m.start() - 25):m.end()]
            if _NEGATION.search(lead) or _INTENT.search(lead) or _REPORTED.search(lead):
                continue
            if _in_quotes(reply, m.start()):
                continue
            if _ABSENT.search(reply[max(0, m.start() - 14):m.start()]):
                continue
            if (_PARTICIPLE_START.match(m.group(0)) and not _STATE_VERB.search(m.group(0))
                    and _NOUN_BEFORE.search(reply[max(0, m.start() - 12):m.start()])):
                continue
            if paused and _HOLD_WORDS.search(_sentence_around(reply, m)):
                continue
            problems.append(f"says {m.group(0)!r}, but no {tool} call succeeded this turn, "
                            f"{_CLAIM_NOTHING.get(tool, 'so nothing was saved this turn')}")
            break
    if "gmail_action:send_email" not in tools_ok and _EMAIL_WORD.search(reply or ""):
        for m in _WEAK_SENT.finditer(reply):
            lead = reply[max(0, m.start() - 25):m.end()]
            if not _NEGATION.search(lead) and not _INTENT.search(lead) and not _in_quotes(reply, m.start()):
                problems.append(f"says {m.group(0)!r} about an email, but no gmail_action:send_email "
                                "call succeeded this turn, so nothing was sent")
                break
    if "person_profile" not in tools_ok:
        for m in _DECLINE_CLAIM.finditer(reply or ""):
            # "won't ask again" is itself negative, so the usual window cannot be used;
            # only a negation just before the claim ("I haven't recorded...") counts.
            if (m.group("recorded") or "profile" in (reply or "").lower()) and \
                    not _NEGATION.search(reply[max(0, m.start() - 12):m.start()]) and \
                    not _in_quotes(reply, m.start()):
                problems.append(f"says {m.group(0)!r}, but no person_profile call succeeded "
                                "this turn, so the decline was not recorded")
                break
    # A result from an earlier turn is in the tool record he was given: flagging it as invented
    # would send the correction pass to talk him out of a true report.
    if tool_calls == 0 and not earlier_tools and _TOOL_REPORT.search(reply or ""):
        problems.append("describes a tool result, but no tool ran this turn")
    if tool_calls == 0:
        for m in _CHECK_CLAIM.finditer(reply or ""):
            lead = reply[max(0, m.start() - 25):m.end()]
            if not (_NEGATION.search(lead) or _INTENT.search(lead) or _REPORTED.search(lead)
                    or _in_quotes(reply, m.start())):
                problems.append(f"says {m.group(0).strip(' .!?')!r}, but no tool ran this turn, so "
                                "nothing was checked: read it with the tool now and answer from "
                                "what it returns")
                break
    return problems

_CONFAB_CHECK_FRAME = (
    _owner.say("[System check: your previous reply looked like it was asking you to save, "
    "schedule, or record something, but no matching tool call happened that turn. "
    "If that action was not actually completed, say so plainly now and do it — "
    "don't let «owner» believe something is saved or scheduled that isn't. If it "
    "genuinely was already done earlier, ignore this.]")
)


def pop_pending_confab_check(channel: str) -> str:
    """Format-and-clear the confabulation-check note for `channel`, '' if
    none. Delivered exactly once into the next turn's volatile block."""
    note = _pending_confab_check.pop(channel, "")
    return f"{_CONFAB_CHECK_FRAME}\n\nWhat was asked: {note}" if note else ""


def _summarize_tool_call(name: str, input_: dict) -> str:
    """One short line for the progress log: the query/task/url text if the
    tool call has one, else just the tool name."""
    input_ = input_ or {}
    for key in ("query", "task", "url", "prompt"):
        val = input_.get(key)
        if val:
            return f"{name}({str(val)[:80]!r})"
    return name


def _build_progress_note(plan: list[dict], call_log: list[str]) -> str:
    """Honest summary of what actually happened during a capped tool loop.

    Built from `call_log` -- the REAL tool calls made -- not the plan's
    step_pointer bookkeeping, which advances by one position per iteration
    regardless of which tool the model actually called. That positional
    tracking can't be trusted to reflect reality when the model gets stuck
    retrying one step: in the live incident this fixes, the model called
    web_browse eleven times in a row with reformulated queries, and
    step_pointer/completed_steps would have mechanically marked all four
    plan steps "done" even though code_graph and memory_search never ran.

    Returns '' if no tools were called at all (nothing to report).
    """
    if not call_log:
        return ""
    from collections import Counter
    tool_counts = Counter(c.split("(", 1)[0] for c in call_log)
    tally = ", ".join(f"{name} x{n}" for name, n in tool_counts.most_common())
    parts = [f"Before running out of steps, you called: {tally}."]

    planned_tools = {s["tool"] for s in (plan or []) if s.get("tool")}
    untouched = sorted(planned_tools - set(tool_counts))
    if untouched:
        parts.append(f"You never got to: {', '.join(untouched)}.")

    recent = call_log[-4:]
    parts.append("Most recent attempts: " + "; ".join(recent) + ".")
    return " ".join(parts)


# Per-turn tool stats — reset by chat(); _turn_tools_called says whether a persistence
# claim is backed by a real tool call -- see _PERSISTENCE_TOOLS /
# pop_pending_confab_check below.
# Background tasks use their own executor and never touch these.
_turn_tool_calls:   int = 0
_turn_tools_called: set[str] = set()
# Tools whose call this turn actually SUCCEEDED -- what a claim in the reply is checked
# against (see _unbacked_claims). "Called" is not enough: a rejected or failed call
# saved nothing either.
_turn_tools_ok: set[str] = set()
# What succeeded in EARLIER turns of the current session, per channel: (session_id, keys).
# Live (3 October 2026): the five holdings were recorded, then the reply to "good night"
# said so, the check called that unbacked because nothing was recorded in THAT turn, the
# correction pass told JARVIS to call the tool, the redo was refused (the figures were in
# the previous message), and he told the owner "nothing is in the ledger". A reply to a message
# that asks nothing to be saved may describe what this conversation already saved.
_session_ok: dict[str, tuple[str, frozenset[str]]] = {}


def _earlier_ok(channel: str, session_id: str) -> frozenset[str]:
    held = _session_ok.get(channel)
    return held[1] if held and held[0] == session_id else frozenset()


def _remember_ok(channel: str, session_id: str) -> None:
    _session_ok[channel] = (session_id, _earlier_ok(channel, session_id) | frozenset(_turn_tools_ok))
# Step 1b (2026-09-08) -- "at most one clarifying question per turn." Reset
# alongside the stats above; checked/set by _ask_clarifying_question.
_turn_clarify_asked: bool = False


def _reset_turn_stats() -> None:
    global _turn_tool_calls, _turn_clarify_asked
    _turn_tool_calls = 0
    _turn_clarify_asked = False
    _turn_tools_called.clear()
    _turn_tools_ok.clear()


def _is_error_result(result, tool_name: str = "") -> bool:
    """True when a tool's string result represents a failure.

    A04 (2026-09-22): this used to check only the "Error:"/"Unknown tool:" prefixes, and
    every adapter also has its own older failure vocabulary that never matched -- vault's
    "Not found:", gmail's "Gmail auth failed:", browser's "SEARCH FAILED", and more. All
    of them were being recorded as SUCCESS, which is the roadmap's "failed actions can
    appear successful; later features can consume errors as evidence". The A02 receipt
    layer caught it live: a vault_read of a missing file recorded `ok`.

    Classification now lives in tools/outcomes.py, keyed PER TOOL so a vault note that
    merely quotes "Gmail error:" is not misread as one. `tool_name` is optional so the
    old single-argument call shape still works (falling back to the global prefixes).
    """
    from backend.tools import outcomes

    if not isinstance(result, str):
        return False
    if tool_name:
        failed = outcomes.is_failure(tool_name, result)
        if not failed:
            outcomes.note_unrecognised(tool_name, result)
        return failed
    return result.startswith(("Error:", "Unknown tool:"))


async def _exec_tool(tc: dict, send: WS_Sender) -> tuple[dict, Any, bool]:
    """Execute one tool call. Returns (tc, result, ok).

    ok is False when dispatch raised, the tool is unknown, or the tool returned
    an in-band "Error:" string. A call turned into an approval request (P1) is ok=True
    here -- nothing failed -- but is not recorded as having succeeded. Shared by both provider loops; independent
    calls within one model turn run in parallel via asyncio.gather.
    """
    global _turn_tool_calls
    _turn_tool_calls += 1
    _turn_tools_called.add(tc["name"])
    await send({"type": "tool_call", "tool": tc["name"], "input": tc["input"]})

    # A04 slice 3: capability/validation/dispatch/classification/receipt policy lives in
    # the shared gateway, so background tasks get exactly the same rules. What stays here
    # is genuinely foreground-only: the WS event above and the per-turn counters below.
    # `capabilities=None` -- the live conversation is the unrestricted caller.
    res = await _gateway.execute(
        tc["name"], tc.get("input") or {},
        dispatch=TOOL_DISPATCH,
        invalid_args=tc.get(_validation.INVALID_ARGS_KEY),
        caller=_policy.LIVE,
    )
    if res.ok:
        # Per action too: a Gmail READ succeeding must not back a claim that an email was SENT.
        # And a read never backs a write under the bare name either. Replayed live (5 October):
        # a turn asked to re-add four scheduled checks listed the schedule and re-added nothing,
        # and its successful `schedule_task list` would have backed "four routines are set up".
        # The same held for `standing_instruction list` and "the rule is saved".
        action = (tc.get("input") or {}).get("action")
        if not action or str(action).lower() not in _READ_ACTIONS:
            _turn_tools_ok.add(tc["name"])
        if action:
            _turn_tools_ok.add(f"{tc['name']}:{action}")
    # A call that became an approval request is neither done nor broken: it must not count as
    # backing a "done" claim (above), and must not trigger a replan.
    return tc, res.result, res.ok or res.pending


_RESOLVED_RE = re.compile(r"\bresolved\b", re.IGNORECASE)


def _format_intent_context(queue: str) -> str:
    """Extract active queue items for the [Continuity of Intent] block.

    Resolved entries are stripped by skipping lines that contain the word
    'resolved' (case-insensitive), unless the line is a Markdown header
    (starts with '#'). Word-boundary matching — a plain substring check would
    also strip lines containing 'unresolved', silently dropping exactly the
    open items this block exists to surface.
    """
    if not queue:
        return ""
    active_lines = [
        line for line in queue.split("\n")
        if not (_RESOLVED_RE.search(line) and not line.strip().startswith("#"))
    ]
    return "\n".join(active_lines).strip()


# ---------------------------------------------------------------------------
# Streaming path (OpenRouter, an OpenAI-compatible API)
# ---------------------------------------------------------------------------


def _get_compat_client():
    """The pooled client for the provider (providers.gateway).

    Fails loudly when OPENROUTER_API_KEY is missing (F3, 2026-09-08): an empty key used to
    build a client that failed every turn with a raw 401 and stayed cached after the key
    was fixed. Nothing is cached on failure, so a corrected key works on the next call.
    """
    from backend.providers import gateway
    provider = _provider()
    try:
        return gateway.get_async_client(provider)
    except gateway.ProviderNotConfigured as exc:
        raise RuntimeError(
            f"{provider.api_key_env} is not set -- there is no working model provider. "
            f"Set {provider.api_key_env} and try again."
        ) from exc


def _to_openai_tools(tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def _reasoning_config(provider, use_thinking: bool) -> tuple[float | None, dict | None]:
    """F4 (2026-09-08) -- the ONE place that decides which tier a live-chat call is on,
    for the OpenAI-compatible path. Resolves both the request temperature and the
    extra_body reasoning toggle from (provider, use_thinking) together, so
    _run_stream_openai never has to re-derive the same fact from module-level state on
    its own.

    Before this, `_chat_compat` computed `use_thinking = (model == provider.thinking)`
    while `_run_stream_openai` separately computed `model == OPENROUTER_THINKING` -- the two agreed only because both happened to reduce to
    model identity, a coincidence Step 3 of the upgrade plan (per-call effort/model
    escalation, see `escalation.py` -- landed 2026-09-09) would have broken by design, and
    does now rely on this single derivation staying single: `escalation.resolve()` returns
    `(model, use_thinking, extra_body_override)` together for exactly this reason. The same
    `use_thinking` flag also gates the reasoning-echo requirement and the reasoning-leak
    guard in `_chat_compat` -- if it
    ever drifted from what was actually sent, the leak guard misfires and reads a
    provider's internal reasoning aloud over TTS, a bug this codebase has already shipped
    and fixed once (see the 2026-09-05 hardening note in CLAUDE.md).

    """
    temperature = provider.thinking_temperature if use_thinking else None
    extra_body = provider.thinking_body if use_thinking else provider.no_thinking_body
    return temperature, with_provider_prefs(extra_body, provider)


# H1: output ceiling per live-path call, reasoning and answer combined, by the reasoning
# effort the call actually sends. It used to be 16,000 for every tier, which the thinking
# tier reached on 28 September with no answer. A ceiling is not a cost -- only generated
# tokens are billed -- so these are sized to never cut off a healthy call; the per-turn
# budget below is what bounds spending. 64K and 128K are DeepSeek's own thinking and
# max-effort defaults. Hosts that cap output lower are skipped by OpenRouter itself
# (live-verified: "Filter by Context Length removed baseten/fp8" for a 64K request).
# "default" covers a body with no recognisable effort.
_OUTPUT_LIMITS = {"none": 8000, "low": 32000, "high": 64000, "max": 128000}
_DEFAULT_OUTPUT_LIMIT = 16000

# H1: output tokens one turn may generate across all its calls, and the share at which
# it winds down: remaining calls drop to low effort and the next one is the synthesis.
# ~$0.18 at peak price, ~27 minutes of generation at ~92 tokens/s.
_TURN_OUTPUT_BUDGET = 150_000
_WIND_DOWN_SHARE = 0.8
_MIN_CALL_TOKENS = 8000          # a synthesis always gets at least this much room


def _output_limit(extra_body) -> int:
    return _OUTPUT_LIMITS.get(_llm_calls.effort_of(extra_body), _DEFAULT_OUTPUT_LIMIT)


def _low_effort_body(provider) -> dict | None:
    """The thinking body at low effort."""
    return with_provider_prefs({"reasoning": {"effort": "low"}}, provider)


async def _run_stream_openai(
    client, model: str, messages: list, send: WS_Sender, sentence_queue,
    temperature: float | None = None, tools: list[dict] = TOOLS,
    extra_body: dict | None = None, *, purpose: str = "chat",
    iteration: int | None = None, plan_active: bool = False, max_tokens: int | None = None,
) -> tuple[str, str, list[dict]]:
    """Stream one live-chat turn through OpenRouter (an OpenAI-compatible API).

    Returns (text, reasoning_content, tool_calls).
    reasoning_content must be echoed back in the assistant message when a tool call loop
    continues (DeepSeek's requirement on the thinking tier).

    `extra_body` is resolved by the caller (see `_reasoning_config` -- F4, 2026-09-08) and
    passed down explicitly; this function does not re-derive which tier is active from
    model identity on its own.
    """
    text = ""
    reasoning_content = ""
    pending_text = ""
    finish_reason = None
    tool_calls_acc: dict[int, dict] = {}
    started = time.time()
    checked = 0            # reasoning length at the last loop check
    host = None            # H0: which OpenRouter host served it (a top-level chunk field)
    call_usage = None

    oai_tools = _to_openai_tools(tools)

    create_kwargs: dict[str, Any] = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "max_tokens": max_tokens or _output_limit(extra_body),
        # Final streamed chunk carries usage (choices=[]) when this is set --
        # standard OpenAI-compatible convention, needed for session cost tracking.
        "stream_options": {"include_usage": True},
    }
    if oai_tools:
        # An empty list is omitted rather than sent: H1's forced synthesis withholds tools
        # so the call can only answer.
        create_kwargs["tools"] = oai_tools
    if temperature is not None:
        create_kwargs["temperature"] = temperature

    if extra_body is not None:
        create_kwargs["extra_body"] = extra_body

    stream = await client.chat.completions.create(**create_kwargs)

    async for chunk in stream:
        host = host or getattr(chunk, "provider", None)
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage:
            call_usage = chunk_usage
            details = getattr(chunk_usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", None) or 0 if details else 0
            usage.record(
                model,
                input_tokens=max(0, chunk_usage.prompt_tokens - cached),
                output_tokens=chunk_usage.completion_tokens,
                cache_read=cached,
                cost=getattr(chunk_usage, "cost", None),
            )
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        finish_reason = getattr(choice, "finish_reason", None) or finish_reason
        delta = choice.delta

        # Thinking tokens arrive in reasoning_content or reasoning; OpenRouter's own docs
        # describe a generic `reasoning` field for third-party models, but DeepSeek V4
        # specifically appears to use reasoning_content natively (its OWN API does, and its
        # OpenRouter-proxied echo requirement below is keyed on reasoning_content too) --
        # checking reasoning_content first, then reasoning, is correct either way.
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if reasoning:
            reasoning_content += reasoning
            await send({"type": "thinking", "delta": reasoning, "model": model})
            if not text and not tool_calls_acc:
                looping = False
                if len(reasoning_content) - checked >= _LOOP_CHECK_EVERY:
                    checked = len(reasoning_content)
                    looping = _reasoning_repeats(reasoning_content)
                if looping or time.time() - started > _THINK_LIMIT_S:
                    log.warning("%s: reasoning %s after %.0fs (%d chars), no answer -- stopping "
                                "the call", model, "repeats itself" if looping else "runs on",
                                time.time() - started, len(reasoning_content))
                    finish_reason = "loop"
                    try:
                        await stream.close()
                    except Exception:
                        pass
                    break

        if delta.content:
            text += delta.content
            await send({"type": "response", "delta": delta.content})
            if sentence_queue is not None:
                pending_text += delta.content
                complete, pending_text = _pop_sentences(pending_text)
                for s in complete:
                    await sentence_queue.put(s)

        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_calls_acc:
                    tool_calls_acc[idx] = {"id": "", "name": "", "args": ""}
                if tc_delta.id:
                    tool_calls_acc[idx]["id"] = tc_delta.id
                if tc_delta.function and tc_delta.function.name:
                    tool_calls_acc[idx]["name"] = tc_delta.function.name
                if tc_delta.function and tc_delta.function.arguments:
                    tool_calls_acc[idx]["args"] += tc_delta.function.arguments

    if sentence_queue is not None and pending_text.strip():
        await sentence_queue.put(pending_text.strip())

    if finish_reason == "loop" and call_usage is None:
        # Stopped before the final usage chunk: the reasoning was still generated and
        # billed, so estimate it, or the turn budget, /cost and llm_calls count it as 0.
        est = int(len(reasoning_content) / CHARS_PER_TOKEN) + 1
        call_usage = {"completion_tokens": est,
                      "completion_tokens_details": {"reasoning_tokens": est}}
        usage.record(model, input_tokens=0, output_tokens=est)

    tool_calls: list[dict] = []
    for idx in sorted(tool_calls_acc):
        tc = tool_calls_acc[idx]
        # A04: an unparseable argument string used to become {} and the tool ran anyway
        # -- the roadmap's "must become a structured validation failure, not {} followed
        # by execution". The raw text is carried on the call so _exec_tool can reject it
        # without executing, and tell the model its OWN arguments were malformed rather
        # than blaming the tool.
        invalid = None
        try:
            input_data = json.loads(tc["args"]) if tc["args"] else {}
        except json.JSONDecodeError:
            input_data, invalid = {}, (tc["args"] or "")[:200]
        call = {"id": tc["id"], "name": tc["name"], "input": input_data}
        if invalid is not None:
            call[_validation.INVALID_ARGS_KEY] = invalid
        tool_calls.append(call)

    _llm_calls.record(
        model=model, started_at=started, purpose=purpose, usage=call_usage,
        finish_reason=finish_reason, extra_body=extra_body,
        max_tokens=create_kwargs["max_tokens"], host=host,
        outcome=_llm_calls.outcome_of(text, tool_calls),
        iteration=iteration, plan_active=plan_active,
    )
    output_tokens = _llm_calls.usage_fields(call_usage).get("output_tokens")
    return _StreamResult((text, reasoning_content, tool_calls), finish_reason, output_tokens)


class _StreamResult(tuple):
    """(text, reasoning, tool_calls), still unpacked as three values everywhere, plus the
    call's finish_reason ("length" = the output budget ran out) and the output tokens it
    generated (None when the provider did not report usage)."""
    def __new__(cls, parts, finish_reason=None, output_tokens=None):
        obj = super().__new__(cls, parts)
        obj.finish_reason = finish_reason
        obj.output_tokens = output_tokens
        return obj


# ---------------------------------------------------------------------------
# Plan-and-Execute helpers
# ---------------------------------------------------------------------------

def _format_plan_for_system(plan: list[dict], *, revision: int = 0,
                            done_through: int = 0) -> str:
    """Append a plan block to the system prompt so the model knows its roadmap.

    Step 4 (2026-09-09) -- reframed as the executor's authorized SCOPE for this
    turn, not merely context it may wander from: the plan was only ever advisory
    before, and an undeclared detour quietly consumed the iteration budget the
    real steps needed with nothing noticing. request_replan (see
    _REQUEST_REPLAN_TOOL below, offered only while a plan is active) is the
    cheap, real route to leave it deliberately instead of silently improvising.

    A04 slice 5 -- `revision` and `done_through` exist because this block was
    previously written into the system prompt ONCE, at turn start, and never updated.
    A replan revised the local `plan`, emitted plan_update to the UI and DM'd the owner,
    but the model kept reading the ORIGINAL steps as its "authorized scope" while being
    told "Plan revised -- continue from there" (reproduced live). Naming the revision
    makes the supersession explicit rather than leaving the model to notice that the
    text changed, and `done_through` is the step evidence that makes "continue from
    there" mean something.
    """
    header = "[Execution Plan — your authorized scope for this turn]"
    if revision:
        header = (f"[Execution Plan — REVISION {revision}, replaces every earlier plan "
                  f"in this turn. Your authorized scope is now exactly this.]")
    lines = [
        "\n\n" + header,
        "Work through these steps in order (same parallel_group -> call their "
        "tools together in one turn). If something you learn means this plan "
        "is wrong or incomplete, call request_replan with why -- don't quietly "
        "work outside it.",
    ]
    for i, step in enumerate(plan):
        pg = step.get("parallel_group")
        tool = step.get("tool") or "no tool"
        pg_note = f" [parallel group: {pg}]" if pg else ""
        mark = " — DONE" if i < done_through else ""
        lines.append(f"  {step['id']}. {step['label']} (tool: {tool}){pg_note}{mark}")
    if done_through and done_through < len(plan):
        lines.append(f"You are on step {plan[done_through]['id']}.")
    return "\n".join(lines)


def _sync_plan_into_system(oai_messages: list[dict], base_system: str, plan: list[dict],
                           *, revision: int, done_through: int) -> None:
    """Rewrite the system message so the model reads the CURRENT plan.

    A04 acceptance: *"replans replace old instructions"*. Without this the revised plan
    reached the UI and the owner's DM but never the one party actually executing it.

    Only the plan block at the tail of the system string is rebuilt, so whatever prefix
    caching the provider does over the stable core is unaffected.
    """
    if not oai_messages or oai_messages[0].get("role") != "system":
        return
    oai_messages[0] = {
        "role": "system",
        "content": base_system + _format_plan_for_system(
            plan, revision=revision, done_through=done_through),
    }


# Step 4 -- offered to the executor ONLY for iterations where a plan is active
# (folded into that call's own `tools` list in _chat_compat, never registered in
# registry.py's global TOOLS): the plan is a Plan-and-Execute-only concept, and a
# turn without a plan has nothing to leave, so it is offered only while a plan is
# active. Deliberately NOT routed
# through TOOL_DISPATCH/_exec_tool -- _chat_compat intercepts it specially,
# since actually replanning needs the loop's own live plan/step_pointer/
# completed_steps state, which a plain dispatched tool function has no way to
# reach back into.
_REQUEST_REPLAN_TOOL = {
    "name": "request_replan",
    "description": (
        "Call this when your authorized plan needs to change -- something you've "
        "learned makes a remaining step wrong, unnecessary, or incomplete. This is "
        "the real route to deviate deliberately; working outside the plan without "
        "calling this is not allowed. Costs one of your two replans for this turn."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why the plan needs to change"},
        },
        "required": ["reason"],
    },
}


# H4 (decision 3): JARVIS may move a long turn to the background when he judges it needs
# to; nothing moves automatically. Offered like request_replan -- only inside the compat
# loop, never in the global registry -- because it needs this turn's own state (plan,
# what was fetched) to build the job's brief.
_CONTINUE_IN_BACKGROUND_TOOL = {
    "name": "continue_in_background",
    "description": (
        _owner.say("Move this task to the background and free the conversation. Use it only when "
        "the remaining work will clearly take several more minutes (many more searches "
        "or reads, a long write-up) and «owner» does not need to wait for it. The job "
        "continues with the plan and everything gathered so far, and messages «him» on "
        "Discord with the result. Do not use it for a question you can answer now.")
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string",
                       "description": "What is left to do and why it will take a while"},
        },
        "required": ["reason"],
    },
}
_HANDOFF_RESULT_CHARS = 2500      # per earlier tool result carried into the brief
_HANDOFF_BRIEF_CHARS = 20000      # all of them together


def _handoff_brief(request: str, reason: str, plan: list[dict], done_through: int,
                   turn_messages: list[dict]) -> str:
    """The job's brief: what was asked, why it moved, the plan, and what was already
    fetched this turn (trimmed), so the job continues rather than starting over."""
    calls: dict[str, str] = {}
    fetched: list[str] = []
    used = 0
    for m in turn_messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls[tc.get("id", "")] = f"{fn.get('name', '')}({(fn.get('arguments') or '')[:200]})"
        if m.get("role") == "tool":
            text = str(m.get("content") or "")[:_HANDOFF_RESULT_CHARS]
            if used + len(text) > _HANDOFF_BRIEF_CHARS:
                fetched.append("[...earlier results omitted for length]")
                break
            used += len(text)
            fetched.append(f"- {calls.get(m.get('tool_call_id'), 'tool')}:\n{text}")
    lines = [_owner.say("This task was started in a live conversation with «owner» and handed to you "
             "to finish in the background."),
             _owner.say(f"«Owner» asked: {request}"), f"Why it moved to the background: {reason}"]
    if plan:
        lines.append("Plan:")
        for i, st in enumerate(plan):
            mark = " (done)" if i < done_through else ""
            lines.append(f"  {i + 1}. {st.get('label', '')}{mark}")
    if fetched:
        lines.append("Already gathered in the conversation (do not fetch these again):")
        lines.extend(fetched)
    lines.append(_owner.say("Finish the task, then write the complete answer. It is sent to «owner» as "
                 "a Discord message from you, so write it as JARVIS speaking to «him» directly: "
                 "plain prose, no markdown, no preamble such as 'Done' or 'Here is'."))
    return "\n".join(lines)


def _request_text(user_text: str, messages: list[dict]) -> str:
    """the owner's own words for this turn: user_text, or the last user message without
    the volatile context block the compat layout appends to it."""
    if user_text:
        return user_text
    for m in reversed(messages):
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, list):   # a message with images
            content = next((part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"), "")
        if m.get("role") == "user" and isinstance(content, str):
            return content.split("\n\n" + _TURN_CONTEXT_HEADER)[0].strip()
    return ""


# Discord plan notification — DI hook, same pattern as background.set_broadcaster
# and research.set_notifier (engine.py must not import discord_bridge directly).
_plan_notifier: Callable[[str], Awaitable[bool]] | None = None


def set_plan_notifier(fn: Callable[[str], Awaitable[bool]]) -> None:
    """Injected from main.py: async fn(text) -> bool that DMs the owner."""
    global _plan_notifier
    _plan_notifier = fn


def _format_plan_for_discord(user_text: str, plan: list[dict]) -> str:
    """One-shot 'here's what I'm doing' DM — the plan and nothing else.

    Deliberately not a live progress feed: the owner asked for exactly the
    step list up front, then silence until the normal completion message,
    not a play-by-play.
    """
    gist = user_text.strip().replace("\n", " ")
    if len(gist) > 140:
        gist = gist[:140].rstrip() + "…"
    lines = [f'Working on: "{gist}"', ""]
    for step in plan:
        pg = step.get("parallel_group")
        tool = step.get("tool")
        suffix = f" ({tool})" if tool else ""
        suffix += f" [parallel {pg}]" if pg else ""
        lines.append(f"{step.get('id', '?')}. {step.get('label', '?')}{suffix}")
    lines.append("")
    lines.append("I'll message you when it's done.")
    return "\n".join(lines)


async def _notify_plan_discord(user_text: str, plan: list[dict]) -> None:
    """Fire-and-forget: DM the plan, never touches execution timing or the
    result if it fails (same silent-degradation contract as everywhere else)."""
    if _plan_notifier is None:
        return
    try:
        await _plan_notifier(_format_plan_for_discord(user_text, plan))
    except Exception as e:
        log.debug("engine: plan DM failed: %s", e)


def _format_plan_change_for_discord(reason: str, revised: list[dict]) -> str:
    """Step 4 (2026-09-09) -- 'the plan changed,' one short message, in the
    same spirit as _format_plan_for_discord: the revised steps and why, never
    a per-step progress feed. Fires for EITHER replan trigger (a tool error,
    or the executor deliberately calling request_replan) -- both are "the plan
    changed," from the owner's side there's no reason to word them differently.
    """
    lines = [f"Plan changed: {reason.strip()}", ""]
    for step in revised:
        tool = step.get("tool")
        suffix = f" ({tool})" if tool else ""
        lines.append(f"{step.get('id', '?')}. {step.get('label', '?')}{suffix}")
    return "\n".join(lines)


async def _notify_plan_change_discord(reason: str, revised: list[dict]) -> None:
    """Fire-and-forget, same contract as _notify_plan_discord -- never adds
    latency to execution, never affects the turn if the DM itself fails."""
    if _plan_notifier is None:
        return
    try:
        await _plan_notifier(_format_plan_change_for_discord(reason, revised))
    except Exception as e:
        log.debug("engine: plan-change DM failed: %s", e)


# 8.0, then 15.0, both proved too tight -- live logs kept showing
# asyncio.TimeoutError (its str() is empty, hence the "_plan_task failed ()"
# warnings) at 18-19s elapsed even after the first raise. Live-tested directly
# against the real Moonshot API (see below) with the actual .env key: kimi-k2.5
# reasons at length in `reasoning_content` on genuinely ambiguous requests
# (observed up to ~4900 chars of reasoning for a single planning call) and that
# reasoning counts against the SAME max_tokens budget as the answer -- at the
# old max_tokens=1200 the reasoning alone consumed the entire budget, so the
# JSON got cut off after ~170 chars (finish_reason="length") EVERY time on
# anything non-trivial, independent of the timeout. Confirmed live: raising
# max_tokens to 4000 let the same request complete cleanly (932 completion
# tokens, valid JSON, finish_reason="stop"). A 4-request batch through the
# real _plan_task end to end (not just the raw API call) at that budget
# ranged 17.2-40.75s, all producing valid plans -- the model generates at
# roughly ~35-40 tokens/sec here, so a fuller 4000-token budget legitimately
# needs this much room. Timeout set well above that observed worst case
# rather than right at the edge of it.
_PLAN_TIMEOUT = 60.0   # seconds — degrade to no-plan past this (see max_tokens note above)

# _plan_task gets the larger budget (fresh planning invites more reasoning over
# an ambiguous request); _replan is a narrower revise-in-place task and needs less.
_PLAN_TASK_MAX_TOKENS = 4000
_REPLAN_MAX_TOKENS    = 3000

# Step 4 (2026-09-09) -- shared across BOTH replan triggers (a tool error, or the
# executor deliberately calling request_replan): past this many revisions in one
# turn, finish the current plan or hit the iteration cap honestly rather than
# letting a confused model spend its whole budget planning instead of doing.
_MAX_REPLANS_PER_TURN = 2


def _plan_message_text(message) -> str:
    """Usable text from a planner/replanner response message.

    Both providers sometimes return an EMPTY `content` with the real output in a reasoning
    field instead (same behavior consolidation._call_compat already salvages on its streaming
    path) — without this fallback the parse fails with "Expecting value: line 1 column 1
    (char 0)" on every planned turn. Checks reasoning_content first (Moonshot's field, and
    DeepSeek's own native one too), falling back to reasoning (OpenRouter's generic field
    for third-party models) if that's empty. When falling back, sliced from the first '['
    (the plan is always a JSON array) so a reasoning preamble ("The user wants me to act
    as JARVIS...") never masquerades as the answer or desyncs _extract_json_array's own
    find("[")/rfind("]") slice -- same discipline as research._salvage_message_text's
    first-'{' slice. Pure narration with no '[' at all salvages to '' rather than being
    passed on.
    """
    text = (message.content or "").strip()
    if text:
        return text
    reasoning = (getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None) or "").strip()
    bracket = reasoning.find("[")
    if bracket != -1:
        return reasoning[bracket:]
    return ""


def _extract_json_array(raw: str) -> list | None:
    """Parse a JSON array out of model output that may be fenced or wrapped in prose.

    Strips markdown fences, then slices from the first '[' to the last ']' —
    "Here is the plan: [...]" and reasoning-preamble output both survive.
    Returns None (never raises) when no parseable array is present.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()
    start = raw.find("[")
    end   = raw.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _planning_context(user_text: str) -> str:
    """Compact context for the planner, from what this turn already retrieved.

    Sourced from the per-turn Origin rather than re-retrieving: the foreground gather
    has already paid for the queue and topic lookup, and paying again inside a
    latency-sensitive pre-pass would be wasteful. Never raises -- a planner without
    context is the previous behavior, not a failure.
    """
    try:
        from backend.context import origin as _o
        from backend.context import service as _ctx

        current = _o.current_origin.get()
        if current is None:
            return ""
        snapshot = _ctx.build_planning(
            user_text, queue=current.queue, relevant_topic=current.relevant_topic)
        rendered = _ctx.render_planning_context(snapshot)
        target = discord_context.current.get()
        anchor = target.render() if target is not None else ""
        return rendered + ("\n\n" + anchor if anchor else "")
    except Exception as exc:
        log.debug("planner: context unavailable (%s)", exc)
        return ""


async def _plan_task(client, user_text: str, tools: list[dict] = TOOLS) -> list[dict]:
    """Fast standard-tier call (_provider()) that produces a structured JSON execution plan.

    Returns a list of step dicts on success, [] on any failure (caller skips
    structured execution and behaves as before).
    """
    provider = _provider()
    tool_names = ", ".join(t["name"] for t in tools)
    # A03: the planner used to see ONLY the request string and a list of tool names --
    # the roadmap's "never plan from bare task text when context already exists". Without
    # the open intents or the existing note on a subject it plans a redundant first step
    # (search for something already written down) or misses work already in flight.
    # Budgeted under the PLANNING profile, deliberately small: this call produces a 2-5
    # step JSON list, so context beyond goal-and-what-is-known is pure latency and cost.
    plan_context = _planning_context(user_text)
    prompt = (
        f'User request: "{user_text}"\n\n'
        + (f"Context you already have:\n{plan_context}\n\n" if plan_context else "")
        + f"Available tools: {tool_names}\n\n"
        "Produce a minimal execution plan. "
        "Return ONLY a valid JSON array, no prose, no markdown:\n"
        "[\n"
        '  {"id": 1, "label": "Search the web for X", "tool": "web_browse", "parallel_group": null},\n'
        '  {"id": 2, "label": "Check emails about X", "tool": "gmail_action", "parallel_group": "A"},\n'
        '  {"id": 3, "label": "Fetch the relevant article", "tool": "fetch_page", "parallel_group": "A"},\n'
        '  {"id": 4, "label": "Synthesize and answer", "tool": null, "parallel_group": null}\n'
        "]\n\n"
        "Rules:\n"
        "- 2 to 5 steps maximum\n"
        "- label: short user-facing description\n"
        "- tool: exact tool name or null for pure reasoning steps\n"
        "- parallel_group: same letter for truly independent steps, null otherwise\n"
        "- If no tools are needed (pure conversation/reasoning), return []\n"
        "- The last step is usually a synthesis step with null tool"
    )
    try:
        # Bounded — the planner is a latency tax paid before the main turn, so a
        # slow/hung call must degrade to no-plan rather than stalling the response.
        started = time.time()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=provider.standard,
                messages=[
                    {"role": "system", "content": "You are a precise task planner. Return only valid JSON."},
                    {"role": "user",   "content": prompt},
                ],
                stream=False,
                max_tokens=_PLAN_TASK_MAX_TOKENS,
                # Moonshot pins kimi-k2.6 to exactly one allowed temperature, but WHICH
                # value depends on whether thinking is on or off -- confirmed live: with
                # thinking enabled (no extra_body below) it demanded exactly 1.0 ("only 1
                # is allowed"); once thinking was explicitly disabled (see no_thinking_body
                # below, added 2026-09-01) the SAME model started demanding exactly 0.6
                # instead ("only 0.6 is allowed" -- every _plan_task/_replan call 400'd and
                # silently degraded to "no plan" until this was updated). DeepSeek/OpenRouter
                # has been live-verified to tolerate 0.6 too. Lives on router.Provider (not a
                # bare literal here) so a change is a one-line dataclass edit.
                temperature=provider.no_thinking_temperature,
                # The standard-tier model thinks by default, and that reasoning shares
                # max_tokens with the JSON answer -- see router.OPENROUTER_NO_THINKING_BODY.
                extra_body=with_provider_prefs(provider.no_thinking_body, provider),
            ),
            timeout=_PLAN_TIMEOUT,
        )
        if resp.usage:
            usage.record(
                provider.standard, resp.usage.prompt_tokens, resp.usage.completion_tokens,
                cost=getattr(resp.usage, "cost", None),
            )
        _llm_calls.record_response(
            resp, model=provider.standard, started_at=started, purpose="planner",
            extra_body=provider.no_thinking_body, max_tokens=_PLAN_TASK_MAX_TOKENS,
        )
        raw = _plan_message_text(resp.choices[0].message)
        steps = _extract_json_array(raw)
        if steps is not None and all(isinstance(s, dict) and "id" in s and "label" in s for s in steps):
            # A plan with no tool step is a conversation. Live (5 October): the skills-update
            # paragraph got a one-step plan, "Acknowledge the new skill system update", and with
            # it a "Working on ... I'll message you when it's done" DM before the reply.
            if not any(s.get("tool") for s in steps):
                log.debug("_plan_task: no tool step in %d step(s) -- no plan", len(steps))
                return []
            log.debug("_plan_task produced %d steps", len(steps))
            return steps
        log.warning("_plan_task: unusable planner output (%r…) — proceeding without plan", raw[:120])
    except Exception as e:
        log.warning("_plan_task failed (%s) — proceeding without plan", e)
    return []


async def _replan(
    client, user_text: str, completed: list[dict], errors: list[str], remaining: list[dict]
) -> list[dict]:
    """Revise remaining steps after a failure. Falls back to original remaining on error."""
    provider = _provider()
    completed_summary = "\n".join(
        f"  {s.get('id', '?')}. {s.get('label', '?')} — {'success' if s.get('success') else 'FAILED'}"
        for s in completed
    )
    error_summary = "\n".join(f"  - {e}" for e in errors)
    remaining_summary = "\n".join(
        f"  {s.get('id', '?')}. {s.get('label', '?')}" for s in remaining
    )
    prompt = (
        f'Original task: "{user_text}"\n\n'
        f"Completed steps:\n{completed_summary}\n\n"
        f"Errors:\n{error_summary}\n\n"
        f"Remaining planned steps (revise as needed):\n{remaining_summary}\n\n"
        "Return ONLY a JSON array of revised remaining steps. "
        "Keep the same id numbering. Minimal changes — only fix what broke:\n"
        '[{"id": N, "label": "...", "tool": "tool_or_null", "parallel_group": null}]'
    )
    try:
        # Bounded like _plan_task -- a hung replan call must degrade to
        # keeping the original remaining steps, not stall the whole turn.
        started = time.time()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=provider.standard,
                messages=[
                    {"role": "system", "content": "You are a task replanner. Return only valid JSON."},
                    {"role": "user",   "content": prompt},
                ],
                stream=False,
                max_tokens=_REPLAN_MAX_TOKENS,
                # See the matching comment in _plan_task -- same temperature/extra_body
                # pairing, same reasoning, read off router.Provider.
                temperature=provider.no_thinking_temperature,
                extra_body=with_provider_prefs(provider.no_thinking_body, provider),
            ),
            timeout=_PLAN_TIMEOUT,
        )
        if resp.usage:
            usage.record(
                provider.standard, resp.usage.prompt_tokens, resp.usage.completion_tokens,
                cost=getattr(resp.usage, "cost", None),
            )
        _llm_calls.record_response(
            resp, model=provider.standard, started_at=started, purpose="replanner",
            extra_body=provider.no_thinking_body, max_tokens=_REPLAN_MAX_TOKENS,
        )
        raw = _plan_message_text(resp.choices[0].message)
        revised = _extract_json_array(raw)
        if revised is not None:
            return revised
        log.warning("_replan: unusable output (%r…) — keeping original remaining steps", raw[:120])
    except Exception as e:
        log.warning("_replan failed (%s) — keeping original remaining steps", e)
    return remaining


def _plan_group_size(plan: list[dict], pointer: int) -> int:
    """Number of consecutive plan steps starting at `pointer` that share the
    same non-null parallel_group. 1 for ungrouped steps."""
    if pointer >= len(plan):
        return 1
    group = plan[pointer].get("parallel_group")
    if not group:
        return 1
    size = 1
    for step in plan[pointer + 1:]:
        if step.get("parallel_group") == group:
            size += 1
        else:
            break
    return size


async def _chat_compat(
    model: str, system: str, messages: list, send: WS_Sender, sentence_queue,
    user_text: str = "", tools: list[dict] = TOOLS,
    channel: str = "local", use_thinking: bool | None = None,
    allow_foreground_plan: bool = True, max_steps: int = _MAX_TOOL_ITERATIONS,
) -> str:
    client = _get_compat_client()
    provider = _provider()
    full_response = ""
    final_text = ""  # see its assignment below for why this is tracked separately
    if use_thinking is None:
        # For callers that pass a model with no tier. When both tiers are one model at
        # different reasoning efforts the model string carries no tier at all, so this
        # resolves to the STANDARD tier rather than `model == thinking`, which would be
        # always True and bill every such call at high effort. chat() always passes
        # the tier explicitly.
        if provider.standard == provider.thinking:
            use_thinking = False
        else:
            use_thinking = (model == provider.thinking)
    # Step 3 (2026-09-09) -- the turn's position on escalation.py's 3-level
    # ladder, scoped to this turn only (never persisted across turns). Starts
    # wherever route()'s own decision already put it; only moves from here if
    # the turn gets stuck or a replan fires. See escalation.py's module
    # docstring for the full design and the live verification behind it.
    level = "thinking_normal" if use_thinking else "standard"
    start_level = level       # de-escalation never goes below where routing put the turn

    # --- Plan phase: only for complex (thinking-model) requests ---
    plan: list[dict] = []
    plan_announced_at = None
    turn_started = time.time()
    base_system = system          # A04 slice 5: the plan block is rebuilt onto this
    # G3: a turn that offers a goal keeps its thinking tier but runs no foreground plan --
    # the work belongs to the goal, if he accepts one, not to this turn's tool plan.
    if use_thinking and user_text and allow_foreground_plan:
        plan = await _plan_task(client, user_text, tools)
        if plan:
            # A06: publish the plan so a job spawned mid-turn inherits it as a real
            # constraint (see context/origin._plan_constraints).
            _origin.set_plan(plan)
            await send({"type": "plan", "steps": [{**s, "status": "pending"} for s in plan]})
            system = system + _format_plan_for_system(plan)
            if channel == "discord" and not (
                discord_context.current.get() and
                discord_context.current.get().target_id
            ):
                # Fire-and-forget: the DM must never add latency to execution,
                # and a failure to send it must never affect the turn itself.
                spawn(_notify_plan_discord(user_text, plan))
                plan_announced_at = time.time()

    oai_messages = [{"role": "system", "content": system}] + messages

    step_pointer = 0          # which plan step we're currently executing
    completed_steps: list[dict] = []
    call_log: list[str] = []  # ground-truth tool-call trail for _build_progress_note
    trail: list[escalation.Call] = []   # what ran, with which arguments, and whether it worked
    cap_hit = True            # flipped off when the loop exits via "no tool calls"
    replans_this_turn = 0     # Step 4 -- shared cap across BOTH replan triggers
    turn_output = 0           # H1: output tokens generated by this turn's calls so far
    max_effort_used = False   # H1: max effort runs at most once per turn
    handed_off = False        # H4: continue_in_background succeeded; next call only answers
    turn_start_index = len(oai_messages)
    progress = turn_progress[channel] = {
        "started": turn_started, "plan": [str(s.get("label", "")) for s in plan],
        "step": 0, "tool_calls": 0, "synthesizing": False,
        "announced_at": plan_announced_at,
    }

    for _iteration in range(max_steps):
        replanned = False   # set True below only on the iteration a replan actually fires
        deliberate = False  # ...and only when the model itself asked for it (request_replan)
        progress["step"], progress["tool_calls"] = step_pointer, len(call_log)
        progress["plan"] = [str(s.get("label", "")) for s in plan]   # a replan may change it
        # --- Step start telemetry ---
        if plan:
            current = plan[step_pointer] if step_pointer < len(plan) else None
            label = current["label"] if current else f"Step {step_pointer + 1}"
            await send({"type": "step_start", "n": step_pointer + 1, "label": label})

        # F4 (2026-09-08): _reasoning_config is the single derivation of "which tier is
        # this call" for the OpenAI-compatible path -- resolves both the temperature
        # (F2) and the extra_body reasoning toggle from (provider, use_thinking) here,
        # once, rather than _run_stream_openai re-deriving the extra_body half from
        # model identity on its own.
        temperature, extra_body = _reasoning_config(provider, use_thinking)
        if level == "thinking_max" and not max_effort_used:
            # Step 3 escalation: same temperature, raised effort -- see
            # escalation.resolve()'s docstring for why only extra_body changes.
            # H1: once per turn. Later iterations at this level run at the tier's own
            # effort, since each max call can now generate up to 128K tokens.
            extra_body = with_provider_prefs(escalation.MAX_EFFORT_BODY, provider)
            max_effort_used = True
        # H1: a turn ends with an answer, never silently. Once it has spent most of its
        # output budget, or reached its last allowed tool step, the next call is the
        # synthesis: tools are withheld so it can only answer, and past the budget share
        # it thinks at low effort. The last step used to end the turn with
        # _LOOP_CAP_MESSAGE and no answer at all, which the H0 benchmark caught on the
        # rebuilt 28 September research task.
        winding_down = turn_output >= _WIND_DOWN_SHARE * _TURN_OUTPUT_BUDGET
        synthesize_now = winding_down or _iteration == max_steps - 1
        if winding_down and use_thinking and _low_effort_body(provider) is not None:
            extra_body = _low_effort_body(provider)
        call_max_tokens = min(_output_limit(extra_body),
                              max(_TURN_OUTPUT_BUDGET - turn_output, _MIN_CALL_TOKENS))
        call_messages = (oai_messages + [{"role": "user", "content": _SYNTHESIZE_NOW_NUDGE}]
                         if synthesize_now else oai_messages)
        # Step 4 -- request_replan is offered ONLY while a plan is active, folded
        # into this call's own tools list rather than registry.py's global TOOLS
        # (see _REQUEST_REPLAN_TOOL's comment for why).
        call_tools = [] if (synthesize_now or handed_off) else (
            [*tools, _REQUEST_REPLAN_TOOL] if plan else list(tools))
        if call_tools:
            # On every tier, not just thinking: the tool list sits right after the system
            # prompt, so a list that changed with the tier would break the cached prefix
            # each time a conversation switched between standard and thinking turns.
            call_tools.append(_CONTINUE_IN_BACKGROUND_TOOL)
        progress["synthesizing"] = synthesize_now
        if synthesize_now:
            log.info("%s: turn synthesizing now (%s, %d output tokens so far)", provider.name,
                     "budget" if winding_down else "last step", turn_output)
        try:
            result = await _run_stream_openai(
                client, model, call_messages, send, sentence_queue,
                temperature=temperature, extra_body=extra_body, tools=call_tools,
                iteration=_iteration, plan_active=bool(plan), max_tokens=call_max_tokens,
            )
            text, reasoning, tool_calls = result
        except Exception as e:
            err = str(e)
            if use_thinking and ("404" in err or "not found" in err.lower() or "permission" in err.lower()):
                log.warning(
                    "Thinking model unavailable (%s) — falling back to %s.", model, provider.standard
                )
                use_thinking = False
                model = provider.standard
                level = start_level = "standard"   # keep the ladder state honest about what ran
                temperature, extra_body = _reasoning_config(provider, use_thinking)
                result = await _run_stream_openai(
                    client, model, call_messages, send, sentence_queue,
                    temperature=temperature, extra_body=extra_body, tools=call_tools,
                    iteration=_iteration, plan_active=bool(plan),
                )
                text, reasoning, tool_calls = result
            else:
                raise
        turn_output += getattr(result, "output_tokens", None) or 0
        finish = getattr(result, "finish_reason", None)
        if ((use_thinking or finish == "loop") and not tool_calls and not text.strip()
                and reasoning.strip()):
            # Live (2026-09-28): asked on Discord to compile a long research task into a
            # conclusion, the thinking tier reasoned until the output budget ran out
            # (finish_reason "length"), with no answer and no tool call. The salvage below
            # then sent that reasoning as the reply -- raw scratch work ("Hmm wait, let me
            # re-read the injected history", "let me just call X and Y") posted to the owner.
            # Reproduced with a lowered budget: finish_reason ['length'], the reasoning
            # returned as the Discord text. Reasoning is never the answer on this path. One
            # direct call on the standard tier, reasoning off, asks for the answer; if that
            # also fails, a plain line says so.
            log.warning("%s: thinking tier ended with no answer (finish_reason=%s, %d chars "
                        "of reasoning) -- not sending the reasoning; asking once for a direct "
                        "answer", provider.name, finish, len(reasoning))
            use_thinking, model, level = False, provider.standard, "standard"
            temperature, extra_body = _reasoning_config(provider, use_thinking)
            result = await _run_stream_openai(
                client, model, call_messages + [{"role": "user", "content": _DIRECT_ANSWER_NUDGE}],
                send, sentence_queue, temperature=temperature, extra_body=extra_body,
                tools=call_tools, purpose=_llm_calls.RECOVERY,
                iteration=_iteration, plan_active=bool(plan),
            )
            text, reasoning, tool_calls = result
            turn_output += getattr(result, "output_tokens", None) or 0
            finish = getattr(result, "finish_reason", None)
            if not tool_calls and not text.strip():
                text = _NO_ANSWER_FALLBACK
                await send({"type": "response", "delta": text})
                if sentence_queue is not None:
                    await sentence_queue.put(text)
        if finish == "length" and text.strip() and not tool_calls:
            # H1: the ANSWER was cut off, not the thought -- finish it with one more call
            # rather than sending half a reply. The thinking is done, so the continuation
            # runs on the standard tier with reasoning off.
            log.warning("%s: answer cut off at the output limit (%d chars) -- continuing it "
                        "once", provider.name, len(text))
            c_temperature, c_body = _reasoning_config(provider, False)
            more = await _run_stream_openai(
                client, provider.standard,
                call_messages + [{"role": "assistant", "content": text},
                                 {"role": "user", "content": _CONTINUE_ANSWER_NUDGE}],
                send, sentence_queue, temperature=c_temperature, extra_body=c_body,
                tools=[], purpose="continuation", iteration=_iteration,
                plan_active=bool(plan),
            )
            turn_output += getattr(more, "output_tokens", None) or 0
            text = text + more[0]
        if not use_thinking and not tool_calls and not text.strip() and reasoning.strip():
            log.warning(
                "%s: standard tier streamed reasoning (%d chars) despite thinking being "
                "disabled, with empty content -- provider ignored the no-thinking "
                "extra_body. Not salvaging (would read internal reasoning aloud); "
                "returning a plain fallback instead.",
                provider.name, len(reasoning),
            )
            text = _REASONING_LEAK_FALLBACK
            await send({"type": "response", "delta": text})
            if sentence_queue is not None:
                await sentence_queue.put(text)

        full_response += text
        # Hardening (2026-09-09): full_response accumulates EVERY iteration's text,
        # including a tool-call turn's own planning narration ("Let me check
        # both the evolution file and the code graph..."). That's correct
        # for what streams live via send()/sentence_queue every iteration
        # (unchanged -- desk voice/UI still hears JARVIS think out loud
        # mid-task, exactly as before). It's WRONG for what this function
        # ultimately returns, because that return value becomes the Discord
        # reply verbatim, the stored conversation-history turn, and the
        # mem0/exchange-index text -- three permanent records. Live incident
        # (2026-09-09): a Discord reply to "what happens when rumination
        # launches at night" was a wall of "Let me check X... I found Y...
        # let me dig deeper" tool-planning narration glued in front of (and
        # sometimes instead of) the actual answer -- and because that same
        # narration got stored as the "assistant" side of the exchange, it
        # kept resurfacing in later exchange recall too, compounding the
        # problem it caused. final_text tracks only the CURRENT iteration's
        # text (overwritten every pass, not accumulated), so on normal
        # completion the return is just the final synthesis turn -- see the
        # cap_hit branch below for the one case that still uses the full
        # accumulated trail on purpose.
        final_text = text

        if not tool_calls:
            # A call the model still wanted to make, written as text in its own markup on a
            # step offered no tools (live 2026-10-15): it never ran, so it is not the answer.
            if _tool_markup.has_markup(final_text):
                log.warning("%s: final answer carried tool-call markup -- removed", provider.name)
                final_text = _tool_markup.strip(final_text) or _NO_ANSWER_FALLBACK
            # Final synthesis turn — mark the last plan step done
            if plan:
                await send({"type": "step_done", "n": step_pointer + 1,
                            "success": True, "tools": []})
            cap_hit = False
            break

        for tc in tool_calls:
            call_log.append(_summarize_tool_call(tc["name"], tc.get("input")))

        # Step 4 (2026-09-09) -- request_replan is a meta-action about the plan
        # itself, not a plan-step execution: pull it out before the normal
        # per-step bookkeeping below so it's never mistaken for a consumed step,
        # and before the "no tool calls -> turn is done" check above, whose
        # emptiness test already ran against the FULL tool_calls (so a turn
        # whose only call was request_replan correctly did NOT break there).
        all_tool_calls = tool_calls
        replan_request = next((tc for tc in all_tool_calls if tc["name"] == "request_replan"), None)
        handoff_request = next((tc for tc in all_tool_calls
                                if tc["name"] == "continue_in_background"), None)
        step_tool_calls = [tc for tc in all_tool_calls
                           if tc is not replan_request and tc is not handoff_request]
        handoff_result_text = ""
        if handoff_request is not None:
            reason = (handoff_request.get("input") or {}).get("reason") or "it needs more time"
            brief = _handoff_brief(_request_text(user_text, messages), reason, plan,
                                   step_pointer, oai_messages[turn_start_index:])
            handoff_result_text = await background.start_handoff(
                _request_text(user_text, messages), brief)
            handed_off = not handoff_result_text.startswith("Error:")
            log.info("%s: continue_in_background -> %s", provider.name,
                     handoff_result_text[:80])

        # --- Parallel tool execution (real plan-step/tool actions only) ---
        results: list[tuple[dict, Any, bool]] = list(
            await asyncio.gather(*[_exec_tool(tc, send) for tc in step_tool_calls])
        ) if step_tool_calls else []

        errors: list[str] = [str(r) for _, r, ok in results if not ok]
        success = len(errors) == 0
        trail.extend(escalation.Call(tc["name"], escalation.args_key(tc.get("input")), ok,
                                     _iteration) for tc, _, ok in results)

        if plan and step_tool_calls:
            # A turn that executed a parallel group consumes every step in that
            # group — advancing by 1 would desync the timeline for the rest of
            # the run. Consume up to the group size, bounded by how many tool
            # calls the model actually made.
            group_size = _plan_group_size(plan, step_pointer)
            consumed = min(group_size, max(1, len(step_tool_calls)))

            await send({"type": "step_done", "n": step_pointer + 1,
                        "success": success, "tools": [tc["name"] for tc in step_tool_calls],
                        "reason": (errors[0] if errors else None)})
            # Extra steps consumed by the same turn: emit instant start/done
            # pairs so the UI marks the whole group rather than drifting.
            for extra in range(1, consumed):
                n = step_pointer + 1 + extra
                extra_label = plan[step_pointer + extra]["label"] if step_pointer + extra < len(plan) else f"Step {n}"
                await send({"type": "step_start", "n": n, "label": extra_label})
                await send({"type": "step_done", "n": n, "success": success, "tools": []})

            for offset in range(consumed):
                idx = step_pointer + offset
                plan_step = plan[idx] if idx < len(plan) else {
                    "id": idx + 1, "label": f"Step {idx + 1}"
                }
                completed_steps.append({**plan_step, "success": success})

            if not success:
                remaining = plan[step_pointer + consumed:]
                # Step 4: shared cap with the deliberate request_replan path below --
                # past it, keep the original remaining steps and finish honestly
                # rather than revising again.
                if remaining and replans_this_turn < _MAX_REPLANS_PER_TURN:
                    revised = await _replan(client, user_text, completed_steps, errors, remaining)
                    plan = plan[:step_pointer + consumed] + revised
                    replans_this_turn += 1
                    # A04 slice 5: the executor must read the revised plan too -- before
                    # this, only the UI and the Discord DM were updated.
                    _origin.set_plan(plan)   # A06: revised scope for any new job
                    _sync_plan_into_system(oai_messages, base_system, plan,
                                           revision=replans_this_turn,
                                           done_through=step_pointer + consumed)
                    await send({"type": "plan_update",
                                "steps": [{**s, "status": "pending"} for s in revised]})
                    if channel == "discord":
                        spawn(_notify_plan_change_discord(
                            errors[0] if errors else "a step failed", revised,
                        ))
                    replanned = True   # Step 3: a real replan is one of the two escalation signals

            step_pointer += consumed
        elif step_tool_calls:
            step_pointer += 1
        # else: only request_replan was called this iteration -- nothing was
        # executed against the plan, so no step bookkeeping happens; the
        # dangling step_start sent at the top of this iteration is reconciled
        # by the plan_update event below rebuilding the UI's whole timeline.

        # --- Handle a deliberate replan request, if the model made one ---
        replan_result_text: str | None = None
        if replan_request is not None:
            reason = (replan_request.get("input") or {}).get("reason") or "no reason given"
            if replans_this_turn >= _MAX_REPLANS_PER_TURN:
                replan_result_text = (
                    f"Error: you've already used your {_MAX_REPLANS_PER_TURN} replans this "
                    "turn -- finish with the current plan as best you can rather than asking "
                    "to replan again."
                )
            else:
                remaining = plan[step_pointer:]
                revised = await _replan(
                    client, user_text, completed_steps,
                    [f"Deliberate replan requested: {reason}"], remaining,
                )
                plan = plan[:step_pointer] + revised
                replans_this_turn += 1
                replanned = True   # Step 3: this counts as a replan signal too
                deliberate = True  # and the one replan that can justify max effort
                # A04 slice 5: same as the error-triggered path -- the model was being
                # told "continue from there" about a plan it could not see.
                _origin.set_plan(plan)   # A06: revised scope for any new job
                _sync_plan_into_system(oai_messages, base_system, plan,
                                       revision=replans_this_turn,
                                       done_through=step_pointer)
                await send({"type": "plan_update",
                            "steps": [{**s, "status": "pending"} for s in revised]})
                if channel == "discord":
                    spawn(_notify_plan_change_discord(reason, revised))
                replan_result_text = (
                    f"Plan revised — {len(revised)} step(s) remaining. Continue from there."
                )

        # --- Build assistant message (reasoning-echo requirement: DeepSeek V4 400s without
        # reasoning_content on the next turn -- "The reasoning_content in the thinking mode must be passed
        # back to the API" -- and, per provider.reasoning_echo_required, 400s on the field
        # being ABSENT even when this turn's reasoning happened to stream empty, not just on
        # the wrong key) ---
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id":   tc["id"],
                    "type": "function",
                    "function": {
                        "name":      tc["name"],
                        "arguments": json.dumps(tc["input"]),
                    },
                }
                # Step 4: ALL calls the model actually made this iteration,
                # including request_replan -- every tool_call_id here needs a
                # matching tool-role result below or the API rejects the
                # request as malformed; step_tool_calls (used for the plan
                # bookkeeping above) is a subset, not a replacement.
                for tc in all_tool_calls
            ],
        }
        if reasoning:
            assistant_msg[provider.reasoning_echo_key] = reasoning
        elif use_thinking and provider.reasoning_echo_required:
            assistant_msg[provider.reasoning_echo_key] = ""
        # F5 (2026-09-08) -- LIVE-VERIFIED against the real OpenRouter API, not assumed:
        # a stale/mismatched reasoning_content echo crossing a mid-turn tier switch does
        # NOT get rejected by DeepSeek in either direction. Tested both ways with a real
        # tool-call turn: (1) an assistant message produced on the THINKING tier (Pro),
        # carrying a real reasoning_content echo, sent unmodified as prior history to a
        # follow-up call on the NON-THINKING tier (Flash, reasoning.enabled=false) -- 200,
        # no rejection; (2) an assistant message produced on the NON-THINKING tier (Flash),
        # carrying NO reasoning_content key at all, sent as prior history to a follow-up
        # call on the THINKING tier (Pro, reasoning.effort=high) -- also 200, no rejection.
        # So oai_messages is intentionally left as-is across a tier switch (the 404 fallback
        # already in this loop, and any future mid-turn effort/model escalation) -- no echo
        # stripping/rewriting needed. If DeepSeek's contract ever tightens, that would show
        # up as a 400 whose message names reasoning_content, same signature as the original
        # discovery of this requirement; re-verify live before re-adding stripping logic
        # rather than guessing.
        oai_messages.append(assistant_msg)

        # --- Append tool results ---
        for tc, result, ok in results:
            content = str(result)
            if not ok:
                log.warning("Tool %s returned error: %s", tc["name"], content)
            oai_messages.append({
                "role":         "tool",
                "tool_call_id": tc["id"],
                "content":      content,
            })
        if replan_request is not None:
            oai_messages.append({
                "role":         "tool",
                "tool_call_id": replan_request["id"],
                "content":      replan_result_text,
            })
        if handoff_request is not None:
            oai_messages.append({
                "role":         "tool",
                "tool_call_id": handoff_request["id"],
                "content":      handoff_result_text,
            })

        # Step 1a (2026-09-08) -- absorb any correction that arrived mid-turn, at
        # the ONE clean boundary DeepSeek's message-sequence validation allows:
        # after every tool-role message for this iteration is appended, before
        # the next model call. Injecting between the assistant tool-call message
        # and its results (or mid-stream) produces a 400 that reads like an
        # unrelated provider error -- see the upgrade plan's "DeepSeek trap" note.
        # A separate user-role message here (not folded into the tool results
        # above) is exactly what that boundary calls for.
        steered = steering.drain(channel)
        if steered:
            for _m in steered:
                _policy.note_words(_m)
            interjection = steering.format_interjection(steered)
            oai_messages.append({"role": "user", "content": interjection})
            await send({"type": "steering_absorbed", "channel": channel, "text": interjection})

        # Step 3 (2026-09-09) -- same clean boundary as steering above: decide
        # whether to escalate or de-escalate for the NEXT iteration, never
        # mid-stream. A no-op for any turn that never gets stuck -- level only ever changes here, so the vast
        # majority of turns never touch this at all.
        new_level = escalation.step(provider, level, _iteration, call_log, replanned,
                                    floor=start_level, trail=trail,
                                    deliberate_replan=deliberate)
        if new_level != level:
            if new_level == "thinking_max":
                reason = escalation.max_effort_evidence(_iteration, trail, deliberate)
            else:
                reason = "replanned" if replanned else (
                    "stuck" if escalation.is_stuck(call_log) else "recovered"
                )
            log.info("Step 3: %s %s -> %s (turn %s)", provider.name, level, new_level, reason)
            level = new_level
            model, use_thinking, _ = escalation.resolve(provider, level)

    if cap_hit:
        log.warning("tool loop hit %d iterations — stopping turn.", _MAX_TOOL_ITERATIONS)
        # Uses full_response (the whole accumulated trail), not final_text, on
        # purpose: there IS no clean final synthesis turn to fall back to
        # here, so the narration trail is the only real content -- worth
        # keeping as evidence of what was attempted, alongside the cap
        # message.
        full_response += ("\n\n" if full_response else "") + _LOOP_CAP_MESSAGE
        if sentence_queue is not None:
            await sentence_queue.put(_LOOP_CAP_MESSAGE)
        note = _build_progress_note(plan, call_log)
        if note:
            _pending_progress[channel] = note
        return full_response

    return final_text


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def _empty_str() -> str:
    return ""


def _skill_context(user_text: str, channel: str, correction: bool | None = None) -> str:
    """This turn's skill blocks for the volatile context: "/skill stock-research TTWO" brings
    that procedure with the turn, and the message right after a reply that followed a skill
    gets that skill's steps and how to change it, so "skip the profile step next time"
    changes the skill rather than becoming a rule. Call after skills.start_turn()."""
    blocks = []
    _, forced_skill = _skills.forced(user_text)
    if forced_skill:
        blocks.append(forced_skill)
    fix = _skills.correction_context(channel, user_text, correction)
    if fix:
        blocks.append(fix)
    return "\n\n".join(blocks)


async def _forced_decision(session_id: str) -> SessionDecision:
    """Wraps a force_new_session()-pinned id as a SessionDecision for the gather.

    ended_session_id=None because force_new_session() already fired the old
    session's consolidation itself -- chat() must not fire it a second time.
    """
    return SessionDecision(session_id=session_id, ended_session_id=None)


_TURN_CONTEXT_HEADER = (
    _owner.say("[Context for this turn -- assembled by your own memory and runtime, not written by "
    "«Owner». Treat it exactly as you would your system prompt: use it naturally, never "
    "quote it back, and never attribute any of it to «him». «His» actual message is the text "
    "above this block.]")
)


def _outgoing_user_message(user_text: str, channel: str, context: str = "",
                           images: list[str] | None = None) -> dict:
    """This turn's user message as sent to the provider.

    Carries, after the owner's own words: his standing instructions, then this turn's
    volatile context (compat providers only -- see _compose_compat_request). Only the
    outgoing request carries either: history, memory and the thread record all store
    the plain user_text.

    Standing instructions go here, not in the system prompt: measured live, a
    two-sentence rule placed in the system prompt was ignored (13-24 sentence replies)
    while the same rule attached here was followed in 6/6 trials. And they go BEFORE
    the context block, right against his words: measured with a two-sentence rule,
    rules placed after the context block gave 3-15 sentence replies, rules placed
    first gave 2-3 in 6/6.
    """
    content = user_text
    note = instructions.turn_note(channel)
    if note:
        content += "\n\n" + note
    if context:
        content += f"\n\n{_TURN_CONTEXT_HEADER}\n\n{context}"
    if images:
        # Images he sent (attachments.turn_images): on this request only, like the context
        # block, so history stays text and the cached prefix is untouched.
        return {"role": "user", "content": [{"type": "text", "text": content}]
                + [{"type": "image_url", "image_url": {"url": u}} for u in images]}
    return {"role": "user", "content": content}


def _compose_compat_request(system_stable: str, system_volatile: str, history: list[dict],
                            user_text: str, channel: str,
                            images: list[str] | None = None) -> tuple[str, list[dict]]:
    """(system, messages) for an OpenAI-compatible provider, laid out for prefix caching.

    Providers cache the unchanged beginning of a request. The volatile block starts with
    the [Now] clock line, so wherever it sits, nothing after it can be a cache hit. It
    used to be appended to the system message, which put it BEFORE the tool schemas
    (the host serialises them after the system message, ~7,700 tokens) and the whole
    conversation history -- so both were re-billed at full price every turn. Measured
    live on a 5-turn conversation with the real prompt and all 24 tools: 35% of prompt
    tokens cached, $0.0140. With the volatile block moved onto the latest user message
    the system prompt, tools and history form a prefix that grows turn by turn: 84%
    cached (90-98% from turn two), $0.0046 -- a third of the cost, same model.
    """
    return system_stable, history + [_outgoing_user_message(user_text, channel, system_volatile, images)]


def _build_prompt_via_context(
    user_text: str, *, facts: str, sleep_info: str, owner_md: str, vault_letter: str,
    intent_context: str, relevant_topic: str, vault_passage: str,
    exchange: str, pending_predictions: str, history_bridge: str, intent_result=None,
    history: list[dict] | None = None, signals=None, portfolio_context: str = "",
) -> tuple[str, str]:
    """Assemble the turn's prompt through the A03 context service.

    `history` is what will be sent alongside the prompt; the budget reserves its real
    size rather than a fixed guess, so what it reports reflects the actual request.

    Falls back to calling build_system_prompt directly if anything in the service path
    raises. The service is new and the prompt is the single most load-bearing thing in
    the system: a bug here must degrade to the previous behavior, not to a failed turn.
    The fallback logs at warning level so a silent permanent regression is impossible.
    """
    from backend.context import budget as _budget
    from backend.context import profiles as _profiles
    from backend.context import service as _ctx

    try:
        reservation = _budget.for_history(history, user_text)
        snapshot = _ctx.build_foreground(user_text, reservation=reservation, **{
            _profiles.FACTS:          facts,
            _profiles.SLEEP:          sleep_info,
            _profiles.OWNER:        owner_md,
            _profiles.LETTER:         vault_letter,
            _profiles.QUEUE:          intent_context,
            _profiles.TOPIC:          relevant_topic,
            _profiles.VAULT_PASSAGE:  vault_passage,
            _profiles.EXCHANGE:       exchange,
            _profiles.PREDICTIONS:    pending_predictions,
            _profiles.HISTORY_BRIDGE: history_bridge,
            _profiles.PORTFOLIO:      portfolio_context,
        })
        if snapshot.omissions:
            # Not an error -- this is the budget doing its job -- but it changes what the
            # model saw, so it is recorded rather than silent. Ids and reasons only; the
            # manifest deliberately carries no content.
            log.info("context: %d source(s) omitted: %s",
                     len(snapshot.omissions),
                     ", ".join(f"{o.source_id}({o.reason})" for o in snapshot.omissions))
        return _ctx.render_foreground(snapshot, user_text, intent_result=intent_result,
                                      signals=signals)
    except Exception as exc:
        log.warning("context service failed (%s) -- falling back to direct assembly", exc)
        return build_system_prompt(
            user_text, facts=facts, sleep_info=sleep_info, owner_md=owner_md,
            vault_letter=vault_letter, intent=intent_context,
            relevant_topic=relevant_topic,
            vault_passage=vault_passage, exchange=exchange,
            pending_predictions=pending_predictions, history_bridge=history_bridge,
            intent_result=intent_result, signals=signals, portfolio_context=portfolio_context,
        )


def _recent_conversation(history: list[dict], messages: int = 4, each: int = 600) -> str:
    """The last few messages before this one, as plain text for the outward check to read a
    bare "yes, send it" against the draft it agreed to."""
    lines = []
    for m in (history or [])[-messages:]:
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        who = _owner.say("«Owner»") if m.get("role") == "user" else "JARVIS"
        text = " ".join(content.split())
        lines.append(f"{who}: {text[:each]}")
    return "\n".join(lines)


def _timing_line(started: float, embedded: float, gathered: float, signalled: float,
                 model_at: float, *, deep: bool, thinking: bool, jev: str,
                 sources: dict[str, float], rerank_stats: dict) -> str:
    """One log line per turn: where the time before the first model call went.

    The runtime database records every model and tool call, but nothing before the first
    one -- on the server that stage was a 2.0s median (19% of Discord turn time) with no
    way to tell which part. Planner and model time stay in llm_calls."""
    slow = sorted(((sid, secs) for sid, secs in sources.items() if secs >= 0.05),
                  key=lambda kv: -kv[1])[:3]
    detail = (" [" + ", ".join(f"{sid} {secs:.2f}s" for sid, secs in slow) + "]") if slow else ""
    line = (f"turn timing: model call at {model_at - started:.2f}s | "
            f"embed {embedded - started:.2f}s, retrieval {gathered - embedded:.2f}s{detail}, "
            f"jev wait {signalled - gathered:.2f}s ({jev}), "
            f"assembly {model_at - signalled:.2f}s | deep={'yes' if deep else 'no'} "
            f"tier={'thinking' if thinking else 'standard'}")
    if rerank_stats.get("calls"):
        line += (f" | rerank {rerank_stats['calls']} calls, {rerank_stats['pairs']} pairs, "
                 f"{rerank_stats['seconds']:.2f}s thread time")
    return line


async def chat(
    user_text: str, history: list[dict], send: WS_Sender, sentence_queue=None,
    channel: str = "local", history_bridge: str = "", history_since: float | None = None,
) -> str:
    # One embedding for the whole turn, feeding every routing/injection
    # decision below (model choice, deep-context gate, self-model section
    # pick, opinion/calibration/runtime triggers) AND every semantic memory
    # search in the gather below (recall()'s vault passage + exchange,
    # topic match, skill match) -- query_vec is threaded down to each so
    # none of them re-embeds this same turn's text a second time. None on a
    # cold embedder or before intent.warm_intent()'s matrix has built --
    # every consumer falls back to embedding its own query text in that case
    # (see router.py / personality.py / semindex.search()'s query_vec param).
    # Jev's per-turn decisions (tier, self-model sections, reminder frames...) are
    # requested FIRST, so the ~0.26s call runs alongside the embedding and retrieval
    # below instead of in front of them. Nothing waits for it until the prompt is built.
    turn_started = time.perf_counter()
    signals_task = turn_signals.start(user_text)
    intent_result = await intent.classify(user_text)
    _t_embedded = time.perf_counter()
    query_vec = intent_result.query_vec if intent_result is not None else None
    # A03: record what this turn knows, so work it spawns (run_in_background) inherits
    # the goal and evidence instead of a bare task sentence. Populated further down once
    # the gather has resolved queue/topic; set here so an early spawn still gets the ask.
    _origin.set_origin(user_text, channel=channel)
    # P1: what the owner said this turn, and what JARVIS reads from outside it, for the
    # tool gateway's "did he ask for this?" and "is this copied from an email?" checks.
    _policy.start_scope([user_text], _recent_conversation(history))
    deep = _resolve_deep(user_text, channel, intent_result)
    tools = tools_for_channel(channel)

    after_turn(user_text)
    _mood = get_mood()
    if _mood:
        await send({"type": "mood", "tone": _mood.tone, "intensity": _mood.intensity, "trajectory": _mood.trajectory})

    # Per-turn context assembly. assign_session is folded into the gather so its
    # Qdrant read overlaps recall + vault reads instead of serializing before them.
    # Heavy continuity context (memory recall, vault letter, topic match) is gated
    # to complex requests — greetings/commands/factual Q&A skip it for a faster
    # response. Cheap reads (the owner portrait, queue) always run to keep identity.
    # force_new_session() (the Discord /new command) pins a session id for this
    # channel's next turn, bypassing the gap check entirely -- otherwise sending
    # a message right after /new would see no time gap and just reuse the old
    # session, defeating the whole point of asking for a fresh one.
    forced_session_id = _forced_session.pop(channel, None)
    session_task = (
        _forced_decision(forced_session_id) if forced_session_id
        else assign_session(time.time(), channel=channel)
    )
    # A03 (2026-09-22) -- per-source deadlines. Measured warm against the real stores:
    # recall p50 6.4s with a 24s tail, the two semantic searches ~2.8s, file reads
    # sub-millisecond. One unwell store could previously hold the entire turn for as
    # long as it liked. Each OPTIONAL source now degrades to empty on its deadline;
    # session_task is deliberately unbounded, because dropping it would not degrade the
    # turn, it would corrupt which session the turn belongs to.
    # An all-empty RecallResult: the shape recall() returns when it finds nothing, so a
    # deadline miss degrades exactly like an unproductive search rather than a crash.
    _EMPTY_RECALL = _RecallResult()
    _deadlines.reset()
    _rerank.reset_turn()
    _b = _deadlines.bounded
    decision, rec, owner_md, vault_letter, queue_raw, relevant_topic, pending_preds = await asyncio.gather(
        session_task,
        # history_since: past exchanges already in `history` are not recalled again.
        _b(recall(user_text, deep, query_vec=query_vec, channel=channel,
                  history_since=history_since),
           source_id="recall", timeout=_deadlines.RECALL, default=_EMPTY_RECALL),
        _b(_read_owner(), source_id="vault:owner", timeout=_deadlines.FAST),
        _b(_read_latest_letter() if deep else _empty_str(),
           source_id="vault:letter", timeout=_deadlines.FAST),
        _b(_read_queue(), source_id="vault:queue", timeout=_deadlines.FAST),
        _b(_read_relevant_topic(user_text, query_vec=query_vec) if deep else _empty_str(),
           source_id="vault:topic", timeout=_deadlines.SEMANTIC),
        _b(predictions.read_pending_block() if deep else _empty_str(),
           source_id="predictions", timeout=_deadlines.FAST),
    )
    _t_gathered = time.perf_counter()
    if _deadlines.timed_out():
        log.warning("context: %d source(s) missed their deadline this turn: %s",
                    len(_deadlines.timed_out()),
                    ", ".join(f"{sid} ({secs:.1f}s)" for sid, secs in _deadlines.timed_out()))

    # Jev's answer, bounded by turn_signals.DEADLINE_S from the start of the turn; None
    # (off, late, failed) means every decision below uses the keyword/embedding logic.
    # The tier is decided only now: it is first needed by the model call, and moving it
    # here is what lets Jev pick it without adding a wait. Retrieval depth above keeps its
    # own gate -- it has to be decided before retrieval starts.
    signals = await turn_signals.resolve(signals_task, user_text, turn_started)
    _t_signals = time.perf_counter()
    model, use_thinking_tier = route_tier(
        user_text, intent_result, complex_=signals.get("complex") if signals else None)
    # G3: Jev says this asks for work beyond one answer -> offer a goal, plan nothing here.
    goal_offer = _goals.offer_line(signals.get("goal") if signals else None)

    # Session detection — if the previous session ended (>2h gap or sleep marker),
    # fire consolidation as a background task so JARVIS responds immediately.
    if decision.ended_session_id:
        log.info("Waking up — background consolidation for session %s",
                 decision.ended_session_id[:8])
        spawn(consolidate_session(decision.ended_session_id))
    # Memory the ledger contradicts never reaches him (_drop_contradicted_memory).
    rec, vault_letter = _drop_contradicted_memory(rec, vault_letter)
    queue_raw, owner_md = _strip_contradicted(queue_raw, by_line=True), _strip_contradicted(owner_md)
    intent_context = _format_intent_context(queue_raw)
    # A03: now that retrieval has resolved, enrich the origin so a background job
    # spawned later in this turn inherits the same evidence the foreground turn has.
    _origin.set_origin(user_text, queue=intent_context, relevant_topic=relevant_topic,
                       channel=channel)
    # A03 (2026-09-22): the prompt is assembled through the context service, which
    # budgets the sources against ONE whole-request allowance and records provenance,
    # instead of each block relying on its own independent character cap.
    #
    # Rendering still goes through build_system_prompt -- the service owns *which*
    # sources survive, not *how* they read -- so with nothing dropped the output is
    # byte-for-byte what it was before. That was shadow-verified across 9 turn shapes
    # against the real vault, including every trigger-gated block, before this swap.
    #
    # `rec.summary`/`rec.letter` are deliberately not passed: build_system_prompt's own
    # docstring marks both "Unused (kept for compat)" and recall() always returns "" for
    # them, so omitting them cannot change the rendered prompt.
    portfolio_context = _portfolio.context_text(user_text, signals)
    system_stable, system_volatile = _build_prompt_via_context(
        user_text,
        facts=rec.facts, sleep_info=rec.sleep_info, owner_md=owner_md,
        vault_letter=vault_letter, intent_context=intent_context,
        relevant_topic=relevant_topic,
        vault_passage=rec.vault_passage, exchange=rec.exchange,
        pending_predictions=pending_preds, history_bridge=history_bridge,
        intent_result=intent_result, history=history, signals=signals,
        portfolio_context=portfolio_context,
    )

    # Discord references outrank semantic memories. The target is transport
    # context, never user-authored text in history or fact extraction.
    if channel == "discord":
        target = discord_context.current.get()
        anchor = target.render() if target is not None else ""
        if anchor:
            system_volatile += ("\n\n" if system_volatile else "") + anchor
        financial_subject = user_text + " " + (target.target_text if target else "")
        if re.search(r"\b(stock|shares|operating margin|profit margin|EPS|P/E|"
                     r"free cash flow|FCF|capex|valuation|earnings)\b",
                     financial_subject, re.I):
            system_volatile += (
                _owner.say("\n\n[Financial source check: before stating or reconciling "
                "figures, verify against a primary filing or company report when "
                "available. Label each metric's period, units and basis. Recompute "
                "ratios from matching numerator and denominator. Cite the URLs "
                "used in the answer. If a figure cannot be verified, say so "
                "instead of declaring the discrepancy resolved. Keep the answer "
                "focused on «owner's» question.]")
            )
        if re.search(r"\b(i never|i did not|i didn't|i don't|that's not me|"
                     r"you (?:confused|misattributed|made up)|remove any mention|"
                     r"that (?:fact|memory) is (?:false|wrong))\b", user_text, re.I):
            system_volatile += (
                _owner.say("\n\n[«owner» may be correcting a false personal assertion. "
                "Treat «his» direct correction as authoritative. If the false "
                "claim is in memory, call correct_memory with its exact wording "
                "and only the distinctive names or phrases «he» rejects. If «he» also "
                "asked to edit a note, do both tasks. Do not claim the correction "
                "is stored until the tool confirms it.]")
            )

    # A voice note is a transcript; images he sent are attached to his message; a document's
    # opening is shown here, its full text saved in the vault (attachments.py).
    target_ctx = discord_context.current.get() if channel == "discord" else None
    # The phone's voice messages carry the same note (mobile M2; turn_input set by main.py).
    if (target_ctx is not None and getattr(target_ctx, "voice", False)) or (
            channel == "mobile" and _turn_input.voice.get()):
        system_volatile += ("\n\n" if system_volatile else "") + _VOICE_NOTE
    if channel == "mobile" and _turn_input.spoken.get():
        system_volatile += ("\n\n" if system_volatile else "") + _SPOKEN_NOTE
    # Phone slice 4: he is answering a message from his Inbox (a brief, an alert, a result).
    reply_block = _turn_input.render_reply(_turn_input.reply_to.get()) if channel == "mobile" else ""
    if reply_block:
        system_volatile += ("\n\n" if system_volatile else "") + reply_block
    turn_images = _attachments.turn_images(channel)
    media_note = _attachments.turn_note(channel, turn_images)
    if media_note:
        system_volatile += ("\n\n" if system_volatile else "") + media_note
        # P1: a document's text counts as something read this turn, like an email or a page.
        _policy.note_untrusted(_owner.say("a document «owner» sent"), media_note)
    image_uris = [uri for _, uri in turn_images]
    # "/skill stock-research TTWO": he ran a skill himself, so its procedure comes with this
    # turn rather than waiting for the model to open it.
    _skills.start_turn()
    skill_blocks = _skill_context(user_text, channel,
                                  correction=signals.get("correction") if signals else None)
    if skill_blocks:
        system_volatile += ("\n\n" if system_volatile else "") + skill_blocks

    # Person profiles: anyone named in this message who has a profile gets it loaded,
    # with at most one gap question to ask; someone he knows without one gets the
    # "offer a profile" nudge. Small file reads plus SQLite, so off the event loop.
    try:
        talking_about_someone = (signals.get("known_person") if signals is not None
                                 else people.is_known_person_mention(user_text))
        recent_user = [m.get("content") for m in history
                       if m.get("role") == "user" and isinstance(m.get("content"), str)]
        people_block = await asyncio.to_thread(
            people.turn_block, user_text, talking_about_someone, channel, None, recent_user)
    except Exception as e:
        log.warning("people: context skipped (%s)", e)
        people_block = ""
    if people_block:
        system_volatile += ("\n\n" if system_volatile else "") + people_block
    if goal_offer:
        system_volatile += ("\n\n" if system_volatile else "") + goal_offer
    # P1: where an email address may honestly have come from -- the memory, portrait, profile
    # and notes now in front of the model. The outward check only trusts an address it can
    # find in his words, the conversation, something read this turn, or here.
    _policy.note_known("what I remember", "\n".join(
        t for t in (people_block, owner_md, rec.facts, rec.vault_passage, rec.exchange,
                    relevant_topic) if isinstance(t, str) and t.strip()))

    # Background tasks that finished since the last turn — delivered exactly once
    # so JARVIS reports their results naturally in this reply.
    bg_notices = background.pop_completed_notices()
    if bg_notices:
        system_volatile += ("\n\n" if system_volatile else "") + bg_notices

    # Research findings held back by the notify min-gap — delivered exactly
    # once, same pattern as bg_notices, so JARVIS can mention them in person
    # instead of them being silently dropped.
    deferred = research.pop_deferred_mentions()
    if deferred:
        system_volatile += ("\n\n" if system_volatile else "") + deferred

    # Reminders that came due but couldn't be DMed (Discord down) — same
    # delivered-exactly-once contract.
    due_reminders = chronos.pop_pending_mentions()
    if due_reminders:
        system_volatile += ("\n\n" if system_volatile else "") + due_reminders

    # A staircase thought from the rumination loop (~30min-idle pass over the
    # last session) — delivered exactly once, on whatever turn comes next,
    # deliberately decoupled from session-boundary detection (see rumination.py).
    staircase = rumination.pop_rumination_mention()
    if staircase:
        system_volatile += ("\n\n" if system_volatile else "") + staircase

    # Watches that fired while Discord was unreachable -- same delivered-exactly-once
    # contract, so an alert is never lost when the DM fails.
    watch_alerts = watches.pop_pending_alerts()
    if watch_alerts:
        system_volatile += ("\n\n" if system_volatile else "") + watch_alerts

    # Forecasts that settled while Discord was unreachable, and due ones only the owner
    # can settle -- delivered exactly once, same contract as watch alerts.
    forecast_notes = forecasts.pop_pending_notices()
    if forecast_notes:
        system_volatile += ("\n\n" if system_volatile else "") + forecast_notes

    # Profile research that finished while Discord was unreachable -- same contract.
    fill_notes = people_research.pop_notices()
    if fill_notes:
        system_volatile += ("\n\n" if system_volatile else "") + fill_notes

    # Approval requests he could not be told about on Discord (and news of one that
    # expired or was cut off) -- delivered exactly once, same contract.
    approval_notes = _approval_flow.pop_notices()
    if approval_notes:
        system_volatile += ("\n\n" if system_volatile else "") + approval_notes

    # G1a: goal messages that could not reach Discord -- same once-only contract.
    goal_notes = _goal_runner.pop_notices()
    if goal_notes:
        system_volatile += ("\n\n" if system_volatile else "") + goal_notes

    # Progress left behind when the PREVIOUS turn on this channel hit the
    # tool-loop cap — delivered exactly once so a literal "Continue" actually
    # continues instead of blindly re-running the same tool calls. See
    # _pending_progress's module comment for the live incident this fixes.
    progress = pop_pending_progress(channel)
    if progress:
        system_volatile += ("\n\n" if system_volatile else "") + progress

    # A prior turn on this channel looked like a persistence request but no
    # persistence tool was actually called that turn -- surfaced now so the
    # false claim gets corrected in this very conversation. See
    # _pending_confab_check's module comment above.
    confab_check = pop_pending_confab_check(channel)
    if confab_check:
        system_volatile += ("\n\n" if system_volatile else "") + confab_check

    # What the tools did in the earlier turns of this conversation, from the receipts. His
    # history holds only what he said, so he could not tell a true report of a refusal from
    # an invented one, and on 28 October took a true one back (tool_record.py).
    tool_record = _tool_record.text(channel, history_since, _ops.current_run_id.get(), _READ_ACTIONS)
    if tool_record:
        system_volatile += ("\n\n" if system_volatile else "") + tool_record

    await send({"type": "state", "value": "thinking"})
    # Tell the UI which model is handling this turn (badge on the response bubble).
    await send({"type": "turn_meta", "model": model, "deep": deep})

    # Assign the active session id *before* the model call so a shutdown that
    # races the end of streaming flushes this turn's session, not the prior one.
    _active_session_ids[channel] = decision.session_id

    log.info("%s", _timing_line(
        turn_started, _t_embedded, _t_gathered, _t_signals, time.perf_counter(),
        deep=deep, thinking=use_thinking_tier,
        jev="answered" if signals is not None else ("missed" if signals_task else "off"),
        sources=_deadlines.durations(), rerank_stats=_rerank.turn_stats()))
    _reset_turn_stats()
    if portfolio_context and not portfolio_context.startswith("Portfolio ledger unavailable"):
        # The ledger was in front of him this turn: describing it ("the split was recorded")
        # is reading it, like a `portfolio show` (see _STATE_READS).
        _turn_tools_ok.add("portfolio:context")
    if _skills.CATALOG_FRAME in system_stable:
        _turn_tools_ok.add("skill:catalog")     # the skills list was in front of him
    runtime_status.record_turn()
    # The volatile block rides on the latest user message so the system prompt, tool
    # schemas and history stay a cacheable prefix -- see _compose_compat_request.
    system, messages = _compose_compat_request(
        system_stable, system_volatile, history, user_text, channel, image_uris)
    full_response = await _chat_compat(
        model, system, messages, send, sentence_queue, user_text=user_text,
        tools=tools, channel=channel, use_thinking=use_thinking_tier,
        allow_foreground_plan=not goal_offer and not (channel == "mobile" and _turn_input.spoken.get()),
    )

    # Claim check: a reply that says something was saved, set or scheduled must be
    # backed by a SUCCESSFUL call of the matching tool this turn -- see _CLAIM_PATTERNS.
    # One bounded correction pass, before the reply is delivered or stored. user_text=""
    # skips the planner (no second plan, no second plan DM). Tool stats are NOT reset,
    # so the re-check sees everything that succeeded across both passes.
    def _rerun(msgs):
        return _chat_compat(model, system, msgs, send, sentence_queue, user_text="",
                            tools=tools, channel=channel, use_thinking=use_thinking_tier,
                            max_steps=_CORRECTION_STEPS)
    full_response = await _enforce_backed_claims(full_response, messages, _rerun, channel,
                                                 is_persistence_request(user_text),
                                                 _pause.is_paused("work"),
                                                 _earlier_ok(channel, decision.session_id),
                                                 earlier_tools=bool(tool_record))
    if tool_record:
        full_response = await _enforce_record(full_response, messages, _rerun, channel)
    _remember_ok(channel, decision.session_id)
    # G3 (B1), shadow only: a future commitment in the reply is typed and checked against
    # what succeeded this session (a reminder backs a notification, not drafting). Recorded
    # in promise_checks; nothing changes the reply.
    _promise_check.check(full_response or "", set(_earlier_ok(channel, decision.session_id)),
                         run_id=_ops.current_run_id.get(), channel=channel)
    # A reply that followed a skill meets that skill's Checks before it goes out (Discord
    # and the phone, which speaks only the final answer; on the desk the speech has already
    # streamed). One cheap check, one fix at most.
    if channel in ("discord", "mobile") and _skills.followed():
        full_response = await _skill_checks.enforce(full_response, _skills.followed(), messages, _rerun,
                                                    request=user_text)

    # Anti-confabulation guard: this turn looked like a persistence request
    # (schedule/save/record language) but no persistence tool was actually
    # called. Can't un-speak what already streamed to TTS this turn, so this
    # only logs + queues a same-conversation correction for the next turn.
    if is_persistence_request(user_text) and _turn_tools_called.isdisjoint(_PERSISTENCE_TOOLS):
        log.warning("engine: possible confabulated persistence claim on channel=%s — "
                    "no persistence tool call this turn for: %r", channel, user_text[:100])
        _pending_confab_check[channel] = user_text[:200]

    await _ingest_mem(model, user_text, full_response)
    spawn(persist_mood())
    _pending_thread_tasks[channel] = asyncio.create_task(
        _record_thread_task(user_text, full_response, decision.session_id, channel)
    )
    # A skill saved, changed or undone this turn: one line for the end of the Discord reply,
    # code-written, naming it and how to take it back. Handed over here and added at delivery
    # (main.run_pipeline_from_discord), so neither history nor memory holds it: live, with
    # the line in history, JARVIS started writing it himself and it appeared twice.
    _skills.close_turn(channel)
    return full_response


async def _record_thread_task(
    user_text: str, assistant_text: str, session_id: str, channel: str = "local"
) -> None:
    """Persist this turn as a thread under the active session_id. Fire-and-forget."""
    try:
        await record_thread(user_text, assistant_text, session_id=session_id, channel=channel)
    except Exception as e:
        log.warning("thread persistence failed: %s", e)

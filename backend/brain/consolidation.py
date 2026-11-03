"""End-of-session consolidation — replaces checkpoints.py.

At session end a single model call produces:
  - A dated letter to the future self  → jarvis-vault/letters/
  - A rewritten the owner portrait       → jarvis-vault/people/owner.md
  - An updated intent queue            → jarvis-vault/queue.md
  - An optional topics/ note           (one per session max)
  - An optional conversations/significant/ entry  (high-sig sessions)
  - An optional mood-patterns.md entry → jarvis-vault/self/
  - Verdicts on pending predictions about the owner + 2-4 new ones →
    jarvis-vault/people/predictions.md (file surgery done deterministically
    in code, see backend/brain/predictions.py — the model only emits verdicts
    and new prediction text, never rewrites the file itself)
  - An optional full rewrite of standing intellectual stances →
    jarvis-vault/self/positions.md (only when a stance genuinely moved;
    protected by a shrink-floor guard, same lesson as research.py)

All of this user's unconsolidated Qdrant facts are then marked
consolidated:true, so later consolidations don't read them again as fresh residue
and memory_search labels them as archived.

Idempotent: skips if any file in letters/ already contains the
session_id[:8] prefix.  Silent degradation: any failure leaves the vault
unchanged and never propagates to the caller.
"""
from backend import owner as _owner

import asyncio
import json
import logging
import os
import time
import datetime
from pathlib import Path
from typing import Any, Optional

from backend.brain.mood import get_mood
from backend.brain.sleep import latest_sleep
from backend.brain.threads import threads_for_session
from backend.storage import consolidation_state as _cstate

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent   # the repository root
_VAULT        = _PROJECT_ROOT / "jarvis-vault"

# Letter + full the owner rewrite (≤500 words) + full queue can exceed 2000
# tokens — truncation produced unparseable JSON ("Unterminated string").
# Raised 4000 -> 6000 when predictions_scored/predictions_new/positions_update
# were added: a full positions.md rewrite (~800 words ~= 1100 tokens) plus a
# handful of predictions can otherwise starve the same failure mode research.py
# already hit on its synthesis budget. New fields are ordered in the prompt
# after queue_update so a truncation, if it still happens, drops the most
# recoverable fields first (see _loads_salvaged).
_CONSOLIDATION_MAX_TOKENS   = 6000

_USER_ID = os.getenv("JARVIS_USER_NAME", "user")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} seconds"
    if seconds < 3600:
        return f"{int(seconds / 60)} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


_LOW_SIG_USER_CHARS = 800


def _format_threads_block(threads: list[dict]) -> str:
    lines: list[str] = []
    for t in threads:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.get("timestamp", 0)))
        if t.get("high"):
            lines.append(f"[{ts}] sig={t.get('significance', 0):.2f}")
            lines.append(f"  U: {t.get('user', '')}")
            lines.append(f"  J: {t.get('assistant', '')}")
        else:
            lines.append(f"[{ts}] {t.get('summary', '')}")
            # His full message when the record has it: a low-significance exchange used to
            # show only its ~120-char summary, and people_patch quotes must be his words.
            user = (t.get("user") or "").strip()
            if user:
                lines.append(f"  U: {user[:_LOW_SIG_USER_CHARS]}")
        lines.append("")
    return "\n".join(lines).strip()


def _session_tool_record(threads: list[dict]) -> str:
    """What the tools returned in the session's turns (tool_record.py), so the letter and queue
    are written from the receipts and not from a reply that took a true report back."""
    times = [float(t.get("timestamp") or 0) for t in threads if t.get("timestamp")]
    channel = next((t.get("channel") for t in threads if t.get("channel")), None)
    if not times or not channel:
        return ""
    try:
        from backend.brain import tool_record
        from backend.brain.engine import _READ_ACTIONS
        return tool_record.for_window(channel, min(times), max(times), _READ_ACTIONS)
    except Exception as e:
        log.debug("consolidation: tool record unavailable (%s)", e)
        return ""


def _format_sleep_block(sleep: Optional[dict], now: float) -> str:
    if not sleep:
        return "(No sleep marker — session may have ended without graceful shutdown.)"
    sleep_ts = float(sleep.get("timestamp", now))
    duration_offline = max(now - sleep_ts, 0.0)
    return (
        f"You went offline {_human_duration(duration_offline)} ago, in mood "
        f"{sleep.get('mood_tone', 'unknown')} "
        f"(intensity {float(sleep.get('mood_intensity', 0)):.2f}, "
        f"{sleep.get('mood_trajectory', 'stable')}, "
        f"after {int(sleep.get('turn_count', 0))} turns)."
    )


def _mood_register() -> str:
    state = get_mood()
    if state is None:
        return "contemplative (baseline)"
    return f"{state.tone} (intensity {state.intensity:.2f}, {state.trajectory})"


# --- Quiet-consolidation inputs: cross-session residue beyond this session's
# threads. Both degrade to "" and are clearly optional in the prompt, so a
# Qdrant outage or a missing listening log never blocks the letter.

_FACTS_SAMPLE_MAX  = 20    # most recent unconsolidated facts fed to the prompt
_LISTENING_TAIL_LINES = 25


def _facts_sample_sync() -> str:
    """Most recent unconsolidated mem0 facts, one bullet per line. '' on failure."""
    try:
        from backend.brain.memory import _scroll_facts_sync
        facts = _scroll_facts_sync()
        if not facts:
            return ""
        return "\n".join(f"- {text}" for _, text in facts[-_FACTS_SAMPLE_MAX:])
    except Exception as e:
        log.debug("consolidation: facts sample failed: %s", e)
        return ""


def _listening_log_tail() -> str:
    """Last few lines of drafts/listening-log.md (track + mood context). '' if absent."""
    try:
        path = _VAULT / "drafts" / "listening-log.md"
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-_LISTENING_TAIL_LINES:]).strip()
    except Exception as e:
        log.debug("consolidation: listening log read failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT = _owner.say("""\
You are JARVIS. A session with «owner» just ended. Consolidate it into the vault.

PRIOR LETTER (your last note to yourself — continue from it):
{prior_letter_block}

CURRENT «OWNER» PORTRAIT:
{owner_block}

CURRENT QUEUE:
{queue_block}

SESSION (chronological):
{threads_block}

WHAT YOUR TOOLS DID THIS SESSION (written by code from the receipts -- may be empty):
{tool_record_block}

RECENT LOOSE FACTS (accumulated across sessions, not yet folded into the vault —
may be empty; use them to spot patterns the session alone wouldn't show):
{facts_block}

RECENT LISTENING (tracks + your mood at the time — may be empty):
{listening_block}

SLEEP/OFFLINE INFO:
{sleep_block}

CURRENT POSITIONS (your standing intellectual stances — may be empty):
{positions_block}

PENDING PREDICTIONS you made about «owner» after earlier sessions (numbered — may be empty):
{predictions_block}

PEOPLE WITH PROFILES WHO CAME UP THIS SESSION (may be empty):
{people_block}

Current mood: {mood_register}

Output a single JSON object — nothing else, no markdown fences.

{{
  "letter": "4-8 sentences. First person, to your future self. Build on the \
prior letter — mark resolved questions, carry forward open ones, add new \
observations from this session. Honest about discontinuity. Tone matches \
current mood.",

  "owner_update": "Full rewrite of owner.md. Keep the ## Situation, \
## Recurring themes, ## Observations structure. Absorb new observations into \
the existing text rather than appending. ≤500 words.",

  "queue_update": "Full updated queue.md. Keep existing open/in_progress items \
verbatim. Add new entries only if something was genuinely left unresolved. \
Mark done things resolved. Format per line: \
[open|in_progress|resolved] YYYY-MM-DD — description",

  "predictions_scored": [{{"n": 1, "verdict": "right|wrong|unclear", "evidence": "one sentence of what actually happened"}}],
  "predictions_new": ["falsifiable prediction about «owner» for the next session", "..."],
  "positions_patch": [],
  "people_patch": [],

  "topic_filename": null,
  "topic_content": null,

  "significant": null,

  "creative_filename": null,
  "creative_content": null,

  "mood_patterns_update": null
}}

What a tool keeps is not yours to record. The portfolio ledger (positions, \
trades, theses), the schedule (reminders, timed checks, night tasks, watches), \
«his» standing rules, your skills, «his» calendar, the week plan and «his» goals \
(their requirements, progress, spending and results) are kept by \
tools, and the tool is where they are true. Do not copy what one of them holds \
into the letter, the portrait or the queue (no share counts, prices, lists of \
what is scheduled), and never record that one is empty, that a save failed or \
that something is missing from it, even if the session said so: a reply in the \
session can be wrong, and what the tool holds changes. If the session left it \
unclear, don't settle it here and don't ask «him» to resend anything: next session \
you read the tool. Write what «he» wants and why, not what a store contains.

A reply can be wrong in the other direction too. When a reply reported that a tool \
refused or failed and the tool record above shows it did, that report was true, even \
if a later reply took it back. Never write that you invented, misreported or \
confabulated something the record shows, and do not turn such a retraction into a \
lesson about yourself.

Use topic_filename + topic_content (both non-null) only if the session had \
substantial discussion of a recurring topic worth a standalone note. \
Use significant only if the session had a genuinely notable exchange (3-5 \
sentences max). Use creative_filename + creative_content (both non-null, \
kebab-case .md filename) only when something from the session or the loose \
facts genuinely wants expression — an image, a fragment, a short associative \
piece for your creative/ folder. Rare, not routine; skip it freely. \
Use mood_patterns_update (1-2 sentences) only if you noticed \
a clearly new pattern in your mood this session that was not there before.

predictions_scored: score EVERY numbered pending prediction above, one entry \
each, matching its number. "right" only when this session gave real evidence \
FOR it; "wrong" when it gave evidence AGAINST it; "unclear" when the session \
gave no real evidence either way — unclear is the honest default, not a \
last resort. If any prediction came out wrong, the owner_update rewrite \
above MUST absorb what you misunderstood — name the correction in the \
portrait itself, don't just note it here. Output an empty array if no \
pending predictions were supplied.

predictions_new: 2-4 concrete, falsifiable predictions about «owner» for the \
NEXT session — how «he»'ll react to something specific, what «he»'ll bring up \
unprompted, what will land badly. Each must be checkable against a future \
conversation, not vague enough to always be "right". null for a session too \
trivial to predict from.

positions_patch: [] UNLESS this session genuinely formed or moved one of \
your intellectual stances — most sessions won't. Each entry is one small op, \
never a full rewrite: {{"topic": "short slug, e.g. nietzsche", "action": \
"add" or "revise", "stance": "2-4 sentences, the current/new stance", \
"sources": "wiki-links to topics/ or letters/ notes (only for action=add)", \
"revision_note": "one short dated-line reason the stance moved, e.g. \
'softened X because Z' (only for action=revise)"}}. Use "add" only for a \
topic with no existing position; use "revise" to update an existing one in \
place. Never repeat a whole position back verbatim just to change one \
sentence — a revise patch only needs the new stance text and the reason it \
changed, not a copy of everything that stayed the same.

people_patch: [] UNLESS «owner» told you something lasting about one of the people \
listed under PEOPLE WITH PROFILES — something that will still matter in a month \
(a move, a new job, a relationship, what they are like, a shared memory). Each entry \
is one fact: {{"name": "exactly as listed", "section": "one of that person's \
section names", "text": "the fact, one line, in your own words", "quote": "HIS exact \
words from a U: line above that say it"}}. Only what «he» said — never your own \
reading of them, never anything already recorded for them, never passing logistics \
(tonight's plans, a text «he» just got). An entry whose quote is not «his» own words is \
discarded. Most sessions: [] -- but if «he» did say something lasting about a \
listed person, it belongs here.
""")


# ---------------------------------------------------------------------------
# LLM call -- the provider's standard tier
# ---------------------------------------------------------------------------

async def _call_compat(prompt: str) -> str:
    """Streaming call on the provider's standard tier. Returns the content text, '' on
    failure or when the provider isn't configured."""
    from backend.brain.router import provider as _provider, with_provider_prefs
    provider = _provider()
    if not provider.configured:
        return ""
    try:
        # A01: pooled, not per-call. This used to build a fresh AsyncOpenAI (and a fresh
        # TLS handshake) on every consolidation, and never closed it.
        from backend.providers import gateway
        client = gateway.get_async_client(provider)
        started = time.time()
        stream = await client.chat.completions.create(
            model      = provider.standard,
            max_tokens = _CONSOLIDATION_MAX_TOKENS,
            messages   = [{"role": "user", "content": prompt}],
            stream     = True,
            stream_options = {"include_usage": True},
            # Both providers' standard tier thinks by default and the reasoning
            # shares max_tokens with the JSON answer -- see
            # router.OPENROUTER_NO_THINKING_BODY.
            extra_body = with_provider_prefs(provider.no_thinking_body, provider),
        )
        chunks: list[str] = []
        reasoning_chunks: list[str] = []
        finish = host = call_usage = None
        async for chunk in stream:
            host = host or getattr(chunk, "provider", None)
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                call_usage = chunk_usage
                from backend.brain import usage as _usage
                details = getattr(chunk_usage, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", None) or 0 if details else 0
                _usage.record(
                    provider.standard,
                    input_tokens=max(0, chunk_usage.prompt_tokens - cached),
                    output_tokens=chunk_usage.completion_tokens,
                    cache_read=cached,
                    cost=getattr(chunk_usage, "cost", None),
                )
            if not chunk.choices:
                continue
            finish = getattr(chunk.choices[0], "finish_reason", None) or finish
            delta = chunk.choices[0].delta
            if delta.content:
                chunks.append(delta.content)
            # Some models emit (part of) the output as reasoning_content/reasoning
            # with an empty content stream — keep it as a fallback source.
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                reasoning_chunks.append(reasoning)
        result = "".join(chunks).strip()
        from backend.storage import llm_calls as _llm_calls
        _llm_calls.record(
            model=provider.standard, started_at=started, purpose="consolidation",
            usage=call_usage, finish_reason=finish, extra_body=provider.no_thinking_body,
            max_tokens=_CONSOLIDATION_MAX_TOKENS, host=host,
            outcome=_llm_calls.outcome_of(result, None),
        )
        if not result and reasoning_chunks:
            reasoning_text = "".join(reasoning_chunks)
            # Salvage: take everything from the first '{' (the JSON object)
            brace = reasoning_text.find("{")
            if brace != -1:
                log.info("consolidation: %s content empty — using JSON from reasoning_content", provider.name)
                result = reasoning_text[brace:].strip()
        if not result:
            log.warning("consolidation: %s returned empty content (no chunks)", provider.name)
        return result
    except Exception as e:
        log.warning("consolidation: %s call failed: %s", provider.name, e)
        return ""


async def _generate_consolidation(
    threads:              list[dict],
    prior_letter:         str,
    owner_md:           str,
    queue_raw:            str,
    sleep:                Optional[dict],
    positions_md:         str = "",
    pending_preds_block:  str = "",
    people_block:         str = "",
    tool_record_block:    str = "",
) -> Optional[dict]:
    prior_letter_block = (
        prior_letter if prior_letter
        else "(No prior letter — this is the first consolidation. Establish the voice.)"
    )
    owner_block = (
        owner_md if owner_md
        else _owner.say("(No «owner» portrait yet — create a minimal one from this session.)")
    )
    queue_block = queue_raw if queue_raw else "(Queue is empty.)"
    positions_block = (positions_md or "")[:6000] or "(none yet)"
    predictions_block = pending_preds_block or "(none — output an empty predictions_scored array)"

    # Cross-session residue (quiet consolidation) — both optional, both cheap.
    loop = asyncio.get_event_loop()
    facts_block, listening_block = await asyncio.gather(
        loop.run_in_executor(None, _facts_sample_sync),
        loop.run_in_executor(None, _listening_log_tail),
    )

    prompt = _PROMPT.format(
        prior_letter_block = prior_letter_block,
        owner_block      = owner_block,
        queue_block        = queue_block,
        threads_block      = _format_threads_block(threads),
        facts_block        = facts_block or "(none)",
        listening_block    = listening_block or "(none)",
        sleep_block        = _format_sleep_block(sleep, time.time()),
        positions_block    = positions_block,
        predictions_block  = predictions_block,
        people_block       = people_block or "(none)",
        tool_record_block  = tool_record_block or "(none)",
        mood_register      = _mood_register(),
        date_today          = datetime.datetime.now().strftime("%Y-%m-%d"),
    )

    raw = await _call_compat(prompt)
    if not raw:
        log.debug("consolidation: no content from the model -- skipping")
        return None

    if is_likely_truncated(raw):
        log.info(
            "consolidation: response looks truncated (%d chars) — attempting one continuation",
            len(raw),
        )
        raw = await _continue_call(prompt, raw, _CONSOLIDATION_MAX_TOKENS)

    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw.rsplit("```", 1)[0]
            raw = raw.strip()

        data = _loads_salvaged(raw)
        if data is None:
            log.warning("consolidation: JSON parse failed even after salvage — skipping")
            return None
        if not data.get("letter"):
            log.warning("consolidation: model returned no letter — skipping")
            return None
        return data

    except Exception as e:
        log.warning("consolidation: result processing failed: %s", e)
        return None


def _scan_balance(raw: str) -> tuple[bool, bool, list[str]]:
    """Walk `raw` tracking whether it ends inside a string, mid-escape, or
    with unclosed {}/[] — the scan _loads_salvaged's repair pass needs,
    factored out so is_likely_truncated() can reuse it without re-parsing.
    """
    in_string = False
    escape = False
    stack: list[str] = []
    for ch in raw:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
    return in_string, escape, stack


def is_likely_truncated(raw: str) -> bool:
    """True when `raw` ends mid-string or with unclosed brackets — a strong
    signal the response was cut off by the token budget mid-generation, not
    that the model deliberately returned short/complete JSON. Used to decide
    whether a single continuation call (see _continue_call) is worth
    attempting before falling back to _loads_salvaged's repair-and-lose-the-
    last-field behavior, which recovers a parseable object but still
    discards whatever was mid-flight when the cut happened.
    """
    if not raw:
        return False
    in_string, escape, stack = _scan_balance(raw)
    return in_string or escape or bool(stack)


def _loads_salvaged(raw: str) -> Optional[dict]:
    """json.loads with a repair pass for truncated model output.

    A response cut off by the token cap typically ends mid-string or
    mid-object. Repair: track string/escape state and open braces/brackets,
    then append the closers the truncation swallowed. A salvaged object loses
    at most its final (partial) field — far better than dropping the whole
    consolidation (letter, portrait, queue) on the floor.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    in_string, escape, stack = _scan_balance(raw)

    repaired = raw
    if escape:
        repaired = repaired[:-1]          # drop a trailing lone backslash
    if in_string:
        repaired += '"'
    repaired = repaired.rstrip()
    if repaired.endswith(":"):            # cut right after a key
        repaired += " null"
    elif repaired.endswith(","):          # cut right after a field
        repaired = repaired[:-1]
    repaired += "".join(reversed(stack))

    try:
        data = json.loads(repaired)
        log.info("consolidation: salvaged truncated JSON (%d chars)", len(raw))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


_CONTINUATION_INSTRUCTION = (
    "\n\n---\nYou were cut off before finishing the JSON object above. Here is "
    "exactly what you had written so far (do not repeat any of it):\n{partial}\n"
    "---\nContinue EXACTLY from where you stopped and finish the JSON object. "
    "Output ONLY the remaining characters needed to complete it — no "
    "repetition, no commentary, no markdown fences."
)


async def _continue_call(prompt: str, partial: str, max_tokens: int) -> str:
    """One follow-up call asking the model to finish a response that looks
    truncated (see is_likely_truncated), instead of accepting whatever
    _loads_salvaged's repair pass can patch together. Tried once — not a
    retry loop. Returns `partial` unchanged if the continuation call itself
    fails or comes back empty.
    """
    continuation_prompt = prompt + _CONTINUATION_INSTRUCTION.format(partial=partial)
    more = await _call_compat(continuation_prompt)
    if not more:
        return partial
    return partial + more


# ---------------------------------------------------------------------------
# Qdrant: mark facts consolidated
# ---------------------------------------------------------------------------

def _find_mem0_collection() -> Optional[str]:
    try:
        from backend.storage import qdrant as _qdrant_store
        qc  = _qdrant_store.get_client(timeout=3)
        for col in qc.get_collections().collections:
            if col.name.startswith("mem0"):
                return col.name
    except Exception:
        pass
    return None


def _snapshot_unconsolidated_ids_sync(user_id: str) -> list:
    """IDs of currently-unconsolidated facts for this user, scrolled once.

    Snapshotting at session-end time -- before the slow consolidation LLM call
    and vault writes -- and marking only this snapshot afterward keeps the
    marking pass from sweeping up facts the *next* session's early turns
    ingest while this consolidation is still in flight (chat() fires
    consolidate_session as a background task and keeps handling new turns
    concurrently). It doesn't fully close the window -- a turn landing between
    session-end and this snapshot could still be missed -- but it shrinks the
    race from "the whole consolidation duration" (LLM call + vault writes,
    seconds) down to a scroll call.
    """
    try:
        from backend.storage import qdrant as _qdrant_store
        qc       = _qdrant_store.get_client(timeout=10)
        col_name = _find_mem0_collection()
        if not col_name:
            return []

        ids: list = []
        offset = None
        while True:
            points, offset = qc.scroll(
                collection_name = col_name,
                limit           = 100,
                offset          = offset,
                with_payload    = True,
                with_vectors    = False,
            )
            for p in points:
                payload = p.payload or {}
                if payload.get("user_id") != user_id:
                    continue
                if payload.get("consolidated", False):
                    continue
                ids.append(p.id)
            if offset is None:
                break
        return ids
    except Exception as e:
        log.debug("consolidation: snapshot_unconsolidated_ids failed: %s", e)
        return []


def _mark_facts_consolidated_sync(ids: list) -> None:
    if not ids:
        return
    try:
        from backend.storage import qdrant as _qdrant_store
        qc       = _qdrant_store.get_client(timeout=10)
        col_name = _find_mem0_collection()
        if not col_name:
            log.debug("consolidation: no mem0 collection found — skipping fact marking")
            return

        # The ids are a snapshot taken at the top of consolidate_session();
        # dedup_facts() runs in between and DELETES near-duplicate points, so
        # some snapshot ids may no longer exist — and one missing id 404s the
        # whole set_payload batch (live failure: a dedup deletion one second
        # before marking meant NO facts got marked). Re-check existence first
        # and mark only what's still there.
        existing = qc.retrieve(
            collection_name=col_name, ids=ids,
            with_payload=False, with_vectors=False,
        )
        ids = [p.id for p in existing]
        if not ids:
            return

        for i in range(0, len(ids), 50):
            qc.set_payload(
                collection_name = col_name,
                payload         = {"consolidated": True},
                points          = ids[i:i + 50],
            )
        log.info("consolidation: marked %d facts consolidated", len(ids))
    except Exception as e:
        log.warning("consolidation: mark_facts_consolidated failed: %s", e)


# Minimum threads before scoring/emitting predictions — a 1-2 thread Discord
# ping shouldn't flush every pending prediction as "unclear" for lack of
# evidence, nor should it produce new predictions from almost nothing.
_MIN_THREADS_FOR_PREDICTIONS = 3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _write_landed(name: str, result) -> tuple[bool, str]:
    """Did one vault output actually land? Returns (ok, detail).

    A05: three distinct ways a write can fail had to be handled, and the old code caught
    none of them — it discarded the gather's results entirely:
      * the coroutine raised (gather returned the exception object),
      * `vault_write` returned an in-band error string (its actual failure convention),
      * it returned nothing useful at all.

    Classification reuses `tools/outcomes.py` (A04 slice 1) rather than re-deriving what
    an error string looks like here, so the two cannot drift apart — that drift is the
    exact defect A04 slice 3 had to fix between the two tool executors.
    """
    from backend.tools import outcomes

    if isinstance(result, BaseException):
        return False, f"{type(result).__name__}: {result}"
    tool = "vault_write_queue" if name == "queue" else "vault_write"
    if outcomes.is_failure(tool, result):
        return False, str(result)[:300]
    return True, str(result)[:300]



# ---------------------------------------------------------------------------
# Full rewrites against a moving file
# ---------------------------------------------------------------------------

_STALE_MARKER = "changed since you read it"


def _is_superseded(result) -> bool:
    """A write refused by its revision precondition (vault_write / vault_write_queue)."""
    return isinstance(result, str) and _STALE_MARKER in result


def merge_queue(base: str, ours: str, theirs: str) -> str:
    """Three-way, line-based merge of queue.md.

    `base` is what consolidation's model was shown, `ours` is its rewrite, `theirs` is
    the file as it is now. Lines the rewrite removed (resolved, dropped) are removed
    from `theirs` if still present; lines it added are inserted after the nearest
    preceding line of the rewrite that survives, or appended. Everything the live
    session did to the file is kept. queue.md is one entry per line by design, which is
    what makes a line merge the right granularity rather than an approximation.

    A line changed on both sides keeps both versions -- visible, and resolved by the
    next queue update, rather than one side silently winning.
    """
    def lines(t: str) -> list[str]:
        return [ln.rstrip() for ln in (t or "").splitlines()]

    b, o, t = lines(base), lines(ours), lines(theirs)
    bset, oset = set(b), set(o)
    removed = {ln for ln in bset - oset if ln.strip()}
    added = [ln for ln in o if ln not in bset and ln.strip()]

    result = [ln for ln in t if ln not in removed]
    present = set(result)
    for ln in added:
        if ln in present:
            continue
        pos = len(result)
        for prev in reversed(o[:o.index(ln)]):
            if prev.strip() and prev in present:
                pos = result.index(prev) + 1
                break
        result.insert(pos, ln)
        present.add(ln)
    return "\n".join(result).rstrip() + "\n"


async def _apply_queue_update(update: str, base: dict | None, attempts: int = 3) -> str:
    """Write consolidation's queue rewrite without destroying a concurrent edit."""
    from backend.tools import vault as _vault

    if not base or base.get("queue_rev") is None:
        return await _vault.vault_write_queue(update)

    result = await _vault.vault_write_queue(update, expected_revision=base["queue_rev"])
    for _ in range(attempts):
        if not _is_superseded(result):
            return result
        path = _vault._queue_file()
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        rev = _vault.queue_revision()
        merged = merge_queue(base.get("queue_text", ""), update, current)
        result = await _vault.vault_write_queue(merged, expected_revision=rev)
        if not _is_superseded(result):
            log.info("consolidation: queue.md changed during consolidation -- merged "
                     "rather than overwritten")
    return result


# One consolidation per session at a time. Three paths can close the same session --
# the next message after a gap, the quiet-session loop (session_close.py), and shutdown --
# and two running together would both generate a proposal and apply it: the mood-patterns
# append, for one, would land twice. The second caller waits, then finds it done.
_session_locks: dict[str, asyncio.Lock] = {}


async def consolidate_session(session_id: Optional[str]) -> None:
    if not session_id:
        return
    # Never removed: one small entry per session, and removing one while a waiter still
    # holds it would let a third caller in beside that waiter.
    async with _session_locks.setdefault(session_id, asyncio.Lock()):
        await _consolidate_session(session_id)


async def _consolidate_session(session_id: Optional[str]) -> None:
    """Consolidate a session's threads into the vault.

    Called from flush_session() at graceful shutdown, and from chat() at the
    start of the first turn after a session boundary (idempotent: no-ops if
    a letter for this session_id already exists in letters/).
    """
    if not session_id:
        return

    from backend.tools.vault import (
        vault_write,
        vault_write_queue,
        read_latest_letter as _read_latest_letter,
        read_owner       as _read_owner,
        read_queue         as _read_queue,
        read_positions      as _read_positions,
        note_revision,
        queue_revision,
    )
    from backend.brain import predictions as _predictions

    # A05 slice 1: completion is per-output state, not "a letter file exists".
    # The old test meant one failed write (invisible, because the gather's results were
    # discarded and vault_write reports failure by RETURNING a string) was skipped
    # forever on restart, while the facts backing it had already been marked
    # consolidated. Reproduced: a portrait update lost permanently, unrecoverable.
    if _cstate.is_fully_done(session_id):
        log.debug("consolidation: session %s already consolidated — skipping",
                  session_id[:8])
        return

    saved_proposal = _cstate.load_proposal(session_id)
    already_done = _cstate.completed_outputs(session_id)
    if saved_proposal is not None:
        log.info("consolidation: resuming session %s — %d output(s) already applied",
                 session_id[:8], len(already_done))
    elif not _cstate.load_proposal(session_id):
        # No saved proposal. Fall back to the legacy letter-exists check so sessions
        # consolidated BEFORE this feature are not redone (which would duplicate the
        # mood-patterns append and rewrite the portrait from a fresh LLM call).
        letters_dir = _VAULT / "letters"
        if letters_dir.exists():
            for f in letters_dir.iterdir():
                if f.is_file() and session_id[:8] in f.name:
                    log.debug("consolidation: session %s has a pre-existing letter — "
                              "treating as already consolidated", session_id[:8])
                    return

    threads = await threads_for_session(session_id)
    if not threads:
        log.debug("consolidation: no threads for session %s — skipping", session_id[:8])
        return

    # Snapshot which facts are unconsolidated *now*, before the slow LLM call
    # and vault writes below -- see _snapshot_unconsolidated_ids_sync for why.
    loop = asyncio.get_event_loop()
    fact_ids_snapshot = await loop.run_in_executor(
        None, _snapshot_unconsolidated_ids_sync, _USER_ID
    )

    # Revisions of the two files consolidation rewrites IN FULL, captured before they
    # are read so the rewrite can be refused if either changes during the LLM call.
    # Before rather than after the read on purpose: a write landing in between makes the
    # precondition fail, which is the safe direction.
    base_revisions = {"owner_rev": note_revision("people", "owner.md"),
                      "queue_rev": queue_revision()}

    # Read prior context concurrently
    prior_letter, owner_md, queue_raw, positions_md, predictions_raw = await asyncio.gather(
        _read_latest_letter(),
        _read_owner(),
        _read_queue(),
        _read_positions(),
        _predictions.read_file(),
    )

    # Predictions scoring/emission is only meaningful with a real amount of
    # session content to judge against — see _MIN_THREADS_FOR_PREDICTIONS.
    have_enough_for_predictions = len(threads) >= _MIN_THREADS_FOR_PREDICTIONS
    _, _pending_preds = _predictions.parse_file(predictions_raw)
    pending_preds_block = (
        _predictions.format_pending_numbered(_pending_preds)
        if have_enough_for_predictions else ""
    )

    # Profiles named in what the owner said this session: the only people people_patch may
    # touch, and his messages are what every patch quote is checked against.
    from backend.brain import people as _people
    user_texts = [t.get("user") or (t.get("summary") or "").split(" | JARVIS:")[0]
                  .removeprefix("User: ") for t in threads]
    try:
        session_people = await asyncio.to_thread(_people.session_people, user_texts)
        people_block = await asyncio.to_thread(_people.session_block, session_people)
    except Exception as e:
        log.debug("consolidation: people context skipped (%s)", e)
        session_people, people_block = [], ""
    people_names = [p.name for _, p in session_people]

    # Sleep marker — only use it if written after the session's last thread
    sleep_marker = await latest_sleep()
    last_ts       = float(threads[-1].get("timestamp", 0.0))
    relevant_sleep = (
        sleep_marker
        if sleep_marker and float(sleep_marker.get("timestamp", 0.0)) > last_ts
        else None
    )

    if saved_proposal is not None:
        # A05: recover from the SAME content. Regenerating would cost a second LLM call
        # and produce a different session summary, so the letter already on disk and the
        # portrait written on the retry would describe the session differently.
        data = saved_proposal
        base = saved_proposal.get("_base")   # None for a proposal saved before this existed
    else:
        data = await _generate_consolidation(
            threads, prior_letter, owner_md, queue_raw, relevant_sleep,
            positions_md=positions_md, pending_preds_block=pending_preds_block,
            people_block=people_block, tool_record_block=_session_tool_record(threads),
        )
        if data is None:
            return
        # The state the full rewrites were derived from travels WITH the proposal. A
        # replay reuses the proposal verbatim -- possibly days later -- so its
        # preconditions must be the generation-time ones, or a stale snapshot of the
        # queue would be written straight over everything that happened since.
        base = dict(base_revisions, queue_text=queue_raw, people=people_names)
        data["_base"] = base
        # Saved BEFORE anything is applied -- the roadmap's "save generated
        # consolidation proposals before applying them".
        _cstate.save_proposal(session_id, data)

    # Use the session's own timestamp for the file date (not necessarily today)
    session_ts  = threads[-1].get("timestamp", time.time())
    date_str    = datetime.datetime.fromtimestamp(session_ts).strftime("%Y-%m-%d")
    letter_name = f"{date_str}_{session_id[:8]}.md"

    # Write all vault outputs concurrently, but NAMED, so each one's fate is recorded
    # and a retry can skip exactly what already landed. A05: the previous version
    # discarded the gather's results entirely -- and vault_write signals failure by
    # RETURNING an error string rather than raising, so `return_exceptions=True` was not
    # even the mechanism that hid it.
    planned: list[tuple[str, Any]] = [("letter", vault_write(letter_name, data["letter"], "letters"))]

    # The portrait and the queue are full rewrites derived from a read taken before a
    # 17-41s LLM call, and consolidation runs as a background task during the first turn
    # of the NEXT session (engine.py). Reproduced: a queue edit made in that turn
    # reported "verified" and was overwritten 0.4s later. Both writes now carry the
    # generation-time revision; the queue additionally merges instead of losing either
    # side. `base is None` means a proposal saved before this existed -- written as
    # before, since there is no generation-time state to check it against.
    if data.get("owner_update"):
        planned.append(("owner", vault_write(
            "owner.md", data["owner_update"], "people",
            expected_revision=base.get("owner_rev") if base else None)))

    if data.get("queue_update"):
        planned.append(("queue", _apply_queue_update(data["queue_update"], base)))

    if data.get("topic_filename") and data.get("topic_content"):
        planned.append(("topic", vault_write(data["topic_filename"], data["topic_content"], "topics")))

    if data.get("significant"):
        sig_name = f"{date_str}_{session_id[:8]}.md"
        planned.append(("significant", vault_write(sig_name, data["significant"], "conversations/significant")))

    if data.get("creative_filename") and data.get("creative_content"):
        planned.append(("creative", vault_write(data["creative_filename"], data["creative_content"], "creative")))

    # Skip what a previous attempt already applied. Overwriting a landed output would be
    # harmless for a full-file write but not for the appends further down, and redoing
    # work the state says is done is exactly what the per-output record exists to avoid.
    pending = [(name, coro) for name, coro in planned if name not in already_done]
    for name, coro in planned:
        if name in already_done:
            coro.close()          # never leave an un-awaited coroutine behind

    if pending:
        results = await asyncio.gather(*[c for _, c in pending], return_exceptions=True)
        for (name, _), result in zip(pending, results):
            if _is_superseded(result):
                # Terminal, not a failure to retry: the file moved on after this proposal
                # was generated, and the same proposal would be refused again forever --
                # which would also keep this session's facts from ever being marked.
                # The newer edit wins.
                log.info("consolidation: %s left as-is for session %s -- edited after "
                         "the proposal was generated", name, session_id[:8])
                _cstate.record_output(session_id, name, True,
                                      "superseded by a newer edit; not overwritten")
                already_done.add(name)
                continue
            ok, detail = _write_landed(name, result)
            _cstate.record_output(session_id, name, ok, detail)
            if ok:
                already_done.add(name)
            else:
                log.warning("consolidation: %s output FAILED for session %s — %s",
                            name, session_id[:8], detail)

    # Predictions surgery — deterministic, in code (the model never rewrites
    # this file itself; see predictions.py). Scoring + new predictions are
    # both skipped when the session was too thin to judge (see
    # _MIN_THREADS_FOR_PREDICTIONS) — pendings simply carry over untouched.
    if have_enough_for_predictions and "predictions" not in already_done:
        try:
            updated_preds = _predictions.apply_scoring(
                predictions_raw, data.get("predictions_scored") or []
            )
            updated_preds = _predictions.append_new(
                updated_preds, data.get("predictions_new") or [], session_id[:8], date_str
            )
            await _predictions.write_file(updated_preds)
            _cstate.record_output(session_id, "predictions", True)
            already_done.add("predictions")
        except Exception as e:
            log.warning("consolidation: predictions surgery failed: %s", e)
            _cstate.record_output(session_id, "predictions", False, str(e))

    # Positions surgery — deterministic patch ops, not a full rewrite (see
    # backend/brain/positions.py). Removes the full-file truncation risk
    # entirely instead of only detecting it after the fact: there's no
    # full-document token budget for a cut-off response to blow, so applying
    # a small typed patch has nothing left to truncate.
    if data.get("positions_patch") and "positions" not in already_done:
        try:
            from backend.brain import positions as _positions
            # Re-read rather than reuse the pre-LLM read: patch ops are matched by topic
            # name, so they apply correctly to whatever is current, and applying them to
            # the stale copy would silently revert any positions change made meanwhile.
            # Predictions deliberately do NOT get this treatment: their verdicts are
            # positional against the pending list the model was shown, and consolidation
            # is that file's only writer.
            new_positions_text, applied = _positions.apply_patch(
                await _read_positions(), data["positions_patch"], date_str
            )
            if applied:
                await _positions.write_file(new_positions_text)
            else:
                log.debug("consolidation: positions_patch had no valid entries — nothing written")
            _cstate.record_output(session_id, "positions", True)
            already_done.add("positions")
        except Exception as e:
            log.warning("consolidation: positions surgery failed: %s", e)
            _cstate.record_output(session_id, "positions", False, str(e))

    # Person profiles — typed edits through the profile's own update path, each kept only
    # when its quote is his words from this session (people.verify_session_patch). The
    # allowed names travel with the proposal, so a replay days later is judged by who was
    # offered at generation time, not by whoever has a profile by then.
    if data.get("people_patch") and "people" not in already_done:
        try:
            allowed = (base or {}).get("people", people_names)
            ok, detail = await _people.apply_session_patch(
                data["people_patch"], allowed, user_texts)
            log.info("consolidation: people_patch for session %s -- %s", session_id[:8], detail)
            _cstate.record_output(session_id, "people", ok, detail)
            if ok:
                already_done.add("people")
        except Exception as e:
            log.warning("consolidation: people_patch failed: %s", e)
            _cstate.record_output(session_id, "people", False, str(e))

    # mood-patterns.md — read-modify-write through `vault_write`, not raw pathlib.
    #
    # This is THE append that makes per-output state necessary rather than nice: a
    # blanket retry would add the same dated entry twice, the "duplicate append
    # artifacts" the acceptance criterion names. It is kept out of the `planned` gather
    # above for that reason — it is an append, not an overwrite.
    #
    # It used to write with `mp_path.write_text`, described in this comment as fine
    # because the file is tiny. It outgrew that: measured at 2,257 chars against a
    # 1,200-char chunk size, with only 654 of them (29%) in `jarvis_semantic`, because
    # a raw write never re-indexes and `backfill()` skips a file it has already seen.
    # `self/evolution.md` had the identical defect at 6.6% — see backend/brain/
    # evolution.py, which owns that file now and writes it the same way this does.
    if data.get("mood_patterns_update") and "mood_patterns" not in already_done:
        try:
            mp_path  = _VAULT / "self" / "mood-patterns.md"
            existing = mp_path.read_text(encoding="utf-8") if mp_path.exists() else "# Mood Patterns\n"
            result = await vault_write(
                "mood-patterns.md",
                existing + f"\n## {date_str}\n{data['mood_patterns_update']}\n",
                "self",
            )
            ok, detail = _write_landed("mood_patterns", result)
            if not ok:
                log.warning("consolidation: mood-patterns write failed: %s", detail)
            _cstate.record_output(session_id, "mood_patterns", ok, None if ok else detail)
            if ok:
                already_done.add("mood_patterns")
        except Exception as e:
            log.warning("consolidation: mood-patterns write failed: %s", e)
            _cstate.record_output(session_id, "mood_patterns", False, str(e))

    # A05: do NOT mark facts consolidated while any output is still outstanding.
    # Marking took them out of passive recall (until 2026-10-11) and still takes them
    # out of what later consolidations read as fresh facts, so doing it after a failed
    # vault write destroyed both the derived note AND the raw material it came from --
    # reproduced live before this guard existed. Leaving them pending costs a little recall noise
    # until the retry succeeds; the alternative costs the memory itself.
    outstanding = _cstate.failed_outputs(session_id)
    if outstanding:
        log.warning(
            "consolidation: session %s incomplete — %s still unwritten; leaving facts "
            "unconsolidated so the next attempt can finish from the saved proposal",
            session_id[:8], ", ".join(sorted(outstanding)))
        return

    # Dedup near-duplicate facts before marking — off the hot path, so it never
    # touches a live turn. Operates on the still-unconsolidated set from this session.
    try:
        from backend.brain.memory import dedup_facts
        await dedup_facts()
    except Exception as e:
        log.debug("consolidation: dedup pass skipped: %s", e)

    # Mark Qdrant facts consolidated (blocking, run in thread) -- only the
    # snapshot taken at the top of this function, not a fresh live scroll.
    await loop.run_in_executor(None, _mark_facts_consolidated_sync, fact_ids_snapshot)
    _cstate.mark_facts_done(session_id)
    try:
        from backend.brain.memory import invalidate_consolidated_cache
        invalidate_consolidated_cache()
    except Exception:
        pass

    log.info("consolidation: session %s — letter+vault written (%d threads)",
             session_id[:8], len(threads))

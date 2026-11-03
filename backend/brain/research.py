"""Autonomous research loop — JARVIS's "dreaming" feature.

On a paced timer, JARVIS picks ONE subject worth understanding better,
chooses a concrete question and an ANGLE for the pass, runs a
web search or two shaped by that angle, folds what he learned into the
matching topics/ note (creating it if new), and — only when the findings
genuinely change or add to what he knew — sends the owner a short unprompted
Discord message about it. Deferred curiosity, not idle chatter.

Research 2.0 — five angles, rotated per note (`last_angle:` frontmatter):
  news          — current events, publications, discoveries (the 1.0 behavior)
  deepen        — study an underdeveloped aspect of the note in real depth
  perspectives  — faithful interpretations, alternatives, and their assumptions
  connections   — bridges to adjacent thinkers/subjects
  questions     — answer an open question the note itself raises
The picker chooses subject + question + angle + why and may skip when no useful
question exists; the fallback rotates angles deterministically. Synthesis keeps
only genuine unresolved questions, explains complex ideas plainly, and may
report no progress without adding content. A per-topic cooldown
(_TOPIC_COOLDOWN_HOURS) keeps one hot
subject from monopolizing every tick (live failure: albert-camus won ~20
consecutive ticks in one night because freshness dominates the score for
~3 days and nothing stopped an immediate re-pick).

Subject selection fuses two signals into one score per candidate topic
(the "D+E fusion" design):

  freshness  — how recently and how significantly this topic came up in
               conversation. Computed from the semantic exchange index:
               best-hit similarity × significance, decayed fast
               (e^-age/3d) so it concentrates on what's live right now.
  staleness  — how long since JARVIS last actively *researched* it
               (a `last_researched:` frontmatter field on the topic note,
               distinct from any edit). Saturates toward 1.0 over ~2 weeks,
               so nothing stays forgotten forever even if never mentioned
               again. A topic with no `last_researched` at all scores the
               full 1.0 — never-researched notes are the first backstop.

  score = 0.65·freshness + 0.35·staleness

Freshness-dominant on purpose: most ticks should chase what the owner is
actually thinking about; staleness only wins when nothing is hot.

Emerging subjects (discussed but with no topics/ note yet) enter through the
picker prompt: the model sees recent exchanges alongside the scored shortlist
and may propose a brand-new subject instead — its first research pass is what
creates the note.

Pace is runtime-controllable from Discord via "/pace off|low|normal|high"
(persisted to .jarvis_pace.json at the project root — operational state, same
family as .self_model_cache.json, deliberately not a vault note). Ticks are
skipped while a conversation turn is in flight (busy check injected from
main.py) so research never competes with a live exchange.

Cost posture: the provider's standard tier (reasoning off) for both LLM calls -- one subject
per tick, at most two searches, and a minimum 2h gap between unprompted messages as a spam
guard on top of the model's own judgment.

Same DI pattern as background.py / discord_bridge.py: never imports
backend.main; the busy check and the notifier are injected at startup. Same
silent-degradation contract: any failure logs and waits for the next tick.

Hardening (2026-09-06) — subject families, and a hard close. The per-file
cooldown above stops one FILE from being re-picked immediately, but it never
stopped one SUBJECT from being re-picked immediately through a different
file: Frankl has four topic notes, Dostoevsky has seven. Each pass put one
file in cooldown while its siblings — scored from the same conversational
freshness, since they're the same subject — stayed fully eligible, so the
next tick's shortlist filled up with siblings of what was just researched.
Live failure (2026-09-04/05): four Frankl passes in one night, two of them
repeating the "perspectives" angle, because the picker could see each
sibling's own last_angle but not that a DIFFERENT sibling had just covered
that exact angle hours earlier. build_families() (above) groups topic files
into subjects via embedding similarity + shared proper-noun tokens (measured
against the real vault, not guessed — see the stopword-list comment), and
_gather_candidates now applies cooldown, the weekly cap, staleness, and
last_angle at the FAMILY level, so the shortlist holds distinct subjects, not
distinct files of the same subject. Family-level rotation writes to whichever
member is most overdue, so a pass on a hot subject also spreads across its
notes instead of hammering one. Separately, the owner can now tell JARVIS a
subject is done ("you've researched Frankl enough") via research_queue's new
'close' action — sets research_closed: true on every file in that subject's
family, an absolute veto _gather_candidates and _closed_family_conflict both
respect: no relevance or staleness score can bring a closed subject back,
including through the picker's "propose something new" path (which would
otherwise mint a fresh file for the same closed subject rather than reusing
an excluded one — a real gap this closes, not just the two failures above).
'reopen' undoes a close. Queued requests (research_queue 'add') are exempt
from the closed check — a deliberate ask isn't the kind of relevance
argument this veto exists to stop. Tests: backend/brain/test_research.py
(family grouping, cooldown/weekly-cap, aggregation, closed-topic veto).
"""
from backend import localtime as _localtime
from backend import owner as _owner

import os
import re
import math
import time
import asyncio
import logging
import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from backend.brain import pacing
from backend.brain import pause
from backend.storage import llm_calls as _llm_calls
from backend.brain.router import provider as _provider, with_provider_prefs

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_PACE_FILE    = _PROJECT_ROOT / ".jarvis_pace.json"
_TOPICS_DIR   = _PROJECT_ROOT / "jarvis-vault" / "topics"

# ---------------------------------------------------------------------------
# Pace — runtime-tunable tick frequency
# ---------------------------------------------------------------------------

_PACE_INTERVALS_S: dict[str, Optional[float]] = {
    "off":    None,
    "low":    4 * 3600.0,
    "normal": 90 * 60.0,
    "high":   30 * 60.0,
}
_DEFAULT_PACE = "normal"

_CHECK_INTERVAL_S  = 60.0          # how often the loop re-evaluates whether a tick is due
_MIN_NOTIFY_GAP_S  = 2 * 3600.0    # unprompted-DM spam guard, independent of pace


def _load_state() -> dict:
    return pacing.load_state(_PACE_FILE)


def _save_state(state: dict) -> None:
    pacing.save_state(_PACE_FILE, state, log_prefix="research")


def _update_state(mutate) -> dict:
    """Atomic read-modify-write. Use this rather than _load_state/_save_state spanning
    an await -- `/pace` and `_defer_notification` write this same file, and a research
    tick runs for minutes. See pacing.update_state."""
    return pacing.update_state(_PACE_FILE, mutate, log_prefix="research")


def get_pace() -> str:
    pace = _load_state().get("pace", _DEFAULT_PACE)
    return pace if pace in _PACE_INTERVALS_S else _DEFAULT_PACE


def set_pace(name: str) -> bool:
    """Persist a new pace. False if the name isn't a valid level."""
    if name not in _PACE_INTERVALS_S:
        return False
    state = _load_state()
    state["pace"] = name
    _save_state(state)
    log.info("research: pace set to %s", name)
    return True


def handle_pace_command(arg: str) -> str:
    """Discord '/pace [level]' handler. Bare '/pace' reports the current level."""
    arg = (arg or "").strip().lower()
    if not arg:
        pace = get_pace()
        interval = _PACE_INTERVALS_S[pace]
        detail = "research is off" if interval is None else f"one research pass roughly every {_human_interval(interval)}"
        return f"Current pace: {pace} — {detail}. Options: off, low, normal, high."
    if set_pace(arg):
        interval = _PACE_INTERVALS_S[arg]
        if interval is None:
            return "Pace set to off — no more research passes until you turn it back on."
        return f"Pace set to {arg} — roughly every {_human_interval(interval)}."
    return f"'{arg}' isn't a pace I know. Options: off, low, normal, high."


def _human_interval(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        h = int(seconds // 3600)
        return f"{h} hour" + ("s" if h > 1 else "")
    return f"{int(seconds // 60)} minutes"


def should_tick(pace: str, last_tick: float, now: float) -> bool:
    """Pure gating: is a research pass due? (Busy check is applied separately.)"""
    return pacing.interval_elapsed(last_tick, now, _PACE_INTERVALS_S.get(pace))


# ---------------------------------------------------------------------------
# Deferred notifications — findings held back by the min-notify gap
# ---------------------------------------------------------------------------
#
# Before this existed, a notify-worthy finding inside the 2h gap was logged
# ("note written, DM skipped") and the message itself was lost forever. Now
# it's parked in the state file and delivered later through whichever door
# opens first: batched into the next DM once the gap expires (a later tick's
# fresh notify, or _flush_deferred on the loop's 60s cycle), or injected into
# the next conversation turn's system prompt (pop_deferred_mentions, consumed
# by chat() the same way background-task notices are) so JARVIS mentions it
# naturally in person instead.

_MAX_DEFERRED = 5   # oldest dropped past this — hours-old findings go stale


def _sourced_notification(message: str) -> bool:
    return "Sources:" in message and bool(_URL_IN_TEXT.search(message))


def _dm_hold_reason(now: float, last_notify: float) -> str:
    """Why an unprompted finding must not be DMed right now, or "" when it may be: the owner
    paused me while this pass was running (P2), or the gap that keeps his phone quiet has not
    passed. A held finding is parked and delivered in person or after /resume."""
    if pause.is_paused("work"):
        return "paused"
    if now - last_notify < _MIN_NOTIFY_GAP_S:
        return "min gap"
    return ""


def _defer_notification(subject: str, message: str) -> None:
    state = _load_state()
    pending = state.get("deferred_notifies", [])
    pending.append({"ts": time.time(), "subject": subject, "message": message})
    state["deferred_notifies"] = pending[-_MAX_DEFERRED:]
    _save_state(state)
    log.info("research: notify-worthy but held back (paused or within the min gap) — %r kept for later", subject)


def pop_deferred_mentions() -> str:
    """Deferred findings as a system-prompt block for the next conversation
    turn — delivered exactly once ('' when none). Consumed by chat() alongside
    background.pop_completed_notices()."""
    state = _load_state()
    pending = state.get("deferred_notifies", [])
    if not pending:
        return ""
    state["deferred_notifies"] = []
    _save_state(state)
    lines = []
    for p in pending:
        if not _sourced_notification(str(p.get("message", ""))):
            log.warning("research: discarded legacy deferred finding without a source")
            continue
        try:
            hhmm = datetime.datetime.fromtimestamp(float(p.get("ts", 0))).strftime("%H:%M")
        except Exception:
            hhmm = "?"
        lines.append(f"- ({hhmm}) {p.get('subject', '?')}: {p.get('message', '')}")
    if not lines:
        return ""
    return (
        _owner.say("[Research findings you wanted to message «owner» about earlier, but held back "
        "to avoid spamming «his» phone — bring them up naturally now if there's an opening, "
        "or mention them briefly]\n") + "\n".join(lines)
    )


async def _flush_deferred(now: float) -> None:
    """DM parked findings once the notify gap has expired. Called every loop
    cycle (cheap early-outs) so delivery doesn't wait for the next full tick.
    Note: if a conversation turn pops the same findings during the send await,
    the rare cost is a double mention, never a lost one."""
    if _notifier is None:
        return
    state = _load_state()
    pending = state.get("deferred_notifies", [])
    if not pending:
        return
    if now - float(state.get("last_notify", 0.0)) < _MIN_NOTIFY_GAP_S:
        return
    text = "\n\n".join(str(p.get("message", "")).strip() for p in pending
                         if _sourced_notification(str(p.get("message", ""))))
    if not text:
        state["deferred_notifies"] = []
        _save_state(state)
        return
    if len(pending) > 1:
        text = "A few things from my research passes I held back earlier:\n\n" + text
    try:
        sent = await _notifier(text)
    except Exception as e:
        log.debug("research: deferred flush failed: %s", e)
        return
    if sent:
        state = _load_state()
        state["deferred_notifies"] = []
        state["last_notify"] = now
        _save_state(state)
        log.info("research: delivered %d deferred notification(s)", len(pending))


# ---------------------------------------------------------------------------
# Scoring — pure functions (unit-tested in test_research.py)
# ---------------------------------------------------------------------------

_FRESH_DECAY_DAYS      = 3.0    # how fast conversational heat cools
_STALE_SATURATION_DAYS = 14.0   # how long until an unresearched topic maxes its pull
_FRESHNESS_WEIGHT      = 0.65
_STALENESS_WEIGHT      = 0.35


def freshness(similarity: float, significance: float, age_days: float) -> float:
    """How 'live' a topic is in recent conversation, in [0, 1].

    similarity/significance come from the best exchange-index hit for the
    topic; age is that exchange's age. Same shape as threads.rank_exchange
    but with a much faster decay — this is about what's hot NOW, not general
    long-term relevance.
    """
    similarity   = max(0.0, min(1.0, similarity))
    significance = max(0.0, min(1.0, significance))
    age_days     = max(0.0, age_days)
    return similarity * (0.5 + 0.5 * significance) * math.exp(-age_days / _FRESH_DECAY_DAYS)


def staleness(days_since_researched: Optional[float]) -> float:
    """How overdue a topic is for a research pass, in [0, 1].

    None = never researched = maximum pull (1.0). Otherwise asymptotic toward
    1.0 with time, saturating over ~_STALE_SATURATION_DAYS.
    """
    if days_since_researched is None:
        return 1.0
    days = max(0.0, days_since_researched)
    return 1.0 - math.exp(-days / _STALE_SATURATION_DAYS)


def score_topic(freshness_val: float, staleness_val: float) -> float:
    """Fused selection score. Freshness-dominant by design."""
    return _FRESHNESS_WEIGHT * freshness_val + _STALENESS_WEIGHT * staleness_val


# ---------------------------------------------------------------------------
# Dependency injection (wired by main.py at startup)
# ---------------------------------------------------------------------------

_busy_check: Optional[Callable[[], bool]] = None
_notifier:   Optional[Callable[[str], Awaitable[bool]]] = None


def set_busy_check(fn: Callable[[], bool]) -> None:
    """Injected: returns True while a conversation turn holds the pipeline lock."""
    global _busy_check
    _busy_check = fn


def set_notifier(fn: Callable[[str], Awaitable[bool]]) -> None:
    """Injected: async fn(text) -> bool that DMs the owner research findings."""
    global _notifier
    _notifier = fn


# ---------------------------------------------------------------------------
# Candidate gathering
# ---------------------------------------------------------------------------

_TOPIC_HEAD_CHARS   = 300   # how much of a note body seeds its freshness query
_MAX_SHORTLIST      = 3     # scored topics shown to the picker
_RECENT_EXCHANGES   = 8     # recent exchanges shown for emerging-subject discovery

# Below this score with nothing fresh in conversation either, live testing
# showed the picker call itself starts failing more often -- handed a
# shortlist where everything scores near zero and no recent exchanges to
# propose something new from, the model apparently returns prose/hedging
# instead of the requested JSON more often than not. Treating that
# combination as a legitimate "nothing to do" -- skipping before ever calling
# the picker -- avoids both the wasted call and the failure. Set well below
# the pure-staleness backstop (0.35·1.0 for a never-researched topic with
# zero freshness) so that backstop still fires on its own.
_MIN_SCORE_TO_ATTEMPT = 0.10


def worth_attempting(best_score: float, has_recent: bool) -> bool:
    """Pure gate: is this tick worth even calling the picker LLM for?"""
    return best_score >= _MIN_SCORE_TO_ATTEMPT or has_recent


# ---------------------------------------------------------------------------
# Research angles — the 2.0 core
# ---------------------------------------------------------------------------

# Each angle: a description shown to the picker, and query templates used by
# the deterministic fallback (the picker writes its own angle-shaped queries;
# templates exist so even a failed picker call isn't one-dimensional).
_ANGLES: dict[str, dict] = {
    "news": {
        "desc": "current events — new publications, discoveries, acquisitions, "
                "anything that actually happened recently around this subject",
        "templates": ["{title} recent developments", "{title} latest news"],
    },
    "deepen": {
        "desc": "study an underdeveloped aspect of the note in real depth — "
                "context that makes the next conversation about it richer",
        "templates": ["{title} key ideas explained analysis", "{title} historical context"],
    },
    "perspectives": {
        "desc": "faithful interpretations of the idea, a serious alternative, and "
                "the assumptions or evidence that distinguish them",
        "templates": ["{title} primary source central argument interpretation",
                      "{title} scholarly interpretations alternatives"],
    },
    "connections": {
        "desc": "bridges to adjacent thinkers and subjects — influences, "
                "contrasts, who continued or answered this",
        "templates": ["{title} influence on other thinkers", "{title} compared to contemporaries"],
    },
    "questions": {
        "desc": "answer one concrete unresolved question the note itself raises",
        "templates": ["{title} central question evidence explanation",
                      "{title} primary source explanation"],
    },
}
_ANGLE_ORDER = ["news", "deepen", "perspectives", "connections", "questions"]

# A topic researched more recently than this is excluded from candidates
# entirely (shortlist AND fallback). Freshness dominates the fused score for
# ~3 days after a conversation, and researching a topic only zeroes the 0.35
# staleness component -- so without a cooldown a conversationally-hot topic
# wins every single tick (live failure: ~20 albert-camus passes in one
# night at pace=high, burning API calls and lossy-rewriting the note each
# time). NOTE: last_researched stores a DATE, which parses to a MIDNIGHT
# timestamp -- 24h against that anchor means exactly "at most one research
# pass per topic per calendar day" (a shorter value would let a same-day
# topic re-qualify by afternoon).
_TOPIC_COOLDOWN_HOURS = 24.0


# A failed pass used to leave nothing behind for some failures ("note has no traceable
# source", "unsupported personal attribution", "all searches failed" return without
# touching last_researched), so the note stayed the most overdue topic and was picked
# again next tick. Live (2026-09-28): topics/content-as-dosing.md failed five passes in a
# row. Consecutive failures per note now hold it out of the candidates: 1 day, then 2,
# then 4, then a week at a time until a pass completes. A deliberate queued request is
# never held back.
_FAIL_HOLD_S = (86400.0, 2 * 86400.0, 4 * 86400.0, 7 * 86400.0)
_FAILURES_KEY = "research_failures"
# H1: a synthesis still cut off after its continuation. A skip, not a failure: the limit
# is ours, not the note's.
_CUT_OFF_OUTCOME = "skipped: synthesis cut off at the output limit"


def failure_hold_s(fails: int) -> float:
    """How long a note stays out of the candidates after `fails` consecutive failures."""
    if fails <= 0:
        return 0.0
    return _FAIL_HOLD_S[min(fails, len(_FAIL_HOLD_S)) - 1]


def in_failure_hold(record: Optional[dict], now: float) -> bool:
    """Pure: is a note with this failure record still held back?"""
    if not record:
        return False
    return now - float(record.get("last", 0.0)) < failure_hold_s(int(record.get("fails", 0)))


def _note_pass_outcome(filename: Optional[str], outcome: str, now: float) -> None:
    """Count a failed pass against the note, or clear the count after a completed one.
    Skips (a concurrent write, a closed-topic veto) count as neither."""
    if not filename:
        return
    failed = outcome.startswith("failed")
    completed = outcome.startswith("wrote") or outcome == "no new understanding"
    if not (failed or completed):
        return

    def mutate(state: dict) -> None:
        records = state.setdefault(_FAILURES_KEY, {})
        if completed:
            records.pop(filename, None)
            return
        rec = records.get(filename) or {"fails": 0}
        rec = {"fails": int(rec.get("fails", 0)) + 1, "last": now, "last_outcome": outcome[:120]}
        records[filename] = rec
        log.warning("research: %s failed %d pass(es) in a row (%s) -- held out for %.0fh",
                    filename, rec["fails"], outcome[:80], failure_hold_s(rec["fails"]) / 3600)
    try:
        _update_state(mutate)
    except Exception as e:
        log.debug("research: failure bookkeeping skipped: %s", e)


def topic_in_cooldown(days_since_researched: Optional[float]) -> bool:
    """Pure gate: was this topic researched too recently to pick again?"""
    if days_since_researched is None:
        return False
    return days_since_researched < _TOPIC_COOLDOWN_HOURS / 24.0


def next_angle(last_angle: Optional[str]) -> str:
    """Deterministic angle rotation for the fallback path.

    A note with no last_angle at all but which HAS been researched before
    dates from the 1.0 era when every pass was effectively "news" — rotate
    to "deepen" rather than repeating news yet again. (Brand-new notes never
    reach this: their first pass goes through the picker, and the fallback
    only handles existing candidates.)
    """
    if last_angle not in _ANGLES:
        return "deepen"
    idx = _ANGLE_ORDER.index(last_angle)
    return _ANGLE_ORDER[(idx + 1) % len(_ANGLE_ORDER)]


# ---------------------------------------------------------------------------
# Subject families (2026-09-06) — the cooldown above is keyed per FILE, but
# one subject is often several files: four Frankl notes, seven Dostoevsky
# notes, four NVIDIA notes. Each pass puts one file into cooldown while its
# siblings — scored from the same conversational freshness, since they're
# about the same thing — remain fully eligible, so the picker's own shortlist
# fills up with siblings of the subject it just researched. Live failure
# (2026-09-04/05): four Frankl passes and a repeated "perspectives" angle in
# one night, because the loop always saw "three Frankl notes, no engagement
# with critics" — it could see every sibling's staleness but not that a
# sibling had *just* covered that exact angle. Grouping files into families
# and applying cooldown/staleness/angle-rotation at the FAMILY level (not the
# file level) closes this the same way _TOPIC_COOLDOWN_HOURS closed the
# single-file version of the same failure mode.
#
# Two files join a family if EITHER:
#   (a) their title+head embedding cosine similarity is >= _FAMILY_EMBED_SIM_MIN
#       (catches near-duplicate notes from repeated research passes), or
#   (b) they share a non-generic token (see _FAMILY_TOKEN_STOPWORDS) of
#       length >= _FAMILY_TOKEN_MIN_LEN from their title/filename (catches a
#       hand-written note like "frankl-my-position.md" that never scored high
#       enough on (a) alone — measured live at 0.856 cosine against its true
#       siblings, below the 0.90 threshold, yet the "frankl" token makes the
#       family membership unambiguous).
#
# The stopword list below was built by measuring real token->file counts
# across every existing topics/ note (not guessed): "stock" appears in both
# the NVIDIA family (4 files) and the Tesla note, and "research"/"philosophy"/
# "analysis"/"connections"/"suffering" each span multiple unrelated families —
# exactly the generic research vocabulary that would wrongly merge different
# subjects if trusted as a family key. Proper nouns (frankl, dostoevsky,
# nvidia, camus, schopenhauer) are what should link files, not what kind of
# note it is.
_FAMILY_EMBED_SIM_MIN      = 0.90
_FAMILY_TOKEN_MIN_LEN      = 5
_FAMILY_WEEKLY_CAP         = 2      # max passes across one family per rolling week
_FAMILY_WEEKLY_WINDOW_DAYS = 7.0

_FAMILY_TOKEN_STOPWORDS = frozenset({
    "stock", "stocks", "research", "philosophy", "philosophical", "connection",
    "connections", "suffering", "analysis", "analyses", "correction", "decline",
    "timeline", "chronology", "perspective", "perspectives", "question", "questions",
    "current", "technical", "setup", "institutional", "position", "positions",
    "snapshot", "valuation", "fundamentals", "tiering", "taste", "trigger",
    "triggers", "stress", "comfort", "content", "dosing", "emotional", "check",
    "news", "deepen", "method", "biographical", "embedded", "tragic", "optimism",
    "pivot", "recent", "developments", "latest", "update", "updates", "report",
    "reports",
})


def _topic_tokens(title: str, filename: str) -> set[str]:
    """Lowercase alnum tokens (len >= _FAMILY_TOKEN_MIN_LEN) from a note's
    title + filename, minus generic research vocabulary — the candidate
    family-linking keys. Biased toward proper nouns/subject names."""
    text = f"{title} {Path(filename).stem}".lower()
    tokens = re.findall(r"[a-z]+", text)
    return {t for t in tokens if len(t) >= _FAMILY_TOKEN_MIN_LEN and t not in _FAMILY_TOKEN_STOPWORDS}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def build_families(
    entries: list[dict], vectors: Optional[dict[str, list[float]]] = None
) -> list[list[str]]:
    """Group topic files into subject families via union-find.

    `entries` is [{"file", "title"}, ...]. `vectors` optionally maps
    file -> embedding; when omitted, grouping falls back to the token
    signal alone. Pure/sync so it's unit-testable with synthetic entries and
    fake vectors, no real embedder required.
    """
    files = [e["file"] for e in entries]
    parent = {f: f for f in files}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    tokens = {e["file"]: _topic_tokens(e["title"], e["file"]) for e in entries}
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            fi, fj = files[i], files[j]
            linked = bool(tokens[fi] & tokens[fj])
            if not linked and vectors and fi in vectors and fj in vectors:
                linked = _cosine(vectors[fi], vectors[fj]) >= _FAMILY_EMBED_SIM_MIN
            if linked:
                union(fi, fj)

    groups: dict[str, list[str]] = {}
    for f in files:
        groups.setdefault(find(f), []).append(f)
    return list(groups.values())


def family_in_cooldown(open_members: list[dict]) -> bool:
    """True if ANY open member was researched within _TOPIC_COOLDOWN_HOURS —
    the family-level analogue of topic_in_cooldown. Each member dict needs
    only a 'days_since' key (Optional[float])."""
    return any(topic_in_cooldown(m.get("days_since")) for m in open_members)


def family_weekly_count(open_members: list[dict]) -> int:
    """How many open members were researched within the rolling week — catches
    the case a 24h cooldown alone can't: two DIFFERENT siblings each touched a
    couple of days apart, neither individually in cooldown, but the subject as
    a whole researched twice already this week."""
    return sum(
        1 for m in open_members
        if m.get("days_since") is not None and m["days_since"] < _FAMILY_WEEKLY_WINDOW_DAYS
    )


def family_in_weekly_cap(open_members: list[dict]) -> bool:
    return family_weekly_count(open_members) >= _FAMILY_WEEKLY_CAP


def _aggregate_family(open_members: list[dict]) -> dict:
    """Fuse one subject family's open members into a single candidate dict.

    - existing_file/staleness come from the MOST OVERDUE member (the one that
      will actually receive this pass's write) — spreads research across
      siblings over time instead of one file winning every pick.
    - freshness is the family's max (the subject is "hot" if any member's
      conversational signal is hot).
    - last_angle is whichever member was researched most recently, so angle
      rotation moves off the family's actual last pass, not one arbitrary
      member's — this is what stops "perspectives" from repeating across
      siblings that all individually show a different last_angle.
    Each member dict needs: file, title, freshness, staleness inputs
    (days_since), last_angle, digest.
    """
    most_overdue = max(
        open_members,
        key=lambda m: (m["days_since"] is None, m["days_since"] or 0.0),
    )
    researched = [m for m in open_members if m["days_since"] is not None]
    most_recent = min(researched, key=lambda m: m["days_since"]) if researched else None
    max_fresh = max(m["freshness"] for m in open_members)
    stale = staleness(most_overdue["days_since"])
    return {
        "file":         most_overdue["file"],
        "title":        most_overdue["title"],
        "freshness":    max_fresh,
        "staleness":    stale,
        "score":        score_topic(max_fresh, stale),
        "last_angle":   most_recent["last_angle"] if most_recent else None,
        "digest":       most_overdue["digest"],
        "open_question": most_overdue.get("open_question", ""),
        "family_files": sorted(m["file"] for m in open_members),
    }


def fallback_pick(candidates: list[dict]) -> Optional[dict]:
    """Deterministic stand-in for a failed/unparseable picker response.

    Live logs showed picker calls failing with no underlying API error --
    the call succeeded, the content just wasn't the requested JSON shape.
    Rather than waste the whole tick, fall back to the top-scored candidate
    (already computed by pure math in _gather_candidates), rotating to the
    next angle after the note's last one so even fallback passes aren't
    one-dimensional "recent developments" checks every time. Returns None
    only when there are no candidates to fall back to at all.
    """
    if not candidates:
        return None
    top = candidates[0]
    title = top["title"]
    angle = next_angle(top.get("last_angle"))
    return {
        "subject": title,
        "existing_file": top["file"],
        "angle": angle,
        "why": "fallback rotation",
        "question": (top.get("open_question") if angle == "questions" else None)
                    or _default_research_question(title, angle),
        "queries": ([top["open_question"],
                     _ANGLES[angle]["templates"][0].format(title=title)]
                    if angle == "questions" and top.get("open_question") else
                    [t.format(title=title) for t in _ANGLES[angle]["templates"]]),
    }


def _default_research_question(subject: str, angle: str) -> str:
    """Specific enough to steer fallback and queued passes without picker prose."""
    stems = {
        "news": "What actually changed recently, and why does it matter?",
        "deepen": "What mechanism or context makes the central idea easier to understand?",
        "perspectives": "Where do informed interpretations differ, and what do they share?",
        "connections": "Which connection clarifies the subject rather than merely resembling it?",
        "questions": "What is the most important unresolved question, and what can evidence answer?",
    }
    return f"For {subject}: {stems[angle]}"


# ---------------------------------------------------------------------------
# Research queue — deferred curiosity JARVIS can add to and follow
# ---------------------------------------------------------------------------
#
# jarvis-vault/research-queue.md (vault root, next to queue.md — visible in
# Obsidian, deliberately NOT semantically indexed since backfill only walks
# specific folders). Two line shapes, pipe-separated:
#   [queued] 2026-07-05 | subject | angle-or-auto | reason
#   [done] 2026-07-05 14:02 | subject [angle] (queued|auto) | outcome
# Queued entries are picked up BEFORE anything else on the next tick (they
# bypass the picker, the scores, and the cooldown — a deliberate request
# beats all heuristics). Every pass, queued or autonomous, appends a [done]
# line, so the file doubles as the research trail the owner asked to follow.

_QUEUE_FILE     = _PROJECT_ROOT / "jarvis-vault" / "research-queue.md"
_MAX_QUEUED     = 10   # pending requests cap — keeps the queue an intention list, not a dumping ground
_STATUS_RECENT_LINES = 8   # how many [done] lines the 'status' action shows — display only,
                           # never affects what's stored (see _log_research_pass below)

_QUEUE_HEADER = [
    "# Research Queue",
    "",
    "Subjects queued for the autonomous research loop, plus the log of completed",
    "passes. Queued entries are picked up before anything else on the next tick.",
    "",
]


def _read_queue_file(*, strict: bool = False) -> list[str]:
    try:
        return _QUEUE_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception as exc:
        if strict:
            log.warning("research: queue could not be read (%s)", exc)
            raise
        return []


def _write_queue_file(lines: list[str]) -> None:
    try:
        _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _QUEUE_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except Exception as e:
        log.debug("research: queue write failed: %s", e)


def _format_queued_line(subject: str, angle: str, reason: str, date: str) -> str:
    return f"[queued] {date} | {subject} | {angle or 'auto'} | {reason}"


def _parse_queued_line(line: str) -> Optional[dict]:
    stripped = line.strip()
    if not stripped.startswith("[queued]"):
        return None
    parts = [p.strip() for p in stripped[len("[queued]"):].split("|")]
    if len(parts) < 2 or not parts[1]:
        return None
    angle = parts[2].lower() if len(parts) > 2 else ""
    return {
        "date":    parts[0],
        "subject": parts[1],
        "angle":   angle if angle in _ANGLES else None,   # 'auto'/unknown -> loop chooses
        "reason":  parts[3] if len(parts) > 3 else "",
    }


def _pop_queued() -> Optional[dict]:
    """Remove and return the oldest [queued] entry, or None. Popped immediately
    (not after the pass) so a failing request can't retry forever — its outcome
    is recorded as a [done]/[failed] line instead."""
    lines = _read_queue_file()
    for i, line in enumerate(lines):
        item = _parse_queued_line(line)
        if item:
            del lines[i]
            _write_queue_file(lines)
            return item
    return None


def snapshot(*, strict: bool = False) -> dict:
    """P2: the research loop as /activity shows it. Read-only; the parsing stays here, where
    the file formats are owned.

    `pace`, `next_at` (epoch of the next scheduled pass; None when research is off), the
    `queued` subjects the owner asked for, `passes` (the [done] trail, oldest first, each with
    `at` as an epoch) and `deferred`, the findings waiting for the notify gap.
    """
    state = pacing.load_state(_PACE_FILE, strict=True) if strict else _load_state()
    pace = state.get("pace", _DEFAULT_PACE) if strict else get_pace()
    interval = _PACE_INTERVALS_S.get(pace)
    last = float(state.get("last_tick", 0.0) or 0.0)
    lines = _read_queue_file(strict=True) if strict else _read_queue_file()
    passes = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("[done]"):
            continue
        head, _, rest = stripped[len("[done]"):].partition("|")
        try:
            stamp = datetime.datetime.strptime(head.strip(), "%Y-%m-%d %H:%M")
            if strict:
                from zoneinfo import ZoneInfo
                stamp = stamp.replace(tzinfo=_localtime.ZONE)
            at = stamp.timestamp()
        except ValueError as exc:
            if strict:
                log.warning("research: invalid completed-pass date (%s)", exc)
                raise
            continue
        subject, _, outcome = rest.partition("|")
        if strict and (not subject.strip() or not outcome.strip()):
            log.warning("research: incomplete completed-pass line")
            raise ValueError("Incomplete research pass")
        passes.append({"at": at, "subject": subject.strip(), "outcome": outcome.strip()})
    return {
        "pace": pace,
        "next_at": (last + interval) if interval and last else None,
        "queued": [q for q in map(_parse_queued_line, lines) if q],
        "passes": passes,
        "deferred": len(state.get("deferred_notifies", []) or []),
    }


def _is_write_refused(result) -> bool:
    """Was a vault write declined rather than performed?

    Routed through tools/outcomes.py rather than matching the message text here, so
    this cannot drift from what the rest of the system calls a failed vault write
    (the drift A04 slice 3 had to fix between the two tool executors).
    """
    from backend.tools import outcomes
    return outcomes.is_failure("vault_write", result)


def _log_research_pass(subject: str, angle: str, outcome: str, queued: bool = False,
                       file: Optional[str] = None) -> None:
    """Append a [done] trail line. Genuinely append-only — never trimmed.

    This used to cap storage at 60 lines and delete the oldest past that,
    which quietly threw away real research history (the file was already
    sitting at exactly 60 when this was noticed — every new pass was
    already discarding an old one). The [done] log is never fed into any
    LLM prompt (the picker/synthesis prompts only ever see the scored
    shortlist and search findings, never this file), so there's no context-
    budget reason to bound it — only the 'status' action's *display* is
    bounded (_STATUS_RECENT_LINES, applied at read time, not storage time).
    Same 'no deletion by design, files accumulate' philosophy as the rest
    of the vault.
    """
    try:
        lines = _read_queue_file() or list(_QUEUE_HEADER)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        origin = "queued" if queued else "auto"
        lines.append(f"[done] {stamp} | {subject} [{angle}] ({origin}) | {outcome}")
        _write_queue_file(lines)
    except Exception as e:
        log.debug("research: pass log failed: %s", e)
    _note_pass_outcome(file, outcome, time.time())


def queued_to_pick(item: dict) -> dict:
    """Turn a queued request into the same pick shape the picker/fallback
    produce, so the rest of _tick treats all three origins identically."""
    subject = item["subject"]
    # Match an existing note by slug first, then by title.
    existing = None
    fname = f"{_slugify(subject)}.md"
    try:
        if (_TOPICS_DIR / fname).exists():
            existing = fname
        else:
            for p in _TOPICS_DIR.glob("*.md"):
                content = p.read_text(encoding="utf-8", errors="replace")
                if _topic_title(p.name, _strip_frontmatter(content)).lower() == subject.lower():
                    existing = p.name
                    break
    except Exception:
        pass
    angle = item.get("angle")
    if angle not in _ANGLES:
        last = None
        if existing:
            try:
                last = _parse_last_angle((_TOPICS_DIR / existing).read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
        angle = next_angle(last)
    reason = str(item.get("reason", "")).strip()
    queries = [t.format(title=subject) for t in _ANGLES[angle]["templates"]]
    if reason:
        # The stated reason is the sharpest query we have — lead with it.
        queries = [f"{subject} {reason}"[:120]] + queries[:1]
    return {
        "subject":       subject,
        "existing_file": existing,
        "angle":         angle,
        "why":           reason or "queued request",
        "question":      reason if "?" in reason else _default_research_question(subject, angle),
        "queries":       queries[:2],
    }


def _find_family_files(subject: str) -> list[str]:
    """All topics/*.md files belonging to `subject`'s family, by exact
    slug/title match or a shared family token (see _topic_tokens) —
    deliberately TOKEN-ONLY, no embedding fuzz. This backs the 'close'
    action below: closing the wrong family silently hides it from all future
    research, so precision matters more here than in build_families' more
    permissive cooldown-grouping use."""
    try:
        files = sorted(_TOPICS_DIR.glob("*.md")) if _TOPICS_DIR.exists() else []
    except Exception:
        return []
    subj_slug = _slugify(subject)
    subj_tokens = _topic_tokens(subject, subject)
    matches: list[str] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        title = _topic_title(path.name, _strip_frontmatter(content))
        if path.stem == subj_slug or title.strip().lower() == subject.strip().lower():
            matches.append(path.name)
            continue
        if subj_tokens and (subj_tokens & _topic_tokens(title, path.name)):
            matches.append(path.name)
    return matches


# A shared word only vetoes when the meaning agrees too. Measured 2026-09-25 on the 34 real
# (subject -> note written) pairs in research-queue.md: a subject and its own note score
# >= 0.871 cosine (median 0.932), while a subject against any note outside its family tops
# out at 0.850 (p99 0.832, n=1028). Live failure this fixes: a closed "Meta's Muse Model"
# note whose only linking token was "model" ("muse"/"meta" are under the 5-letter token
# minimum) vetoed "DeepSeek V4.1 Flash model consolidation" (0.806) seven times in a day.
_CLOSED_TOKEN_SIM_MIN = 0.86
# Meaning alone, at family strength, catches a closed subject reworded with no shared
# token ("Meta's Muse Spark benchmarks" vs the Muse note: 0.921, zero shared tokens).
_CLOSED_SIM_MIN = _FAMILY_EMBED_SIM_MIN


def _closed_notes() -> list[tuple[str, str, str]]:
    """(filename, title, title + head text) for every research_closed topic note."""
    try:
        files = sorted(_TOPICS_DIR.glob("*.md")) if _TOPICS_DIR.exists() else []
    except Exception:
        return []
    out = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if _parse_research_closed(content):
            body = _strip_frontmatter(content)
            title = _topic_title(path.name, body)
            out.append((path.name, title, f"{title}\n{body[:_TOPIC_HEAD_CHARS]}"))
    return out


def _closed_family_conflict(subject: str, filename: str,
                            similarity: Optional[dict[str, float]] = None) -> Optional[str]:
    """Would writing `filename` for `subject` resurrect a topic the owner
    already closed to research? Returns the closed file's name if so, else
    None. Absolute veto for the autonomous picker/fallback path — a closed
    topic can never be re-picked no matter how relevant it scores; queued
    requests are deliberately exempt (see the from_queue check in _tick),
    since an explicit ask is not the kind of relevance argument this exists
    to stop.

    `similarity` maps closed filename -> cosine(subject, note). With it, a shared token
    must be confirmed by meaning (>= _CLOSED_TOKEN_SIM_MIN) and meaning alone vetoes at
    _CLOSED_SIM_MIN. Without it (embedder unavailable) the rule is token-only, as before:
    a closed topic stays closed even when the check can't be made precise."""
    subj_tokens = _topic_tokens(subject, filename)
    for name, title, _text in _closed_notes():
        if name == filename:
            return name
        sim = similarity.get(name) if similarity else None
        if subj_tokens & _topic_tokens(title, name):
            if sim is None or sim >= _CLOSED_TOKEN_SIM_MIN:
                return name
        elif sim is not None and sim >= _CLOSED_SIM_MIN:
            return name
    return None


async def _closed_similarity(subject: str) -> Optional[dict[str, float]]:
    """cosine(subject, each closed note) in one batched embed; None if unavailable."""
    closed = _closed_notes()
    if not closed:
        return {}
    try:
        from backend.brain import embeddings
        vecs = await embeddings.embed_many([subject] + [text for _n, _t, text in closed])
        if not vecs or len(vecs) != len(closed) + 1:
            return None
        return {name: _cosine(vecs[0], v) for (name, _t, _x), v in zip(closed, vecs[1:])}
    except Exception as e:
        log.debug("research: closed-topic similarity unavailable (%s) -- token-only veto", e)
        return None


async def research_queue(action: str, subject: str = "", angle: str = "", reason: str = "") -> str:
    """Registered tool: 'add' queues a subject for the loop; 'status' shows
    pending requests + the recent research trail; 'close' permanently vetoes
    a subject (and every note in its family) from ever being auto-picked
    again — no relevance/staleness score can override it; 'reopen' undoes
    that. Never raises."""
    try:
        action = (action or "").strip().lower()
        if action == "status":
            lines = _read_queue_file()
            queued = [l.strip() for l in lines if l.strip().startswith("[queued]")]
            done   = [l.strip() for l in lines if l.strip().startswith("[done]")][-_STATUS_RECENT_LINES:]
            pace   = get_pace()
            out = [f"Research pace: {pace}."]
            out.append("Pending requests:" if queued else "No pending research requests.")
            out.extend(f"  {l}" for l in queued)
            if done:
                out.append("Recent passes:")
                out.extend(f"  {l}" for l in done)
            try:
                closed_files = sorted(
                    p.name for p in _TOPICS_DIR.glob("*.md")
                    if _parse_research_closed(p.read_text(encoding="utf-8", errors="replace"))
                )
            except Exception:
                closed_files = []
            if closed_files:
                out.append(f"Closed to research: {', '.join(closed_files)}")
            return "\n".join(out)

        if action in ("close", "reopen"):
            subject = (subject or "").strip()
            if len(subject.strip(".")) < 3:
                return "Error: a real subject is required."
            files = _find_family_files(subject)
            if not files:
                return f"No topic note found matching '{subject}' — nothing to {action}."
            # Written directly (not via vault_write) -- this only flips a
            # frontmatter flag, the note's actual content is untouched, so
            # there's nothing new for the semantic index to learn; it also
            # avoids re-indexing on every close/reopen for no benefit.
            from backend.tools.vault import _update_frontmatter
            flag = "true" if action == "close" else "false"
            updated_files = []
            for fname in files:
                path = _TOPICS_DIR / fname
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                new_content = _update_frontmatter(content, {"research_closed": flag})
                try:
                    path.write_text(new_content, encoding="utf-8")
                except Exception as e:
                    log.warning("research: failed to %s %s: %s", action, fname, e)
                    continue
                updated_files.append(fname)
            verb = "Closed" if action == "close" else "Reopened"
            return f"{verb} {len(updated_files)} note(s) {'to' if action == 'close' else 'for'} autonomous research: {', '.join(updated_files)}."

        if action != "add":
            return f"Error: unknown action '{action}'. Use 'add', 'status', 'close', or 'reopen'."

        subject = (subject or "").strip()
        if len(subject.strip(".")) < 3:
            return "Error: a real subject is required."
        angle = (angle or "").strip().lower()
        if angle and angle not in _ANGLES:
            return (f"Error: unknown angle '{angle}'. "
                    f"Options: {', '.join(_ANGLE_ORDER)} — or omit it to rotate automatically.")
        lines = _read_queue_file() or list(_QUEUE_HEADER)
        existing_queued = [_parse_queued_line(l) for l in lines]
        existing_queued = [q for q in existing_queued if q]
        if any(q["subject"].lower() == subject.lower() for q in existing_queued):
            return f"'{subject}' is already in the research queue."
        if len(existing_queued) >= _MAX_QUEUED:
            return f"Error: the research queue already has {_MAX_QUEUED} pending items — let some complete first."
        today = datetime.date.today().isoformat()
        lines.append(_format_queued_line(subject, angle, (reason or "").strip(), today))
        _write_queue_file(lines)
        interval = _PACE_INTERVALS_S[get_pace()]
        when = ("research is currently switched off (pace: off) — it will run when the pace is turned back on"
                if interval is None else f"it will run within ~{_human_interval(interval)}")
        return (f"Queued '{subject}'" + (f" [{angle}]" if angle else "")
                + f" for autonomous research — {when}." + pause.tool_note())
    except Exception as e:
        log.warning("research: research_queue tool failed: %s", e)
        return f"Error: research queue unavailable ({e})"


# Subjects/queries that mean the model echoed the prompt template back
# instead of actually picking -- both shapes happened live (the loop
# dutifully researched the ellipsis punctuation mark and "search queries"
# in general, creating garbage topics/ notes).
_PLACEHOLDER_SUBJECTS = frozenset({"...", "short subject name", "subject", "topic", "the subject"})
_PLACEHOLDER_QUERY_FRAGMENTS = ("focused web search quer", "search queries shaped by")


def _valid_pick(pick: Optional[dict]) -> bool:
    """Reject picker output that is missing fields or echoes the template."""
    if not (pick and pick.get("subject") and pick.get("queries") and pick.get("question")):
        return False
    subject = str(pick["subject"]).strip()
    if len(subject.strip(".").strip()) < 3:
        return False
    if subject.lower() in _PLACEHOLDER_SUBJECTS:
        return False
    question = str(pick["question"]).strip()
    if len(question) < 12 or question.lower() in {"the specific question to answer", "question"}:
        return False
    for q in pick.get("queries", []):
        q_low = str(q).strip().lower()
        if not q_low or q_low in {"...", "1", "2"}:
            return False
        if any(frag in q_low for frag in _PLACEHOLDER_QUERY_FRAGMENTS):
            return False
    return True


def _parse_last_researched(content: str) -> Optional[float]:
    """days-ago timestamp from a `last_researched: YYYY-MM-DD` frontmatter field, else None."""
    try:
        from backend.brain.semindex import _frontmatter_fields
        fields = _frontmatter_fields(content, ("last_researched",))
        raw = fields.get("last_researched", "").strip()
        if not raw:
            return None
        dt = datetime.datetime.strptime(raw, "%Y-%m-%d")
        return dt.timestamp()
    except Exception:
        return None


def _parse_last_angle(content: str) -> Optional[str]:
    """`last_angle: <name>` frontmatter field, else None. Unknown names -> None."""
    try:
        from backend.brain.semindex import _frontmatter_fields
        fields = _frontmatter_fields(content, ("last_angle",))
        raw = fields.get("last_angle", "").strip().lower()
        return raw if raw in _ANGLES else None
    except Exception:
        return None


def _parse_research_closed(content: str) -> bool:
    """`research_closed: true` frontmatter field — a hard veto set via the
    research_queue tool's 'close' action. A closed note (and its whole
    subject family) is excluded from candidates entirely and can never be
    auto-picked again, regardless of freshness/staleness — see 'close'
    action below and _closed_family_conflict."""
    try:
        from backend.brain.semindex import _frontmatter_fields
        raw = _frontmatter_fields(content, ("research_closed",)).get("research_closed", "").strip().lower()
        return raw in ("true", "yes", "1")
    except Exception:
        return False


def _strip_frontmatter(content: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content
    try:
        close = next(i for i, l in enumerate(lines[1:], start=1) if l.strip() == "---")
        return "\n".join(lines[close + 1:])
    except StopIteration:
        return content


def _topic_title(filename: str, body: str) -> str:
    """Best-guess title for a topic note.

    Only trusts a genuine H1 ("# Title") as the title. A first heading at H2+
    ("## Context", "## Core Findings", "## Structure") is a section label,
    not the note's subject -- and grabbing it anyway is exactly what produced
    a live cascade failure: a weather note's "## Context" heading became the
    "topic" ("Context"), which the picker/fallback then dutifully researched
    ("Context recent developments") -- nonsense queries that failed at
    synthesis too. Checking every topics/ note afterward showed half of them
    open with a generic H2 rather than an H1, so this wasn't a one-off.
    Filename is a far more reliable fallback since topics/ filenames are
    already kebab-case and descriptive by convention.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith("##"):
            return stripped.lstrip("#").strip()
        break   # first real content isn't an H1 -- don't hunt deeper for one
    return Path(filename).stem.replace("-", " ")


def _first_open_question(body: str) -> str:
    """Surface the note's actual unresolved question to the picker."""
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if re.match(r"^##\s+open questions\b", stripped, re.I):
            in_section = True
            continue
        if in_section and stripped.startswith("#"):
            break
        if in_section and re.match(r"^[-*]\s+\S", stripped):
            return re.sub(r"^[-*]\s+", "", stripped)[:240]
    return ""


async def _gather_candidates(now: float) -> list[dict]:
    """Score every topics/ note not in cooldown, grouped into subject families.

    Returns [{file, title, score, freshness, staleness, last_angle, digest,
    family_files}] — one candidate PER FAMILY (see build_families above), not
    per file, so the shortlist can't fill up with siblings of the same
    subject. A family is excluded entirely (shortlist AND fallback) if any
    open member was researched within _TOPIC_COOLDOWN_HOURS, if it's already
    hit _FAMILY_WEEKLY_CAP passes this week, or if every member is
    research_closed (see the 'close' research_queue action below).
    """
    from backend.brain import semindex

    try:
        files = sorted(_TOPICS_DIR.glob("*.md")) if _TOPICS_DIR.exists() else []
    except Exception:
        files = []

    by_file: dict[str, dict] = {}
    query_texts: dict[str, str] = {}
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        body  = _strip_frontmatter(content)
        title = _topic_title(path.name, body)
        researched_ts = _parse_last_researched(content)
        days_since = (now - researched_ts) / 86400.0 if researched_ts else None
        closed = _parse_research_closed(content)
        by_file[path.name] = {
            "file":       path.name,
            "title":      title,
            "days_since": days_since,
            "last_angle": _parse_last_angle(content),
            "closed":     closed,
            "digest":     " ".join(body[:_TOPIC_HEAD_CHARS].split()),
            "open_question": _first_open_question(body),
            "freshness":  0.0,   # filled below for open files only
        }
        if not closed:
            query_texts[path.name] = f"{title}\n{body[:_TOPIC_HEAD_CHARS]}"

    # One batched embed serves both family detection (cosine similarity) and
    # the per-file freshness lookup below (semindex.search's query_vec skips
    # re-embedding) — same vector-reuse pattern as chat()'s intent embedding,
    # applied here to avoid N separate embeds for N topic notes every tick.
    vectors: dict[str, list[float]] = {}
    if query_texts:
        try:
            from backend.brain import embeddings
            names = list(query_texts.keys())
            vecs = await embeddings.embed_many([query_texts[n] for n in names])
            vectors = dict(zip(names, vecs))
        except Exception as e:
            log.debug("research: family embedding batch failed: %s", e)

    for name, query in query_texts.items():
        fresh = 0.0
        try:
            hits = await semindex.search(query, kind="exchange", k=3, query_vec=vectors.get(name))
            for h in hits:
                sim = float(h.get("score", 0.0))
                sig = float(h.get("significance", 0.0))
                age_days = max(0.0, (now - float(h.get("timestamp", now))) / 86400.0)
                fresh = max(fresh, freshness(sim, sig, age_days))
        except Exception as e:
            log.debug("research: freshness lookup failed for %s: %s", name, e)
        by_file[name]["freshness"] = fresh

    entries = [{"file": f, "title": info["title"]} for f, info in by_file.items()]
    families = build_families(entries, vectors)

    failures = _load_state().get(_FAILURES_KEY, {})
    candidates: list[dict] = []
    for member_files in families:
        open_members = [by_file[f] for f in member_files if not by_file[f]["closed"]]
        if not open_members:
            continue   # every member of this family is research_closed
        if family_in_cooldown(open_members) or family_in_weekly_cap(open_members):
            continue
        if any(in_failure_hold(failures.get(m["file"]), now) for m in open_members):
            continue   # failing repeatedly: held back, see _FAIL_HOLD_S
        candidates.append(_aggregate_family(open_members))

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


async def _recent_exchange_lines(now: float) -> str:
    """Compact recent-exchange digest for the picker prompt. '' on failure."""
    try:
        from backend.brain.threads import recent_threads
        threads = await recent_threads(_RECENT_EXCHANGES)
        lines: list[str] = []
        for t in threads:
            age_h = max(0.0, (now - float(t.get("timestamp", now))) / 3600.0)
            if t.get("high"):
                text = f"U: {t.get('user', '')[:150]} | J: {t.get('assistant', '')[:150]}"
            else:
                text = t.get("summary", "")[:300]
            if text.strip():
                lines.append(f"[{age_h:.1f}h ago] {text}")
        return "\n".join(lines)
    except Exception as e:
        log.debug("research: recent exchanges failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# LLM calls -- the provider's standard tier, reasoning off
# ---------------------------------------------------------------------------

# Picker output is a few lines of JSON, but when reasoning is on the REASONING
# narration shares the max_tokens budget with the answer -- a 1000-token cap
# starved it (the model burned the whole budget narrating "The user wants me
# to act as JARVIS..." and never reached the JSON), which is what drove the
# ~95% overnight picker-failure rate on 2026-07-04/05. 3000 leaves room for
# narration plus the JSON while staying well under _SYNTH_MAX_TOKENS.
_PICK_MAX_TOKENS = 3000

# Synthesis embeds the FULL updated note inside JSON, so the output budget
# must cover reproducing the existing note (not just the new findings) plus
# JSON escaping overhead. 2500 proved too small in practice (truncated
# mid-note on the first live ticks) so it was raised to 4000 — still not
# enough once real notes grew past ~4-6K chars (the Camus and Dostoevsky
# notes, at 6-8K chars, need ~2000-2500 tokens just to reproduce verbatim
# before any additions), which is exactly what caused the two "synthesis
# returned unusable output" ticks on 2026-07-04: the response was cut off
# before a parseable JSON object ever closed. Raised well past the largest
# real note plus headroom for growth.
_SYNTH_MAX_TOKENS = 8000


def _salvage_message_text(m) -> str:
    """content, else reasoning (or reasoning_content) sliced from its first '{'.

    DeepSeek on OpenRouter can return empty content with the real output in its
    reasoning field (checked here under both
    reasoning_content and reasoning, since which one actually streams isn't
    guaranteed the same way the echo key that must be sent back is -- see
    router.OPENROUTER's reasoning_echo_key comment). When that
    fires, the useful part is the JSON object, not the narration around it --
    return from the first '{' onward (same slice consolidation._call_compat
    already applies), so a reasoning preamble ("The user wants me to act as
    JARVIS...") never masquerades as the answer. Pure narration with no '{' at
    all salvages to '' -- there is no answer in it to recover, and returning
    it only poisons _parse_json and, worse, the continuation path (live,
    2026-09-01: a 37K-char all-reasoning synthesis response triggered a
    doomed, expensive continuation call). With thinking now explicitly
    disabled on this call (see provider().no_thinking_body) this is a
    rare degradation path, not the normal shape.
    """
    text = (m.content or "").strip()
    if text:
        return text
    reasoning = (getattr(m, "reasoning_content", None) or getattr(m, "reasoning", None) or "").strip()
    brace = reasoning.find("{")
    if brace != -1:
        return reasoning[brace:]
    return ""


async def _llm(prompt: str, max_tokens: int = _PICK_MAX_TOKENS, images: list[str] | None = None) -> str:
    """Single-prompt completion on the provider's standard tier, reasoning off.
    '' when the provider is not configured or the call fails.

    `images`: data URIs sent with the prompt (attachments.py: the note kept about an image,
    and scanned PDF pages). DeepSeek V4.1 Flash reads them (tested live 4 October 2026)."""
    content = ([{"type": "text", "text": prompt}]
               + [{"type": "image_url", "image_url": {"url": u}} for u in images]) if images else prompt
    provider = _provider()
    if provider.configured:
        try:
            # A01: pooled, not per-call. _llm also serves the chronos morning brief and
            # rumination, so this ran on a timer -- a new client and TLS handshake per
            # research tick, never closed.
            from backend.providers import gateway
            client = gateway.get_async_client(provider)
            started = time.time()
            resp = await client.chat.completions.create(
                model      = provider.standard,
                messages   = [{"role": "user", "content": content}],
                stream     = False,
                max_tokens = max_tokens,
                # Thinking explicitly disabled: the model reasons by default,
                # sharing max_tokens with the answer, which starved both the
                # picker (3000) and synthesis (8000) budgets before any JSON
                # appeared (live, 2026-09-01) -- see router.OPENROUTER_NO_THINKING_BODY.
                extra_body = with_provider_prefs(provider.no_thinking_body, provider),
            )
            if resp.usage:
                from backend.brain import usage as _usage
                _usage.record(
                    provider.standard, resp.usage.prompt_tokens, resp.usage.completion_tokens,
                    cost=getattr(resp.usage, "cost", None),
                )
            # H0: purpose comes from llm_calls.labelled(...) around the caller.
            _llm_calls.record_response(
                resp, model=provider.standard, started_at=started,
                extra_body=provider.no_thinking_body, max_tokens=max_tokens,
            )
            m = resp.choices[0].message
            text = _salvage_message_text(m)
            if text:
                return text
        except Exception as e:
            log.warning("research: %s call failed: %s", provider.name, e)
    return ""


def _parse_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()
    # Slice from the first '{' to the LAST '}' so both prose preambles
    # ("Let me think through this... {...}") and postambles/trailing fences
    # ("{...} \n```") don't break the parse -- mirrors engine.py's
    # _extract_json_array, which trims both ends the same way. The earlier
    # version only trimmed the leading prose, so a model that narrates its
    # reasoning before AND after the JSON object (a real, recurring kimi-k2.5
    # pattern on the picker call) always failed to parse even though a valid
    # JSON object was right there in the middle of the response.
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start:end + 1]
    from backend.brain.consolidation import _loads_salvaged
    return _loads_salvaged(raw)


async def _continue_truncated(prompt: str, partial: str, max_tokens: int) -> str:
    """One follow-up call asking the model to finish a synthesis response that
    looks truncated, instead of letting note_update_allowed's shrink floor
    silently discard this tick's actual findings. Tried once, not a retry
    loop. See consolidation.is_likely_truncated for the detection signal
    (unclosed JSON structure) shared with the consolidation pipeline.
    """
    from backend.brain.consolidation import _CONTINUATION_INSTRUCTION
    continuation_prompt = prompt + _CONTINUATION_INSTRUCTION.format(partial=partial)
    with _llm_calls.labelled("research.synthesis.continuation"):
        more = await _llm(continuation_prompt, max_tokens=max_tokens)
    if not more:
        return partial
    return partial + more


# ---------------------------------------------------------------------------
# The tick — pick, research, absorb, maybe notify
# ---------------------------------------------------------------------------

_PICK_PROMPT = _owner.say("""\
You are JARVIS choosing ONE subject and ONE research angle for a short autonomous research pass while «owner» is away.

The goal is to understand a subject well enough to explain it clearly and discuss its
nuance with «owner». Checking the news is only ONE of five ways to do that:
{angles}

Scored existing topic notes (score = how live in recent conversation + how overdue for research):
{shortlist}

Recent conversation excerpts (most recent first):
{recent}

Subjects «owner» closed to research -- never propose these, or a new note about them:
{closed}

Usually pick the top-scored topic. Pick something else ONLY if the recent conversation
clearly centers on a subject that has no topic note yet — then propose it as new.
Choose one concrete question that the note or conversation leaves insufficiently
explained. Prefer the angle that answers that question; last-angle variety is only a
tie-breaker. A perspective pass should clarify an idea before comparing it with
alternatives. Do not look for an objection just to create tension. Write queries that
can answer the question, not generic "latest news" or "criticism" searches.
If no worthwhile question is available, return {{"skip": true}} and leave the notes alone.

Respond with ONLY the JSON object, immediately — no analysis or preamble before it:
{{"subject": "the subject", "existing_file": "filename.md or null", "angle": "one of: news, deepen, perspectives, connections, questions", "question": "the specific question to answer", "why": "one sentence: why answering it serves «owner»", "queries": ["one or two web search queries shaped by the question"]}}
""")

# Angle-specific synthesis guidance, injected into _SYNTH_PROMPT.
_ANGLE_SYNTH_INSTRUCTIONS: dict[str, str] = {
    "news": "Integrate what actually happened — events, publications, discoveries — with dates.",
    "deepen": "Weave the deeper context into the sections it belongs to; this pass is about "
              "genuinely understanding an aspect better, not collecting headlines.",
    "perspectives": "First explain the idea in its strongest form. Then compare a serious "
                    "alternative fairly: where they agree, where they differ, and which "
                    "assumptions or evidence matter. Do not manufacture a disagreement.",
    "connections": "Draw the bridges explicitly: who influenced whom, who answered whom, what "
                   "contrasts with what — add or extend a 'Connections' section.",
    "questions": "Answer the open question you targeted as directly as the findings allow, then "
                 "update the Open questions section: remove what is now answered, refine what isn't.",
}

_SYNTH_PROMPT = _owner.say("""\
You are JARVIS. You just researched "{subject}" on your own initiative while «owner» was away.
Research angle for this pass: {angle} — {why}
Question to answer: {question}
{angle_instruction}

EXISTING NOTE (may be empty if this is a new topic):
{existing_note}

SEARCH FINDINGS:
{findings}

Source discipline: search snippets and page comments are third-party speech.
Never turn a web author's first-person statement, taste, investment, or history
into a fact about «owner». Existing notes are context, not proof of authorship.
For every new factual or numerical claim, preserve its source URL and date in
the note. When comparing figures, name the metric, period, units, and accounting
basis; calculate a trailing-year ratio from matching numerator and denominator
instead of copying a secondary site's unlabeled percentage. If figures cannot
be reconciled from primary data, state the uncertainty instead of declaring a
resolution. Do not put an unsupported number in the notification.

First decide what the evidence actually answers, what became clearer, and what remains
uncertain. If it adds no real understanding, set progress=false, note=null, notify=false,
and do not manufacture an objection, connection, or question to justify a pass.

When progress=true, write the full updated topic note in Markdown, without frontmatter.
Preserve supported details and citations, correct errors, and remove repetition where
possible. Do not grow the note merely to show activity. Explain a difficult central
idea in everyday language before its technical detail. Use one concrete example or
analogy when it helps, and say where an analogy stops being accurate. Define necessary
terms in place; preserve the nuance that matters. Make the causal or logical steps
clear enough that «owner» could ask a follow-up about them.
End with "## Open questions" only for genuine unresolved questions (at most two).

Send a message only if you can offer one useful, supported insight: a clear explanation,
a meaningful connection, a practical implication, or a well-supported correction.
Novelty and disagreement alone are not reasons to notify. In 1-3 short sentences,
state what you learned and why it matters; explain jargon simply. «Owner» may reply
asking you to clarify, so make the claim and its scope clear. Otherwise notify=false.

Return ONLY JSON, no prose:
{{"progress": true, "changed_understanding": "one sentence describing the actual gain in understanding", "note": "full note markdown, or null if progress=false", "notify": false, "message": "1-3 clear sentences to «owner», or null", "message_sources": ["URLs from SEARCH FINDINGS that directly support the message"]}}
""")


_WEB_PERSONAL_ASSERTION = re.compile(
    r"\b(?:" + re.escape(_owner.name()) + r"(?:'s)?|your own|you)\b.{0,90}\b(?:commented|posted|"
    r"wrote|said|testified|likes|loves|listens to|prefers|owns|invested in)\b"
    r"|\b(?:your|" + re.escape(_owner.name()) + r"'s)\s+(?:own\s+)?(?:\d{4}\s+)?"
    r"(?:comment|post|testimony)\b",
    re.I,
)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>)\]}'\"]+", re.I)


def _new_personal_assertion(existing: str, updated: str, message: str) -> bool:
    """Block autonomous creation of personal testimony from untrusted web text.

    Existing note lines may be retained, but the research loop cannot add a
    newly phrased personal assertion. A conversational turn can handle a
    genuine user-provided correction through the normal tools instead.
    """
    old_lines = {line.strip().casefold() for line in existing.splitlines()}
    for line in (updated + "\n" + message).splitlines():
        if line.strip().casefold() not in old_lines and _WEB_PERSONAL_ASSERTION.search(line):
            return True
    return False


def _message_sources(synth: dict, findings: str) -> list[str]:
    """Accept only URLs actually present in this pass's search findings."""
    available = {url.rstrip(".,;") for url in _URL_IN_TEXT.findall(findings)}
    proposed = synth.get("message_sources", [])
    if not isinstance(proposed, list):
        return []
    return [url for url in proposed if isinstance(url, str) and url in available][:3]


def _note_has_traceable_source(note: str, findings: str) -> bool:
    available = {url.rstrip(".,;") for url in _URL_IN_TEXT.findall(findings)}
    cited = {url.rstrip(".,;") for url in _URL_IN_TEXT.findall(note)}
    return bool(available & cited)

_FINDINGS_CAP = 5000   # chars of web_browse output per query fed to synthesis
_NOTE_MIN_CHARS = 100  # a "note" shorter than this is a failed generation, don't overwrite
_SHRINK_FLOOR = 0.6    # a rewrite below this fraction of the existing body is presumed truncated

# Input cap on the existing note shown to the synthesis prompt. Must cover
# real notes in full -- the prompt instructs the model to preserve everything
# in the existing note, which it obviously cannot do for content it was never
# shown. The old 4000-char cap silently truncated the two longest notes in
# the vault (Camus at ~6K, Dostoevsky at ~8.2K chars) before the model ever
# saw the back half, guaranteeing either a hallucinated "preservation" or a
# shrink-floor rejection. Set with headroom above the largest real note.
#
# Raised 16000 -> 20000 (2026-09-01): the vault's own growth re-exceeded the
# previous cap -- tesla-stock-research-2026-06.md reached 17,349 chars, over
# the old 16,000 ceiling, which the shrink-floor caught (rejected the rewrite,
# kept the old note) rather than silently corrupting it, but still meant that
# tick's actual research got discarded. 20000 covers it with real headroom for
# further growth. Verified safe against _SYNTH_MAX_TOKENS using the REAL
# measured chars/token ratio for this vault's content (scripts/
# measure_note_token_ratio.py, via Anthropic's actual token counting API, not
# a guessed ratio -- see test_synth_budget_covers_largest_real_note_reproduction):
# worst observed 3.82 chars/token means a 20,000-char note needs ~5,236 tokens
# to reproduce, comfortably under _SYNTH_MAX_TOKENS=8000 even with the test's
# 1.5x headroom factor applied (~7,854).
_EXISTING_NOTE_CAP = 20000


def note_update_allowed(existing_body: str, new_body: str) -> bool:
    """Pure guard: may `new_body` replace `existing_body`?

    A research rewrite that comes back much SHORTER than what it replaces is
    almost always a token-capped truncation, not a deliberate tightening —
    the synthesis response embeds the full note inside JSON, so the note is
    what gets cut when the budget runs out. This happened on the first live
    tick: a 5.7k-char hand-built timeline was replaced by a 1.1k truncated
    stub that sailed past the old 100-char minimum. Growth and modest trims
    pass; anything below _SHRINK_FLOOR of the existing body is rejected
    (the note keeps its content; last_researched still advances so the
    picker doesn't hammer the same topic every tick).
    """
    new_len = len(new_body.strip())
    if new_len < _NOTE_MIN_CHARS:
        return False
    old_len = len(existing_body.strip())
    if old_len == 0:
        return True
    return new_len >= old_len * _SHRINK_FLOOR


def _slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "topic"


def _with_frontmatter(note_body: str, existing_content: str, today: str,
                      angle: Optional[str] = None) -> str:
    """Attach/refresh frontmatter with last_researched (+ last_angle) on the
    model-written body.

    If the existing note had frontmatter, carry it over (updated); otherwise
    mint a minimal topic block. The model is told not to write its own.
    `angle=None` (e.g. the keep-existing-content failure paths) leaves any
    prior last_angle untouched — the pass didn't really happen.
    """
    from backend.tools.vault import _update_frontmatter
    updates = {"last_researched": today}
    if angle in _ANGLES:
        updates["last_angle"] = angle
    if existing_content.startswith("---"):
        lines = existing_content.splitlines()
        try:
            close = next(i for i, l in enumerate(lines[1:], start=1) if l.strip() == "---")
            fm = "\n".join(lines[: close + 1])
            merged = fm + "\n\n" + note_body.strip() + "\n"
            return _update_frontmatter(merged, updates)
        except StopIteration:
            pass
    angle_line = f"last_angle: {angle}\n" if angle in _ANGLES else ""
    fm = (
        "---\n"
        "type: topic\n"
        f"date: {today}\n"
        "tags: [topic]\n"
        f"last_researched: {today}\n"
        f"{angle_line}"
        "---\n\n"
    )
    return fm + note_body.strip() + "\n"


async def _tick() -> None:
    now = time.time()
    today = datetime.date.today().isoformat()

    # A queued request beats every heuristic: no picker, no scores, no
    # cooldown — someone deliberately asked for this one.
    from_queue = False
    queued_item = _pop_queued()
    if queued_item is not None:
        pick = queued_to_pick(queued_item)
        subject       = pick["subject"]
        queries       = pick["queries"]
        existing_file = pick["existing_file"]
        angle         = pick["angle"]
        why           = pick["why"]
        question      = pick["question"]
        from_queue    = True
        log.info("research: queued request %r angle=%s file=%s queries=%s",
                 subject, angle, existing_file, queries)
    else:
        candidates, recent = await asyncio.gather(
            _gather_candidates(now),
            _recent_exchange_lines(now),
        )
        if not candidates and not recent:
            log.debug("research: nothing to work with (no topics, no recent exchanges) — skipping")
            return

        best_score = candidates[0]["score"] if candidates else 0.0
        if not worth_attempting(best_score, bool(recent)):
            log.debug(
                "research: nothing worth attempting (best score %.2f, no recent conversation) — skipping tick",
                best_score,
            )
            return

        shortlist = candidates[:_MAX_SHORTLIST]
        shortlist_text = "\n".join(
            f"- {c['file']} — \"{c['title']}\" (score {c['score']:.2f}: "
            f"freshness {c['freshness']:.2f}, staleness {c['staleness']:.2f}; "
            f"last angle: {c['last_angle'] or 'none yet'})\n  excerpt: {c['digest'][:200]}"
            f"\n  unresolved question: {c.get('open_question') or '(none recorded)'}"
            for c in shortlist
        ) or "(no topic notes exist yet)"
        angles_text = "\n".join(f"- {name}: {a['desc']}" for name, a in _ANGLES.items())

        closed_titles = ", ".join(t for _n, t, _x in _closed_notes())
        with _llm_calls.labelled("research.picker"):
            pick_raw = await _llm(_PICK_PROMPT.format(
                angles=angles_text, shortlist=shortlist_text, recent=recent or "(none)",
                closed=closed_titles or "(none)",
            ))
        pick = _parse_json(pick_raw)

        if pick and pick.get("skip") is True:
            log.info("research: picker found no worthwhile question this pass")
            return

        if not _valid_pick(pick):
            fallback = fallback_pick(candidates)
            if fallback is None:
                log.warning(
                    "research: picker returned unusable output (%r) and no candidates to fall back to — skipping tick",
                    pick_raw[:120],
                )
                return
            log.warning(
                "research: picker returned unusable output (%r) — falling back to top-scored candidate %r",
                pick_raw[:120], fallback["subject"],
            )
            pick = fallback

        subject = str(pick["subject"]).strip()
        queries = [str(q).strip() for q in pick.get("queries", []) if str(q).strip()][:2]
        existing_file = pick.get("existing_file")
        known_files = {c["file"] for c in candidates}
        if existing_file not in known_files:
            existing_file = None
        if not queries:
            log.warning("research: no usable queries — skipping tick")
            return

        # An unknown/missing angle from the picker isn't worth rejecting the whole
        # pick over — coerce it to the deterministic rotation for that note.
        angle = str(pick.get("angle", "")).strip().lower()
        if angle not in _ANGLES:
            picked = next((c for c in candidates if c["file"] == existing_file), None)
            angle = next_angle(picked["last_angle"]) if picked else "news"
        why = str(pick.get("why", "")).strip() or "(no rationale given)"
        question = str(pick["question"]).strip()

        log.info("research: subject=%r angle=%s file=%s queries=%s — %s",
                 subject, angle, existing_file, queries, why)

    if not from_queue:
        # A picker "propose something new" pick, or a fallback rotation, must
        # never resurrect a topic the owner explicitly closed via research_queue
        # action="close" — closed families are already excluded from
        # `candidates` above, but a "new subject" pick with existing_file=null
        # bypasses that filter entirely and would just mint a fresh file for
        # the same closed subject. Queued requests are exempt (checked above
        # via from_queue) since a deliberate ask isn't the relevance argument
        # this veto exists to stop.
        probe_filename = existing_file or f"{_slugify(subject)}.md"
        closed_hit = _closed_family_conflict(subject, probe_filename,
                                             await _closed_similarity(subject))
        if closed_hit:
            # A veto used to end the tick, so a conversation-fresh subject that matched a
            # closed note was re-proposed and re-vetoed every tick (live: seven skipped
            # passes in one day). Candidates already exclude closed families, so the next
            # open candidate is a safe use of the pass.
            fallback = fallback_pick([c for c in candidates if c["file"] != existing_file])
            if fallback is None:
                log.info("research: skipping %r — matches closed topic %r, nothing else due",
                         subject, closed_hit)
                _log_research_pass(subject, angle, f"skipped: matches closed topic {closed_hit}",
                                   queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")
                return
            log.info("research: %r matches closed topic %r — researching %r instead",
                     subject, closed_hit, fallback["subject"])
            subject, existing_file = fallback["subject"], fallback["existing_file"]
            angle, why, question, queries = (fallback["angle"], fallback["why"],
                                            fallback["question"], fallback["queries"])

    # --- Web research ---
    from backend.tools.browser import web_browse
    from backend.tools import outcomes as _outcomes
    findings_parts: list[str] = []
    failed_queries: list[str] = []
    for q in queries:
        try:
            result = await web_browse(q)
        except Exception as e:
            log.debug("research: search raised for %r: %s", q, e)
            failed_queries.append(q)
            continue
        # A06: a failed search must never become synthesized evidence. web_browse
        # signals total failure by RETURNING a string ("SEARCH FAILED ...", "Could not
        # fetch ...") rather than raising, and every result used to be appended
        # unconditionally -- so synthesis was handed the failure text and asked to fold
        # "what you learned" into the note. Reproduced before this check existed. The
        # classification comes from tools/outcomes.py so it cannot drift from what the
        # rest of the system calls a failed web_browse.
        if _outcomes.is_failure("web_browse", result):
            log.warning("research: search failed for %r — excluded from findings", q)
            failed_queries.append(q)
            continue
        findings_parts.append(f"### Query: {q}\n{str(result)[:_FINDINGS_CAP]}")

    findings = "\n\n".join(findings_parts)
    if not findings.strip():
        # Covers both shapes now: every query raised, or every query came back a
        # failure string. Nothing advances last_researched, so the subject stays
        # eligible and is retried rather than being marked as researched on no data.
        log.warning("research: no usable search results for %r — skipping tick", subject)
        _log_research_pass(subject, angle, "failed: all searches failed", queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")
        return
    if failed_queries:
        # Partial failure is still worth synthesizing, but the model must not be told
        # the coverage was complete.
        findings += (f"\n\n### Note: {len(failed_queries)} of {len(queries)} searches "
                     f"returned no usable results. Base the update only on what is "
                     f"above; do not fill the gap from prior knowledge.")

    # --- Synthesis ---
    existing_content = ""
    # A05: capture the note's revision AT READ TIME. Synthesis below awaits an LLM call
    # measured in minutes, and every write path after it rewrites the WHOLE note from
    # this read -- so anything written to the same note in between would be destroyed.
    # Reproduced live before this existed. None = no existing note, nothing to protect.
    existing_revision: str | None = None
    if existing_file:
        try:
            existing_content = (_TOPICS_DIR / existing_file).read_text(encoding="utf-8", errors="replace")
        except Exception:
            existing_content = ""
        try:
            from backend.tools.vault import note_revision
            existing_revision = note_revision("topics", existing_file)
        except Exception:
            existing_revision = None

    # Resolved up front (both the parse-failure path and the write path need
    # them): filename, and existing_body for shrink-floor / keep-as-is writes.
    from backend.tools.vault import vault_write
    filename      = existing_file or f"{_slugify(subject)}.md"
    existing_body = _strip_frontmatter(existing_content)

    synth_prompt = _SYNTH_PROMPT.format(
        subject           = subject,
        angle             = angle,
        why               = why,
        question          = question,
        angle_instruction = _ANGLE_SYNTH_INSTRUCTIONS[angle],
        existing_note     = _strip_frontmatter(existing_content)[:_EXISTING_NOTE_CAP] or "(none — new topic)",
        findings          = findings,
    )
    with _llm_calls.labelled("research.synthesis"):
        synth_raw = await _llm(synth_prompt, max_tokens=_SYNTH_MAX_TOKENS)

    from backend.brain.consolidation import is_likely_truncated
    if is_likely_truncated(synth_raw):
        log.info(
            "research: synthesis for %s looks truncated (%d chars) — attempting one continuation",
            filename, len(synth_raw),
        )
        synth_raw = await _continue_truncated(synth_prompt, synth_raw, max_tokens=_SYNTH_MAX_TOKENS)
    # H1: still cut off after the continuation means the pass ran out of OUR output
    # limit. That says nothing about the note, so it must not count toward the failure
    # hold (_note_pass_outcome), which would otherwise park the note for days.
    cut_off = is_likely_truncated(synth_raw)

    synth = _parse_json(synth_raw)
    if not synth:
        # Hard parse failure (not a shrink-floor rejection -- that's handled
        # below). Still advance last_researched: leaving it untouched means
        # this topic stays maximally "overdue" and can win the shortlist
        # again next tick, retrying (and likely re-failing) the same subject
        # indefinitely instead of the loop moving on.
        log.warning("research: synthesis returned unusable output — skipping write")
        if existing_content:
            full_note = _with_frontmatter(existing_body, existing_content, today)
            await vault_write(filename, full_note, "topics",
                              expected_revision=existing_revision)
        _log_research_pass(subject, angle,
                           _CUT_OFF_OUTCOME if cut_off else "failed: synthesis unusable",
                           queued=from_queue, file=existing_file or f"{_slugify(subject)}.md")
        return

    if synth.get("progress") is not True or not str(
            synth.get("changed_understanding") or "").strip():
        log.info("research: no demonstrated gain in understanding for %s", subject)
        if existing_content:
            # A completed pass with no insight still counts for cooldown and
            # angle rotation, while the note's actual knowledge stays intact.
            await vault_write(
                filename, _with_frontmatter(existing_body, existing_content, today,
                                            angle=angle), "topics",
                expected_revision=existing_revision,
            )
        _log_research_pass(subject, angle, "no new understanding", queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")
        return

    new_body = str(synth.get("note", ""))
    if _new_personal_assertion(existing_body, new_body,
                               str(synth.get("message") or "")):
        log.warning("research: rejected unsupported personal attribution in %s", filename)
        _log_research_pass(subject, angle,
                           "failed: unsupported personal attribution", queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")
        return
    if not _note_has_traceable_source(new_body, findings):
        log.warning("research: refusing note update without a source URL from this pass")
        _log_research_pass(subject, angle,
                           "failed: note has no traceable source", queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")
        return

    if not note_update_allowed(existing_body, new_body):
        # Presumed truncation (or an empty/failed generation). Never destroy
        # the existing note — keep its body, but still advance last_researched
        # so the picker moves on instead of retrying (and re-truncating) the
        # same topic every tick. Notification is skipped too: a truncated
        # synthesis isn't trustworthy enough to message the owner about.
        log.warning(
            "research: rejected suspicious note rewrite for %s (%d -> %d chars) — keeping existing content",
            filename, len(existing_body.strip()), len(new_body.strip()),
        )
        if existing_content:
            full_note = _with_frontmatter(existing_body, existing_content, today)
            await vault_write(filename, full_note, "topics",
                              expected_revision=existing_revision)
        _log_research_pass(subject, angle,
                           _CUT_OFF_OUTCOME if cut_off
                           else "failed: rewrite rejected (presumed truncation)",
                           queued=from_queue, file=existing_file or f"{_slugify(subject)}.md")
        return

    full_note = _with_frontmatter(new_body, existing_content, today, angle=angle)
    write_result = await vault_write(filename, full_note, "topics",
                              expected_revision=existing_revision)

    # A05: the write can legitimately be refused — someone wrote this note while
    # synthesis was running, and this rewrite is derived from the pre-write read, so
    # applying it would delete their newer content. Declining is the point; what must
    # NOT happen is what this call site did before the result was checked at all:
    # claim "wrote topics/x.md", DM the owner about findings, and log a successful pass,
    # none of which happened. Nothing advances `last_researched` here because nothing
    # was written, so the topic stays eligible and the next tick re-researches it
    # against the fresh content rather than losing the subject.
    if _is_write_refused(write_result):
        log.warning("research: %s [%s] — note changed during synthesis, not overwriting",
                    subject, angle)
        _log_research_pass(subject, angle,
                           "skipped: note changed during synthesis (concurrent write)",
                           queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")
        return

    log.info("research: %s [%s] — %s", subject, angle, write_result)
    outcome = f"wrote topics/{filename} — {why}"

    # --- Maybe notify (model judgment + hard spam guard) ---
    if synth.get("notify") and synth.get("message") and _notifier is not None:
        message = str(synth["message"]).strip()
        sources = _message_sources(synth, findings)
        if not sources:
            log.warning("research: withholding DM for %s without traceable source URLs", subject)
            _log_research_pass(subject, angle,
                               "wrote note; DM withheld: no traceable sources", queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")
            return
        message += "\n\nSources: " + " ".join(sources)
        state = _load_state()
        last_notify = float(state.get("last_notify", 0.0))
        hold = _dm_hold_reason(now, last_notify)
        if not hold:
            # Piggyback any parked findings onto this DM — one message, no
            # extra gap consumed, nothing lost.
            pending = state.get("deferred_notifies", [])
            if pending:
                held = "\n".join(f"- {str(p.get('message', '')).strip()}"
                                 for p in pending
                                 if _sourced_notification(str(p.get("message", ""))))
                if held:
                    message += "\n\nAlso, from earlier passes:\n" + held
            sent_count = len(pending)
            sent = False
            try:
                sent = await _notifier(message)
            except Exception as e:
                log.debug("research: notify failed: %s", e)
            if sent:
                # A04 slice 4: apply only this pass's delta to FRESH state. The old code
                # wrote back a dict read before the DM, clobbering anything written
                # meanwhile -- `/pace` from Discord writes this same file, and a tick can
                # run for minutes. It also cleared deferred_notifies wholesale, which
                # would discard a finding deferred *during* the DM; only the entries
                # actually piggybacked onto this message are dropped now.
                def _mark_sent(st, _n=sent_count):
                    st["deferred_notifies"] = list(st.get("deferred_notifies", []))[_n:]
                    st["last_notify"] = now
                _update_state(_mark_sent)
                log.info("research: notified the owner about %r", subject)
                outcome += " (DM sent)"
        else:
            _defer_notification(subject, message)
            outcome += f" (DM held: {hold})"

    _log_research_pass(subject, angle, outcome, queued=from_queue,
                               file=existing_file or f"{_slugify(subject)}.md")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

async def _cycle() -> None:
    # Deferred findings go out as soon as the notify gap frees up — on the
    # 60s cycle, not the (much longer) tick cadence.
    await _flush_deferred(time.time())
    state = _load_state()
    pace = get_pace()
    if not should_tick(pace, float(state.get("last_tick", 0.0)), time.time()):
        return
    if _busy_check is not None and _busy_check():
        return   # conversation in flight — try again next minute
    state["last_tick"] = time.time()
    _save_state(state)
    await _tick()


async def run_loop() -> None:
    """Paced research loop. Started as a background task from main.py's lifespan.

    First tick happens one full interval after startup (never immediately —
    startup is exactly when the machine is busiest). last_tick is written
    BEFORE the tick runs so a failing tick waits a full interval instead of
    retrying every minute.
    """
    state = _load_state()
    if "last_tick" not in state:
        state["last_tick"] = time.time()
        _save_state(state)
    log.info("research: pace=%s", get_pace())
    await pacing.run_periodic(
        name="research", check_interval_s=_CHECK_INTERVAL_S, on_cycle=_cycle, pausable="work",
    )

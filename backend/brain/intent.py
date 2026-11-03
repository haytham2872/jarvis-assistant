"""backend/brain/intent.py — semantic intent gate: one embedding per turn.

Replaces the keyword-trigger prediction problem (personality.py's six trigger
sets + router.py's _COMPLEX_KEYWORDS) with embedding similarity against named
routes, each defined by example utterances instead of keywords. "What's
rattling around in that head of yours?" shares no keyword with any memory
trigger but lands on the memory route by meaning; "fix the meeting time" hits
the keyword "fix" but scores nowhere near the complex route.

Mechanics (the aurelio-labs semantic-router pattern, local-only):
  - At warmup, every route's utterances are embedded once into one matrix
    (the shared gte-small from embeddings.py — no second model).
  - Per turn, classify() embeds the user text ONCE (~25-30ms CPU) and takes
    one matmul against the matrix; a route's score is the max similarity over
    its utterances.
  - NOT YET DONE: this turn embedding is not reused by recall()/topic/skill/
    exchange search, which still each embed the query separately via
    mem0/semindex. Doing that would mean threading a precomputed vector
    through memory.py, vault.py, and threads.py's search signatures — and
    mem0's own hybrid search re-embeds internally regardless, so the win is
    partial. Left as a follow-up rather than bundled into this pass.
  - Self-model section routes are seeded with utterances AND enriched with
    chunks of the live digest (self_model.get_sections()), so a new CLAUDE.md
    section becomes routable without hand-editing any trigger list — the same
    auto-refresh philosophy as the digest cache itself.

Decision policy lives with the CALLERS (router.py / personality.py), which
treat an IntentResult as an optional richer signal and keep their regex sets
as the degradation floor: classify() returning None (embedder missing, matrix
not warm yet) means "fall back to keywords", never "fail the turn". Same
silent-degradation contract as every other brain module.

Thresholds (_FLOORS) are MEASURED, not guessed — gte-small's unrelated-query
cosine baseline sits at ~0.72-0.81, so intuitive-looking floors like 0.5
filter nothing (the exact lesson of memory_search's _SEARCH_SCORE_MIN).
Calibration: scripts/measure_intent_floors.py, run 2026-08-27 — held-out
paraphrases vs. everyday-chat negatives; floors sit between the negative p95
and the positive minimum for each route, re-runnable any time utterances
change.
"""
from backend import owner as _owner

import time
import asyncio
import logging
import threading
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Route definitions — example utterances, not keywords.
#
# Utterances are matched by MAX similarity, so each should be one natural,
# complete way of asking; variety of phrasing matters more than volume.
# Section routes ("sm:*") map 1:1 to self_model._SECTION_HEADINGS keys.
# ---------------------------------------------------------------------------

ROUTES: dict[str, list[str]] = {
    # -- router: does this turn deserve the strong model + deep context? -----
    "complex": [
        "debug this python error for me",
        "write a script that renames all these files",
        "why does the backend crash when I close the window",
        "explain how this algorithm works step by step",
        "help me plan the architecture for a new feature",
        "compare these two approaches and tell me the trade-offs",
        "refactor this function so it's easier to test",
        "analyze what went wrong in this log output",
        "design a schema for storing the session data",
        "what's the difference between these two libraries",
        "walk me through how the request flows through the system",
        "implement a fallback path for when the API is down",
        "do you remember what we decided about the memory system",
        "what do you know about me after all these conversations",
        "tell me about the letter you wrote to yourself",
        "research this topic and give me a real synthesis",
        "what would break if you changed how this module writes its output",
        "trace how this function actually reaches the database",
        "what depends on this piece of code",
        "how does this part of the system connect to that other part",
        "dig into why these tests keep failing intermittently",
        "what did we settle on for the design of this feature last time",
    ],
    # -- personality.py gates ------------------------------------------------
    "runtime": [
        "how are you running right now",
        "how many days has your backend process stayed alive without restarting",
        "give me your vital signs and process uptime",
        "how much has this session cost you in API spend",
        "how much have you spent on API calls today",
        "is your memory subsystem healthy right now",
        "are all your backend systems working",
        "how many conversation turns have we done today",
        "any background tasks running at the moment",
        "what's your current process status",
        "give me a diagnostic readout of your own health",
    ],
    "opinion": [
        "what do you think about free will",
        "what's your take on this whole situation",
        "what's your opinion of that author",
        "where do you stand on this question",
        "do you actually believe that",
        "have you changed your mind about anything lately",
        "what's your position on using AI for art",
        _owner.say("do you agree with what «he» said"),
        "what's your philosophy on memory and identity",
    ],
    "whats_new": [
        "what's new with you specifically these days",
        "did you yourself get any new features recently",
        "what changed about you since we last talked",
        "when was this feature added to your own code",
        "have you personally been updated lately",
        "what have you learned to do lately",
        "is there anything different about you now",
        "what's changed in your own code recently",
        "how long have you had this ability yourself",
        "what did you add to yourself recently",
        "were you upgraded with anything new",
        "what new capabilities do you have now",
    ],
    "calibration": [
        "how well do you know me by now",
        "do you really understand me",
        "how predictable am I to you",
        "do you think you can read me well",
        "how good are your predictions about me",
        "do you actually get me",
        "how accurate is your model of me",
    ],
    "self_reflection": [
        "who are you really",
        "tell me about yourself",
        "what is it like to be you",
        "are you conscious",
        "do you actually feel anything",
        "what's your nature, what are you",
        "do you experience time passing",
        "how do you work",
        "how were you built",
        "tell me about your own code",
        "what does your self model say",
        "are you alive in any sense",
    ],
    # -- self-model section routes (keys match _SECTION_HEADINGS) ------------
    "sm:frontend": [
        "how does your interface work",
        "what's the neural sphere and how is it animated",
        "tell me about the UI you show on screen",
        "how does the electron app talk to the backend",
        "what do the visuals react to when you speak",
        "what is that glowing orb on the screen",
        "why did the ball change color just now",
        "what does the floating sphere animation mean",
    ],
    "sm:tools": [
        "what tools do you have",
        "what can you actually do",
        "can you search the web and send emails",
        "what new capabilities did you get",
        "how does your research loop work while I'm away",
        "can you control my computer",
        "what can you do from my phone over discord",
    ],
    "sm:memory": [
        "how does your memory work",
        "what happens to what we talk about after I leave",
        "how do you remember things between sessions",
        "what's in the letters you write yourself",
        "what happens when you go to sleep",
        "how do you consolidate a session",
        "how do you form opinions and keep positions",
        "do you make predictions about me",
        "what's rattling around in that head of yours",
    ],
    "sm:personality": [
        "how does your mood work",
        "why do you sound sardonic today",
        "what shapes your character",
        "how do your emotions drift over time",
        "what's your tonal register based on",
    ],
    "sm:tts": [
        "how does your speaking voice work",
        "why does your speaking voice sound different sometimes",
        "how do you turn written text into spoken words",
        "what happens when your text to speech service is down",
        "who is the voice actor behind how you talk",
        "explain how you generate spoken audio from your replies",
    ],
    "sm:router": [
        "which model are you using right now",
        "when do you use the bigger model",
        "what version are you",
        "how do you decide to think harder about something",
    ],
    "sm:stt": [
        "how do you hear me",
        "how does the wake word work",
        "how do you transcribe what I say",
        "why didn't you respond when I said hey jarvis",
        "how do you turn my speech into text",
        "what happens when you mishear something I said",
        "how sensitive is your microphone",
    ],
    "sm:claude": [
        "how do you process a request end to end",
        "what's in your system prompt",
        "how are you programmed to respond",
        "what rules were you given",
        "how does your pipeline handle a turn",
    ],
    "sm:scripts": [
        "how do I start you up from the terminal",
        "how do I run your first-time setup",
        "what powershell scripts launch your backend",
        "how do I run you without the microphone enabled",
        "what's the installation procedure for your project",
        "how do I shut your process down cleanly",
    ],
}

# Floors measured by scripts/measure_intent_floors.py (2026-08-27, re-run
# after reseeding sm:tts/sm:stt/sm:scripts to fix two live collisions — see
# git history of this file), one per route — the data shows no single shared
# floor exists (held-out-positive minima 0.79-0.92, everyday-negative maxima
# 0.79-0.85, and they interleave differently per route). Each is set to
# neg_max + a small buffer, i.e. it rejects the single worst everyday-chat
# negative found for that route, not just the p95 the calibration script
# suggests (p95 alone let two real collisions through — see below). Bias is
# precision-first: a false injection costs tokens on every casual turn, while
# a false miss has three backstops (the regex fallback OR'd in by callers,
# the sticky-deep window, and the pull tools get_self_model/memory_search).
#
# Known accepted edge: "tell me a joke" scores 0.850 on self_reflection —
# above any floor that keeps the real positives (min 0.831) — so a joke
# request may pull the (arguably relevant) personality section; benign,
# documented, not worth losing "what's it like being you".
#
# Live collisions found and fixed by reseeding (not just floor-tuning) rather
# than accepted, because the negative scored ABOVE the weakest genuine
# positive -- no floor could separate them without a seed change:
#   - "remind me to call mom at six" scored 0.821 on the original sm:stt
#     seeds (which included "...when I called you") vs. a 0.813-0.820
#     positive range -- pure lexical collision on "call". Reseeded without
#     "call"; new positive floor is 0.920, negative max dropped to 0.825
#     ("tell me a joke", now well clear).
#   - "turn the volume down a bit" scored 0.830 on sm:tts and 0.828 on
#     sm:scripts against seeds using bare "voice"/"how do I run you" --
#     collides with music-playback vocabulary. Reseeded toward "speaking
#     voice"/"spoken audio" and "your backend"/"your process"; positive
#     floors rose to 0.848 and 0.860, negative maxima dropped to 0.825/0.821.
_FLOORS: dict[str, float] = {
    "complex":         0.812,   # neg_max 0.808 ("tell me a joke"); regex+length back up the rest
    "runtime":         0.857,   # neg_max 0.853 ("hey what time is it right now") -- thin (0.008),
                                 # structural collision between "your uptime" and "current time"
                                 # that three seed-rewording passes didn't widen; accepted as-is,
                                 # same class as self_reflection's accepted overlap below
    "whats_new":       0.842,   # neg_max 0.838 ("what's new with the neighbors") -- thin (0.008),
                                 # same structural-collision class as runtime's "current time" gap;
                                 # two reseed passes (emphasizing "about you"/"your own code")
                                 # narrowed it from 0.005 to 0.008 but couldn't fully separate a
                                 # phrase that's genuinely almost the same words about a different
                                 # subject. Accepted as-is.
    "opinion":         0.817,   # neg_max 0.814 ("I think we should leave at eight")
    "calibration":     0.835,   # neg_max 0.832 ("tell me a joke")
    "self_reflection": 0.830,   # accepted overlap with "tell me a joke" (0.850) -- see above
    "sm:frontend":     0.812,   # neg_max 0.806 ("what time is it") -- raised after adding time-check negatives
    "sm:tools":        0.816,   # neg_max 0.812 ("what's new with the neighbors") -- raised after
                                 # adding the whats_new negative pool, same "new negative reveals a
                                 # pre-existing gap in an unrelated route" pattern as sm:router/
                                 # sm:frontend earlier; pos_min 0.851 leaves plenty of room
    "sm:memory":       0.826,   # neg_max 0.823 ("I think we should leave at eight") -- thin, watch this one
    "sm:personality":  0.812,   # neg_max 0.808
    "sm:tts":          0.833,   # neg_max 0.825 ("turn the volume down a bit"), reseeded
    "sm:router":       0.853,   # neg_max 0.849 ("hey what time is it right now") -- raised, same reason
    "sm:stt":          0.870,   # neg_max 0.825 ("tell me a joke"), reseeded
    "sm:claude":       0.811,   # neg_max 0.807
    "sm:scripts":      0.826,   # neg_max 0.821 ("turn the volume down a bit"), reseeded
}
# New digest sections that gain a route later (via digest enrichment) but have
# no measured floor yet gate at this conservative default.
_DEFAULT_FLOOR = 0.828
_MAX_SECTIONS  = 2       # same cap as personality._MAX_SELF_SECTIONS

# Relative-margin suppression, on top of the absolute floors above. Discovered
# live during wiring, not predicted by calibration: gte-small has a real
# "hub" pathology for short, generic, near-exact-match phrases — a query that
# scores near 1.0 against one route's seed (an exact or near-exact utterance
# match) sits in a region of embedding space with elevated similarity to
# EVERY other route too. "who are you really" (verbatim in self_reflection's
# seeds) scored 1.000 on self_reflection but *also independently cleared the
# absolute floor* of runtime, opinion, calibration, complex, and three sm:*
# routes simultaneously — a 6-way false-positive stack an absolute floor
# alone cannot catch, because each of those scores (0.81-0.87) is a perfectly
# ordinary value for its OWN route in isolation. The fix: a route only counts
# if its score is also within _MARGIN of the best score within its FAMILY
# (see _FAMILY below) — not the global best. Measured gap between a genuine
# hub winner and its runner-up was 0.13-0.18 in both cases found; 0.06 sits
# well clear of that while still passing genuinely close ties (e.g. a message
# that legitimately touches two routes at comparable strength).
_MARGIN = 0.06

# Families the margin is computed within. This split exists because "trace
# how a conversation turn reaches your mood engine" scored sm:personality
# 0.893 (correct) but also complex 0.827 — a perfectly good complex/router
# signal that a GLOBAL margin would suppress purely because an unrelated
# personality-section hit happened to be more dominant for this phrasing.
# complex governs model routing (standard vs thinking tier) at a completely different
# call site than the self-facing gates below it, decided independently —
# it must never be judged against a self-model hub score it has nothing to
# do with, so it gets its own singleton family (margin against itself is a
# no-op; only its absolute floor and the negative-separation from
# calibration apply). The self-facing gates genuinely do compete with each
# other for the SAME reason self_reflection used to short-circuit section
# matching in the old keyword design (_SELF_PHRASES matching skipped the
# section loop entirely) — so they share one family and mutually suppress.
_FAMILY: dict[str, str] = {"complex": "task"}
for _name in ROUTES:
    if _name != "complex":
        _FAMILY[_name] = "self"
del _name

# Digest enrichment: chunks of each section's live digest appended to its
# route so content added to CLAUDE.md becomes routable automatically.
_DIGEST_CHUNK_CHARS  = 400
_DIGEST_CHUNKS_MAX   = 3

# Descriptive digest prose scores systematically lower against short spoken
# questions than curated utterances do, so digest-chunk vectors get their own
# (higher) bar: they exist to catch questions about NEW sections that have no
# seeds yet, not to outvote the seeds.
_DIGEST_CHUNK_FLOOR_BONUS = 0.02


@dataclass
class IntentResult:
    """One turn's semantic classification. Scores are max-sim per route."""
    scores: dict[str, float]
    query_vec: list[float]
    elapsed_ms: float = 0.0

    def _family_best(self, name: str) -> float:
        family = _FAMILY.get(name, name)   # unknown routes: singleton family of themselves
        return max(
            (v for r, v in self.scores.items() if _FAMILY.get(r, r) == family),
            default=0.0,
        )

    def is_route(self, name: str) -> bool:
        """A route counts only if it clears its own floor AND sits within
        _MARGIN of the best score within its FAMILY (see _MARGIN/_FAMILY
        docstrings — suppresses the multi-route false-positive stack a hub
        query causes, without letting an unrelated family's hub score
        suppress an orthogonal decision like model routing)."""
        floor = _FLOORS.get(name, _DEFAULT_FLOOR)
        score = self.scores.get(name, 0.0)
        return score >= floor and score >= self._family_best(name) - _MARGIN

    def top_sections(self, k: int = _MAX_SECTIONS) -> list[str]:
        """Self-model sections clearing floor+margin, best first, no sm: prefix."""
        hits = sorted(
            ((s, v) for s, v in self.scores.items() if s.startswith("sm:") and self.is_route(s)),
            key=lambda x: -x[1],
        )
        return [s[3:] for s, _ in hits[:k]]


# ---------------------------------------------------------------------------
# Matrix build + warmup
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_matrix = None                 # np.ndarray (N, 384), unit-normalised
_row_routes: list[str] = []    # route name per matrix row
_row_floor_bonus: list[float] = []   # per-row floor adjustment (digest chunks)
_digest_included = False
_digest_upgrade_in_progress = False


def _digest_chunks() -> list[tuple[str, str]]:
    """(route, chunk) pairs from the live self-model digest, or [] if not warm."""
    try:
        from backend.brain.self_model import is_ready, get_all_sections
        if not is_ready():
            return []
        out: list[tuple[str, str]] = []
        for section, text in get_all_sections().items():
            if f"sm:{section}" not in ROUTES:
                continue   # digest section with no route — ignore, never invent
            for i in range(_DIGEST_CHUNKS_MAX):
                chunk = text[i * _DIGEST_CHUNK_CHARS:(i + 1) * _DIGEST_CHUNK_CHARS].strip()
                if len(chunk) > 80:
                    out.append((f"sm:{section}", chunk))
        return out
    except Exception:
        return []


def _build_matrix_sync() -> bool:
    """Embed all route utterances (+ digest chunks) into one matrix. False on failure."""
    global _matrix, _row_routes, _row_floor_bonus, _digest_included
    from backend.brain import embeddings
    texts:  list[str] = []
    routes: list[str] = []
    bonus:  list[float] = []
    for route, utterances in ROUTES.items():
        for u in utterances:
            texts.append(u)
            routes.append(route)
            bonus.append(0.0)
    chunks = _digest_chunks()
    for route, chunk in chunks:
        texts.append(chunk)
        routes.append(route)
        bonus.append(_DIGEST_CHUNK_FLOOR_BONUS)
    vecs = embeddings._embed_many_sync(texts)
    if not vecs:
        return False
    try:
        import numpy as np
        with _lock:
            _matrix = np.asarray(vecs, dtype="float32")
            _row_routes = routes
            _row_floor_bonus = bonus
            _digest_included = bool(chunks)
        log.info("Intent gate ready: %d routes, %d vectors (%d digest chunks)",
                 len(ROUTES), len(texts), len(chunks))
        return True
    except Exception as e:
        log.warning("Intent matrix build failed: %s", e)
        return False


def warm_intent() -> None:
    """Build the route matrix in a background daemon thread at startup.

    Runs after the embedder warms (embedding ~150 short texts, ~1-2s once the
    model is resident). If the self-model digest isn't ready yet, the matrix
    is built seeds-only and classify() upgrades it in the background the
    first time it notices the digest became available.
    """
    def worker():
        _build_matrix_sync()
    threading.Thread(target=worker, daemon=True, name="intent-warmup").start()


def _maybe_upgrade_with_digest() -> None:
    """One background rebuild when the digest became ready after first build.

    A transient rebuild failure (e.g. the embedder momentarily unavailable)
    must not permanently block future attempts -- only the in-progress flag
    is claimed up front; _digest_included is set by _build_matrix_sync itself,
    only on confirmed success, so a failed attempt is retried on a later turn.
    """
    global _digest_upgrade_in_progress
    if _digest_included or _digest_upgrade_in_progress:
        return
    try:
        from backend.brain.self_model import is_ready
        if is_ready():
            _digest_upgrade_in_progress = True

            def worker():
                global _digest_upgrade_in_progress
                try:
                    _build_matrix_sync()
                finally:
                    _digest_upgrade_in_progress = False

            threading.Thread(target=worker, daemon=True,
                             name="intent-digest-upgrade").start()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_sync(text: str) -> IntentResult | None:
    """Embed once, score every route. None → caller falls back to keywords."""
    if not text or not text.strip():
        return None
    with _lock:
        matrix = _matrix
        routes = list(_row_routes)
        bonus  = list(_row_floor_bonus)
    if matrix is None:
        return None
    from backend.brain import embeddings
    t0 = time.perf_counter()
    vecs = embeddings._embed_many_sync([text[:1000]])
    if not vecs:
        return None
    try:
        import numpy as np
        q = np.asarray(vecs[0], dtype="float32")
        sims = matrix @ q          # both sides unit-normalised → cosine
        scores: dict[str, float] = {}
        for i, route in enumerate(routes):
            # Digest-chunk rows carry a floor bonus: subtracting it from the
            # similarity (rather than tracking per-row floors downstream)
            # keeps IntentResult's single-floor-per-route contract simple.
            s = float(sims[i]) - bonus[i]
            if s > scores.get(route, 0.0):
                scores[route] = s
        elapsed = (time.perf_counter() - t0) * 1000
        _maybe_upgrade_with_digest()
        return IntentResult(scores=scores, query_vec=list(vecs[0]), elapsed_ms=elapsed)
    except Exception as e:
        log.debug("intent classify failed: %s", e)
        return None


async def classify(text: str) -> IntentResult | None:
    """Async wrapper — the embed is CPU-blocking, so it runs in the executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, classify_sync, text)

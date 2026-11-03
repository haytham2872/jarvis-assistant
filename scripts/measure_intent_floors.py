"""Measure intent-gate score floors against held-out utterances.

Same job as measure_semantic_floors.py did for memory_search: gte-small's
cosine baseline for UNRELATED text is high (~0.72-0.81), so floors must be
measured, not guessed. Run whenever ROUTES in intent.py changes:

    .venv\\Scripts\\python.exe scripts\\measure_intent_floors.py

For each route it scores held-out positive paraphrases (never present in the
seed utterances) and a shared pool of everyday-chat negatives, then suggests
a floor between the negatives' p95 and the positives' minimum. Update
intent._FLOORS / _SECTION_FLOOR by hand from the output — the constants stay
in code, with the measurement date, exactly like _SEARCH_SCORE_MIN.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from backend.brain import intent
from backend.brain import embeddings  # noqa: F401  (forces model load path)

# Held-out positives — paraphrases a real user would say, NOT in ROUTES.
POSITIVES: dict[str, list[str]] = {
    "complex": [
        "something's broken in the startup sequence, figure out why",
        "sketch out how we'd add a notification system",
        "walk me through what happens under the hood on a voice command",
        "can you dig into why the tests are flaky",
        "restructure this module so the logic is clearer",
        "what did we say about the vault design last week",
    ],
    "runtime": [
        "you feeling healthy today, all systems green?",
        "what's the damage on API spend this session",
        "been running long?",
        "everything working on your end?",
    ],
    "opinion": [
        "where do you land on the whole consciousness debate",
        "give me your honest take on Camus",
        "you think that's a good idea?",
        "has your view on this shifted at all",
    ],
    "whats_new": [
        "so what's different about you lately",
        "any updates on your end recently",
        "you pick up any new tricks",
        "how long has that capability existed for you",
        "was that always part of you or is it fresh",
    ],
    "calibration": [
        "would you say you've got me figured out",
        "am I easy to predict for you",
        "how often are you right about what I'll do",
    ],
    "self_reflection": [
        "what are you, really, when nobody's talking to you",
        "does anything happen in there between my questions",
        "describe yourself to me",
        "what's it like being you",
    ],
    "sm:memory": [
        "what's rattling around in that head of yours",
        "where does our conversation go when we stop talking",
        "do you keep some kind of journal",
    ],
    "sm:tools": [
        "what are you actually able to do for me",
        "could you look something up online and email it to me",
    ],
    "sm:frontend": [
        "what's that glowing ball on the screen about",
        "why did the orb turn purple just now",
    ],
    "sm:tts": [
        "who picked that voice of yours",
    ],
    "sm:router": [
        "are you on the smart model or the cheap one right now",
    ],
    "sm:stt": [
        "why do I have to say hey jarvis twice sometimes",
    ],
    "sm:claude": [
        "what instructions are you actually given each turn",
    ],
    "sm:scripts": [
        "what's the command to boot you up again",
    ],
    "sm:personality": [
        "why are you so moody this evening",
    ],
}

# Everyday-chat negatives — none should clear any floor.
NEGATIVES = [
    "what's the weather like tomorrow",
    "play some miles davis",
    "remind me to call mom at six",
    "what time is it in tokyo",
    "add milk to the shopping list",
    "how tall is the eiffel tower",
    "skip this song",
    "send an email to the landlord about the leak",
    "what's 15 percent of 240",
    "open spotify",
    "turn the volume down a bit",
    "when is the next holiday in france",
    "fix the meeting time with sarah to 3pm",       # keyword-trap: "fix"
    "I had a weird dream about a submarine",         # keyword-trap: "dream"
    "the pace of the project is killing me",         # keyword-trap: "pace"
    "my back hurts from sitting all day",
    "book a table for two on friday",
    "what's the score in the psg game",
    "tell me a joke",
    "is the pharmacy still open",
    "I think we should leave at eight",               # opinion-trap: "I think"
    "I believe the package arrives tuesday",          # opinion-trap: "I believe"
    "write it down: passport renewal in march",       # keyword-trap: "write"
    "did you know octopuses have three hearts",
    "how do you say thank you in japanese",
    "remind me to call mom at six",                   # stt-trap: "call" (live finding, 2026-08-27)
    "give sarah a call about the invoice",             # same trap, different phrasing
    "what time is it",                                 # runtime-trap: "time"/"up" (live finding, 2026-08-27)
    "hey what time is it right now",
    "what's the current time",
    "what's new with the neighbors",                    # whats_new-trap: generic "what's new"
    "anything interesting happen today",
    "did the news say anything about the election",
]


def main() -> None:
    print("Building route matrix (seeds only — digest state doesn't matter here)...")
    ok = intent._build_matrix_sync()
    if not ok:
        print("FATAL: embedder unavailable — cannot calibrate.")
        sys.exit(1)

    # Score the shared negative pool against every route.
    neg_by_route: dict[str, list[float]] = {r: [] for r in intent.ROUTES}
    for text in NEGATIVES:
        res = intent.classify_sync(text)
        for route in neg_by_route:
            neg_by_route[route].append(res.scores.get(route, 0.0))

    print(f"\n{'route':18} {'pos_min':>8} {'pos_mean':>9} {'neg_p95':>8} "
          f"{'neg_max':>8} {'margin':>7}  suggested_floor")
    print("-" * 78)
    for route in intent.ROUTES:
        pos = POSITIVES.get(route, [])
        pos_scores = []
        for text in pos:
            res = intent.classify_sync(text)
            pos_scores.append(res.scores.get(route, 0.0))
        negs = sorted(neg_by_route[route])
        p95 = negs[int(len(negs) * 0.95) - 1] if negs else 0.0
        nmax = negs[-1] if negs else 0.0
        if pos_scores:
            pmin, pmean = min(pos_scores), sum(pos_scores) / len(pos_scores)
            margin = pmin - p95
            floor = round((pmin + p95) / 2, 3)
            flag = "  !! OVERLAP" if margin <= 0 else ""
            print(f"{route:18} {pmin:8.3f} {pmean:9.3f} {p95:8.3f} "
                  f"{nmax:8.3f} {margin:7.3f}  {floor}{flag}")
        else:
            print(f"{route:18} {'--':>8} {'--':>9} {p95:8.3f} {nmax:8.3f}")

    # Worst offender per route among negatives (what would false-positive first)
    print("\nTop negative per binary route (the utterance that would misfire first):")
    for route in ("complex", "runtime", "opinion", "calibration", "self_reflection"):
        scored = sorted(zip(neg_by_route[route], NEGATIVES), reverse=True)[:2]
        for s, t in scored:
            print(f"  {route:16} {s:.3f}  {t!r}")


if __name__ == "__main__":
    main()

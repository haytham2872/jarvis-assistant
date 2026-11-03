"""Measure the real chars-per-token ratio for vault topic notes.

Same job as measure_intent_floors.py / measure_semantic_floors.py did for their
constants: research.py's _SYNTH_MAX_TOKENS budget-coverage test used to assume an
English-prose ~3.3 chars/token ratio, guessed rather than measured -- this vault's
notes are markdown (headers, bold, wiki-links, occasional non-ASCII punctuation like
em-dashes), which tokenizes differently than plain prose. Run whenever the vault's
largest note changes meaningfully, or research.py's token budgets are revisited:

    .venv\\Scripts\\python.exe scripts\\measure_note_token_ratio.py

Uses Anthropic's real Messages API token counting (client.messages.count_tokens) --
an actual API call, which is exactly why this is a one-time measurement script and
not something the test suite calls live (the suite never makes real API calls). The
measured ratio is baked into test_research.py's
test_synth_budget_covers_largest_real_note_reproduction by hand, with the
measurement date, same convention as _SEARCH_SCORE_MIN / intent._FLOORS.
"""

import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import anthropic   # pip install anthropic: Anthropic's token-counting API

from backend.brain.research import _strip_frontmatter

_TOPICS_DIR = pathlib.Path(__file__).parent.parent / "jarvis-vault" / "topics"
_MODEL = "claude-haiku-4-5-20251001"


def main() -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY not set -- can't measure real token counts. Aborting.")
        sys.exit(1)

    if not _TOPICS_DIR.exists():
        print(f"No vault at {_TOPICS_DIR} -- nothing to measure.")
        sys.exit(1)

    notes = sorted(_TOPICS_DIR.glob("*.md"))
    if not notes:
        print("Vault has no topic notes yet -- nothing to measure.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print(f"{'file':45s} {'chars':>8s} {'tokens':>8s} {'chars/tok':>10s}")
    ratios: list[float] = []
    largest_chars = 0
    largest_tokens = 0
    for path in notes:
        body = _strip_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if not body.strip():
            continue
        resp = client.messages.count_tokens(
            model=_MODEL,
            messages=[{"role": "user", "content": body}],
        )
        tokens = resp.input_tokens
        chars = len(body)
        ratio = chars / tokens if tokens else 0.0
        ratios.append(ratio)
        if chars > largest_chars:
            largest_chars, largest_tokens = chars, tokens
        print(f"{path.name:45s} {chars:8d} {tokens:8d} {ratio:10.2f}")

    if not ratios:
        print("No measurable notes.")
        sys.exit(1)

    worst_ratio = min(ratios)  # fewest chars per token = most token-dense = worst case for a char-based cap
    avg_ratio = sum(ratios) / len(ratios)
    print()
    print(f"Average chars/token: {avg_ratio:.2f}")
    print(f"Worst (most token-dense) chars/token: {worst_ratio:.2f}")
    print(f"Largest note: {largest_chars} chars = {largest_tokens} tokens")
    print()
    print("Update test_research.py's test_synth_budget_covers_largest_real_note_reproduction "
          f"to use {worst_ratio:.2f} chars/token (the worst observed, not the average -- a "
          "budget check should hold for the most token-dense real note, not just a typical one), "
          "dated with today's measurement.")


if __name__ == "__main__":
    main()

"""Score the shadow promise check (G3, B1) on the held-out corpus. Offline, no model call.

    .venv/Scripts/python.exe scripts/measure_promise_check.py

Prints, per case, what was expected and what came out; then detection precision/recall,
verdict accuracy on the detected promises, and false alarms on the promise-free replies.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend.brain import promise_check as P  # noqa: E402
from promise_cases import CASES  # noqa: E402


def main() -> None:
    tp = fp = fn = right = 0
    negatives = false_alarms = 0
    for reply, tools, expected in CASES:
        got = [(k, P.verdict(k, set(tools))[0]) for k, _ in P.detect(reply)]
        mark = "ok " if got == expected else "XX "
        print(mark, expected, "->", got, "|", reply[:80])
        tp += min(len(got), len(expected))
        fp += max(0, len(got) - len(expected))
        fn += max(0, len(expected) - len(got))
        right += sum(1 for g, e in zip(got, expected) if g == e)
        if not expected:
            negatives += 1
            false_alarms += bool(got)
    print(f"\ndetection: tp={tp} fp={fp} fn={fn}  precision={tp / max(1, tp + fp):.2f} "
          f"recall={tp / max(1, tp + fn):.2f}")
    print(f"kind+verdict right on {right}/{tp} detected promises")
    print(f"promise-free replies flagged: {false_alarms}/{negatives}")


if __name__ == "__main__":
    main()

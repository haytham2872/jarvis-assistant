"""backend/brain/predictions.py — JARVIS's predictive model of the owner, scored.

At the end of a session, consolidation writes 2-4 falsifiable predictions
about how the owner will behave in the NEXT session ("he'll push back on X",
"he'll bring up Y again"). At the *next* consolidation, those pending
predictions are scored right/wrong/unclear against that session's actual
threads, and a running calibration record accumulates.

The file (jarvis-vault/people/predictions.md) is machine-owned: consolidation
only ever emits the raw ingredients (verdicts, new prediction text) as JSON;
every byte of file surgery — moving lines from Pending to Scored, bumping
counters, capping the scored log — happens here, deterministically. This
sidesteps the full-rewrite truncation risk documented in research.py
(_SHRINK_FLOOR) entirely, since the LLM never rewrites the file itself.

Format:
    # Predictions — the owner
    <!-- calibration: right=4 wrong=2 unclear=1 -->
    Calibration: 4 right / 2 wrong / 1 unclear — hit rate 67% (of decided)

    ## Pending
    [pending] 2026-07-16 | sid:ab12cd34 | He will open with the rumination
    feature, not the bug list.

    ## Scored
    [wrong] 2026-07-14 | sid:9f00aa11 | He'd be annoyed by X — evidence: he
    laughed it off.

Same silent-degradation contract as the rest of the brain: every function
here returns a harmless default on malformed input rather than raising.
"""
from backend import owner as _owner

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_VAULT = _PROJECT_ROOT / "jarvis-vault"
_PRED_FILE = _VAULT / "people" / "predictions.md"

_SCORED_CAP = 30          # newest-first; pending is uncapped (2-4/session, scored away next session)
_PENDING_BLOCK_CAP = 1200  # chars — read_pending_block() output cap

_LINE_RE = re.compile(
    r"^\[(pending|right|wrong|unclear)\]\s+(\d{4}-\d{2}-\d{2})\s+\|\s+sid:([0-9a-f]+)\s+\|\s+(.+)$"
)
_CALIB_RE = re.compile(
    r"<!--\s*calibration:\s*right=(\d+)\s+wrong=(\d+)\s+unclear=(\d+)\s*-->"
)

_HEADER_TEMPLATE = _owner.say("# Predictions — «owner»")
_DEFAULT_COUNTERS = {"right": 0, "wrong": 0, "unclear": 0}


@dataclass
class Prediction:
    status: str   # pending | right | wrong | unclear
    date: str      # YYYY-MM-DD
    sid: str        # session_id[:8] the prediction was made in
    text: str


# ---------------------------------------------------------------------------
# Pure parsing / rendering
# ---------------------------------------------------------------------------

def parse_file(text: str) -> tuple[dict, list[Prediction]]:
    """Parse predictions.md into (calibration counters, all prediction lines).

    Degrades cleanly on malformed/missing content: counters default to all
    zero, lines list is whatever valid lines could be recovered (invalid
    lines are silently skipped, never raise).
    """
    counters = dict(_DEFAULT_COUNTERS)
    m = _CALIB_RE.search(text or "")
    if m:
        try:
            counters = {"right": int(m.group(1)), "wrong": int(m.group(2)), "unclear": int(m.group(3))}
        except (TypeError, ValueError):
            counters = dict(_DEFAULT_COUNTERS)

    preds: list[Prediction] = []
    for line in (text or "").splitlines():
        lm = _LINE_RE.match(line.strip())
        if not lm:
            continue
        status, date, sid, body = lm.groups()
        preds.append(Prediction(status=status, date=date, sid=sid, text=body.strip()))
    return counters, preds


def pending(preds: list[Prediction]) -> list[Prediction]:
    return [p for p in preds if p.status == "pending"]


def _calibration_line(counters: dict) -> str:
    right = counters.get("right", 0)
    wrong = counters.get("wrong", 0)
    unclear = counters.get("unclear", 0)
    decided = right + wrong
    if decided == 0:
        return f"Calibration: {right} right / {wrong} wrong / {unclear} unclear — no decided predictions yet"
    rate = round(100 * right / decided)
    return f"Calibration: {right} right / {wrong} wrong / {unclear} unclear — hit rate {rate}% (of decided)"


def render_file(counters: dict, preds: list[Prediction]) -> str:
    """Deterministic full-file render from counters + prediction lines.

    Scored lines are capped at _SCORED_CAP, newest first (by list order —
    callers are expected to pass scored lines oldest-appended-last so a
    simple slice keeps the newest). Pending lines are never capped here.
    """
    pend = [p for p in preds if p.status == "pending"]
    scored = [p for p in preds if p.status != "pending"]
    scored = scored[-_SCORED_CAP:] if len(scored) > _SCORED_CAP else scored

    lines = [
        _HEADER_TEMPLATE,
        f"<!-- calibration: right={counters.get('right', 0)} wrong={counters.get('wrong', 0)} "
        f"unclear={counters.get('unclear', 0)} -->",
        _calibration_line(counters),
        "",
        "## Pending",
    ]
    if pend:
        for p in pend:
            lines.append(f"[{p.status}] {p.date} | sid:{p.sid} | {p.text}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Scored")
    if scored:
        for p in scored:
            lines.append(f"[{p.status}] {p.date} | sid:{p.sid} | {p.text}")
    else:
        lines.append("(none yet)")
    lines.append("")
    return "\n".join(lines)


def format_pending_numbered(preds: list[Prediction]) -> str:
    """Numbered block for the consolidation prompt input, e.g.:
    1. (made 2026-07-14) He will open with X, not Y.
    """
    pend = pending(preds)
    if not pend:
        return ""
    return "\n".join(f"{i}. (made {p.date}) {p.text}" for i, p in enumerate(pend, 1))


def apply_scoring(text: str, verdicts: list[dict]) -> str:
    """Move pending predictions to Scored per `verdicts`, bump counters.

    verdicts: [{"n": 1, "verdict": "right|wrong|unclear", "evidence": "..."}]
    `n` is 1-indexed against the pending list in file order (matches
    format_pending_numbered's numbering). Entries with an out-of-range `n`
    or an unrecognized verdict value are ignored — never raises, never
    corrupts the file.
    """
    counters, preds = parse_file(text)
    pend = pending(preds)
    scored_now: dict[int, dict] = {}
    for v in (verdicts or []):
        try:
            n = int(v.get("n"))
        except (TypeError, ValueError):
            continue
        verdict = str(v.get("verdict", "")).strip().lower()
        if verdict not in ("right", "wrong", "unclear"):
            continue
        if not (1 <= n <= len(pend)):
            continue
        scored_now[n] = {"verdict": verdict, "evidence": str(v.get("evidence", "")).strip()}

    new_preds: list[Prediction] = []
    pend_idx = 0
    for p in preds:
        if p.status != "pending":
            new_preds.append(p)
            continue
        pend_idx += 1
        verdict_info = scored_now.get(pend_idx)
        if verdict_info is None:
            new_preds.append(p)  # still pending — not scored this round
            continue
        verdict = verdict_info["verdict"]
        evidence = verdict_info["evidence"]
        new_text = f"{p.text} — evidence: {evidence}" if evidence else p.text
        new_preds.append(Prediction(status=verdict, date=p.date, sid=p.sid, text=new_text))
        counters[verdict] = counters.get(verdict, 0) + 1

    return render_file(counters, new_preds)


def append_new(text: str, new_preds: list[str], sid: str, date: str) -> str:
    """Append 2-4 new pending predictions for the next session."""
    counters, preds = parse_file(text)
    for p in (new_preds or []):
        p = str(p).strip()
        if not p:
            continue
        preds.append(Prediction(status="pending", date=date, sid=sid, text=p))
    return render_file(counters, preds)


def calibration_summary(text: str) -> str:
    """One line + up to 5 most recent scored predictions, or '' if nothing scored."""
    counters, preds = parse_file(text)
    if counters.get("right", 0) + counters.get("wrong", 0) + counters.get("unclear", 0) == 0:
        return ""
    scored = [p for p in preds if p.status != "pending"]
    recent = scored[-5:]
    lines = [_calibration_line(counters)]
    if recent:
        lines.append("")
        lines.append("Recent:")
        for p in reversed(recent):
            lines.append(f"- [{p.status}] {p.text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Async I/O — thin wrappers around the vault
# ---------------------------------------------------------------------------

async def read_file() -> str:
    """Raw predictions.md content, '' if missing or on error."""
    try:
        if not _PRED_FILE.exists():
            return ""
        return _PRED_FILE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("predictions: read_file failed: %s", e)
        return ""


async def write_file(content: str) -> None:
    """Overwrite predictions.md via vault_write — semindex indexing free."""
    try:
        from backend.tools.vault import vault_write
        await vault_write("predictions.md", content, "people")
    except Exception as e:
        log.warning("predictions: write_file failed: %s", e)


async def read_pending_block() -> str:
    """Framed-ready pending-predictions text for injection into a live turn.
    '' if there are no pending predictions or on any error."""
    try:
        text = await read_file()
        if not text:
            return ""
        _, preds = parse_file(text)
        block = format_pending_numbered(preds)
        if not block:
            return ""
        return block[:_PENDING_BLOCK_CAP]
    except Exception as e:
        log.debug("predictions: read_pending_block failed: %s", e)
        return ""

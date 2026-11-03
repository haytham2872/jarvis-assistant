"""H0: one row per LLM call, so output limits can be sized from measurements.

The thinking-headroom roadmap starts here because every limit in the system was a guess:
the live turn's 16,000 `max_tokens`, research synthesis's 8,000, consolidation's 6,000.
None of them was sized against how much a call actually generates, and reasoning tokens
had never been logged at all -- so nobody knew how close the thinking tier runs to its
ceiling until it hit it (2026-09-28, 65,240 chars of reasoning and no answer).

Each call records what it asked for (purpose, effort, `max_tokens`), what it produced
(reasoning and answer tokens, `finish_reason`, whether it ended in an answer, tool calls
or nothing), what it cost, how long it took, and which OpenRouter host served it. The
host matters because they differ by 12-25x in speed and BaseTen caps output at 32,768.

Purpose comes from the caller. The live path passes it explicitly; the cheap-tier
helpers (`research._llm`, which serves the picker, synthesis, the brief, rumination, the
evolution reflection, both judges and profile fills) read it from `current_purpose`, set
around the call with `labelled(...)` so their many test fakes keep their signature.

Additive and non-fatal: a row that cannot be written must never fail the call it
describes. Nothing reads these rows to make a decision yet; H1 will.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from backend.storage import db

log = logging.getLogger(__name__)

# Outcome of a call, from what it returned rather than from finish_reason alone.
ANSWER = "answer"
TOOL_CALLS = "tool_calls"
EMPTY = "empty"

# Purpose of the live path's leak-recovery call (engine._chat_compat): the thinking tier
# produced no answer and a direct standard-tier call had to be made instead. Counting
# these is the roadmap's recovery counter -- H1's acceptance is that it becomes rare.
RECOVERY = "recovery"

current_purpose: ContextVar[str | None] = ContextVar("llm_call_purpose", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id          TEXT PRIMARY KEY,
    run_id           TEXT,
    started_at       REAL NOT NULL,
    purpose          TEXT NOT NULL,
    model            TEXT NOT NULL,
    effort           TEXT NOT NULL,
    max_tokens       INTEGER,
    input_tokens     INTEGER,
    cached_tokens    INTEGER,
    output_tokens    INTEGER,
    reasoning_tokens INTEGER,
    answer_tokens    INTEGER,
    finish_reason    TEXT,
    outcome          TEXT,
    latency_ms       INTEGER,
    host             TEXT,
    iteration        INTEGER,
    plan_active      INTEGER NOT NULL DEFAULT 0,
    cost             REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_time ON llm_calls (started_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_purpose ON llm_calls (purpose, started_at);
"""


def _ensure():
    """Connection with the table present. Created here rather than in `db._MIGRATIONS`
    for the same reason as `runs` and `operations`: schema v1 has shipped."""
    conn = db.connect()
    if conn is None:
        return None
    try:
        conn.executescript(_SCHEMA)
        return conn
    except Exception as exc:
        log.debug("storage: llm_calls table unavailable (%s)", exc)
        return None


@contextmanager
def labelled(purpose: str):
    """Attribute every call made inside the block to `purpose`."""
    token = current_purpose.set(purpose)
    try:
        yield
    finally:
        current_purpose.reset(token)


def effort_of(extra_body) -> str:
    """The reasoning setting a request actually sent, read from its `extra_body`.

    `none` = reasoning explicitly off; `low`/`high`/`max` = OpenRouter's effort level;
    `default` = nothing said, so the provider's own default applies (a call with no
    extra_body at all; DeepSeek then thinks).
    """
    try:
        reasoning = (extra_body or {}).get("reasoning")
        if isinstance(reasoning, dict):
            if reasoning.get("enabled") is False:
                return "none"
            effort = reasoning.get("effort")
            if effort:
                return str(effort)
    except Exception:
        pass
    return "default"


def _int(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def usage_fields(usage) -> dict:
    """Token counts from an OpenAI-compatible or Anthropic usage object.

    Missing fields stay None rather than 0: "the provider did not say" is different
    from "the model generated nothing", and the percentiles must skip the former.
    """
    if usage is None:
        return {}
    get = (lambda k: usage.get(k)) if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    input_tokens = _int(get("prompt_tokens"))
    output_tokens = _int(get("completion_tokens"))
    if input_tokens is None and output_tokens is None:        # Anthropic shape
        input_tokens = _int(get("input_tokens"))
        output_tokens = _int(get("output_tokens"))
        cached = _int(get("cache_read_input_tokens"))
        reasoning = None
    else:
        pdet = get("prompt_tokens_details")
        cdet = get("completion_tokens_details")
        pget = (lambda k: pdet.get(k)) if isinstance(pdet, dict) else (lambda k: getattr(pdet, k, None))
        cget = (lambda k: cdet.get(k)) if isinstance(cdet, dict) else (lambda k: getattr(cdet, k, None))
        cached = _int(pget("cached_tokens")) if pdet is not None else None
        reasoning = _int(cget("reasoning_tokens")) if cdet is not None else None
    answer = None
    if output_tokens is not None:
        answer = output_tokens - (reasoning or 0)
    cost = get("cost")
    try:
        cost = None if cost is None else float(cost)
    except (TypeError, ValueError):
        cost = None
    return {
        "input_tokens": input_tokens, "cached_tokens": cached,
        "output_tokens": output_tokens, "reasoning_tokens": reasoning,
        "answer_tokens": answer, "cost": cost,
    }


def outcome_of(text: str, tool_calls) -> str:
    if tool_calls:
        return TOOL_CALLS
    return ANSWER if (text or "").strip() else EMPTY


def record(
    *,
    model: str,
    started_at: float,
    purpose: str | None = None,
    usage=None,
    finish_reason: str | None = None,
    extra_body=None,
    effort: str | None = None,
    max_tokens: int | None = None,
    host: str | None = None,
    outcome: str | None = None,
    iteration: int | None = None,
    plan_active: bool = False,
) -> None:
    """Write one call's row. Never raises."""
    try:
        conn = _ensure()
        if conn is None:
            return
        from backend.storage.operations import current_run_id
        fields = usage_fields(usage)
        with conn:
            conn.execute(
                "INSERT INTO llm_calls (call_id, run_id, started_at, purpose, model, effort,"
                " max_tokens, input_tokens, cached_tokens, output_tokens, reasoning_tokens,"
                " answer_tokens, finish_reason, outcome, latency_ms, host, iteration,"
                " plan_active, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), current_run_id.get(), started_at,
                 purpose or current_purpose.get() or "unlabelled", model,
                 effort or effort_of(extra_body), _int(max_tokens),
                 fields.get("input_tokens"), fields.get("cached_tokens"),
                 fields.get("output_tokens"), fields.get("reasoning_tokens"),
                 fields.get("answer_tokens"), finish_reason, outcome,
                 int(max(0.0, time.time() - started_at) * 1000),
                 host, iteration, 1 if plan_active else 0, fields.get("cost")),
            )
    except Exception as exc:
        log.debug("storage: llm call row not written (%s)", exc)


def record_response(resp, *, model: str, started_at: float, purpose: str | None = None,
                    extra_body=None, effort: str | None = None,
                    max_tokens: int | None = None) -> None:
    """`record` for a non-streaming OpenAI-compatible or Anthropic response."""
    try:
        finish = text = None
        tool_calls = None
        choices = getattr(resp, "choices", None)
        if choices:                                           # OpenAI-compatible
            choice = choices[0]
            finish = getattr(choice, "finish_reason", None)
            message = getattr(choice, "message", None)
            text = getattr(message, "content", None)
            tool_calls = getattr(message, "tool_calls", None)
        else:                                                 # Anthropic
            finish = getattr(resp, "stop_reason", None)
            finish = "length" if finish == "max_tokens" else finish
            blocks = getattr(resp, "content", None) or []
            text = "".join(getattr(b, "text", "") or "" for b in blocks
                           if getattr(b, "type", "") == "text")
            tool_calls = [b for b in blocks if getattr(b, "type", "") == "tool_use"]
        record(
            model=model, started_at=started_at, purpose=purpose,
            usage=getattr(resp, "usage", None), finish_reason=finish,
            extra_body=extra_body, effort=effort, max_tokens=max_tokens,
            host=getattr(resp, "provider", None), outcome=outcome_of(text or "", tool_calls),
        )
    except Exception as exc:
        log.debug("storage: llm call row not written (%s)", exc)


def recent(since_s: float = 86400.0, now: float | None = None) -> list[dict]:
    conn = _ensure()
    if conn is None:
        return []
    cutoff = (now if now is not None else time.time()) - since_s
    try:
        rows = conn.execute(
            "SELECT * FROM llm_calls WHERE started_at >= ? ORDER BY started_at", (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# H1: a turn costing more than this is logged and named in the next morning brief.
EXPENSIVE_TURN_USD = 0.25


def turn_totals(since_s: float = 86400.0, now: float | None = None) -> list[dict]:
    """Per-run totals (one run = one turn or one background job), deepest first.

    Joined with the run's request preview so a report can say which request it was.
    """
    conn = _ensure()
    if conn is None:
        return []
    cutoff = (now if now is not None else time.time()) - since_s
    try:
        from backend.storage import runs as _runs
        _runs._ensure()
        rows = conn.execute(
            "SELECT c.run_id, COUNT(*) AS calls, MIN(c.started_at) AS started_at,"
            " COALESCE(SUM(c.output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(c.reasoning_tokens), 0) AS reasoning_tokens,"
            " COALESCE(SUM(c.cost), 0) AS cost, r.user_preview AS preview, r.channel AS channel"
            " FROM llm_calls c LEFT JOIN runs r ON r.run_id = c.run_id"
            " WHERE c.started_at >= ? AND c.run_id IS NOT NULL"
            " GROUP BY c.run_id ORDER BY output_tokens DESC", (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("storage: turn totals unavailable (%s)", exc)
        return []


def run_cost(run_id: str | None) -> float:
    if not run_id:
        return 0.0
    conn = _ensure()
    if conn is None:
        return 0.0
    try:
        row = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_calls WHERE run_id = ?",
                           (run_id,)).fetchone()
        return float(row[0] or 0.0)
    except Exception:
        return 0.0


def _describe(t: dict) -> str:
    when = time.strftime("%H:%M", time.localtime(t["started_at"]))
    preview = (t.get("preview") or "").strip().replace("\n", " ")
    preview = f'"{preview[:70]}{"..." if len(preview) > 70 else ""}"' if preview else "(background work)"
    return (f"{when} {preview}: {t['output_tokens']:,} output tokens "
            f"({t['reasoning_tokens']:,} reasoning) over {t['calls']} calls, ${t['cost']:.3f}")


def deepest_turns_lines(since_s: float = 86400.0, n: int = 3, now: float | None = None) -> list[str]:
    """For /cost: the turns that generated the most output in the last day."""
    totals = turn_totals(since_s, now)[:n]
    if not totals:
        return []
    return ["Deepest turns (24h):"] + [f"  {_describe(t)}" for t in totals]


def expensive_turns_text(since_s: float = 86400.0, now: float | None = None) -> str:
    """For the morning brief: turns over EXPENSIVE_TURN_USD in the last day, or ''."""
    over = [t for t in turn_totals(since_s, now) if t["cost"] > EXPENSIVE_TURN_USD]
    return "\n".join(_describe(t) for t in sorted(over, key=lambda t: t["started_at"]))


def _percentile(values: list[int], q: float) -> int:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def status_lines(since_s: float = 86400.0, now: float | None = None) -> list[str]:
    """`[Runtime status]` lines: reasoning tokens by effort, recoveries, and calls cut
    off at their output limit. Empty when nothing has been recorded -- a line of
    zeros on a fresh process is noise."""
    rows = recent(since_s, now)
    if not rows:
        return []
    by_effort: dict[str, list[int]] = {}
    for r in rows:
        if r["effort"] in ("none",) or r["reasoning_tokens"] is None:
            continue
        by_effort.setdefault(r["effort"], []).append(int(r["reasoning_tokens"]))
    lines = []
    if by_effort:
        parts = [
            f"{effort} p50 {_percentile(v, 0.5):,} / p95 {_percentile(v, 0.95):,} (n={len(v)})"
            for effort, v in sorted(by_effort.items())
        ]
        lines.append("Reasoning tokens per call (24h): " + "; ".join(parts))
    recoveries = sum(1 for r in rows if r["purpose"] == RECOVERY)
    cut: dict[str, int] = {}
    for r in rows:
        if r["finish_reason"] == "length":
            cut[r["purpose"]] = cut.get(r["purpose"], 0) + 1
    if recoveries or cut:
        detail = ", ".join(f"{p} {n}" for p, n in sorted(cut.items())) or "none"
        lines.append(f"Output limit hit (24h): {detail}; no-answer recoveries: {recoveries}")
    return lines

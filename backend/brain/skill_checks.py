"""backend/brain/skill_checks.py — a reply that followed a skill meets the skill's Checks.

Skills slice 4 (4 October 2026). A skill ends with a short Checks list ("every figure has
its source and date", "both a bull and a bear case"). Before a Discord reply that followed
a skill is delivered, one cheap-tier call reads the draft against those checks; if any
clearly fails, the turn gets one more pass to fix the reply, the same shape as the claim
check (engine._enforce_backed_claims). Discord only: there the reply is sent after chat()
returns, while on the desk the speech has already streamed and cannot be taken back.

Bounded to one fix: a reply still failing afterwards is delivered and logged, not looped
on. Never raises: a check that cannot run (no model, an unreadable answer) passes the
draft through unchanged, since a slower or failed check must not cost him the reply.

The checks apply only to a reply that carries out the skill's task (5 October 2026). Live,
"cancel and re-add each one against the skill" opened stock-research, the scheduling reply
was judged against "both a bull and a bear case", and the fix pass added a bull and a bear
case on NVDA and SPCX with closing prices nothing had looked up. So the judge sees his
message and the skill's description, and first answers whether the reply does that task;
when it does not, nothing is checked and the turn no longer counts as following the skill.
"""
from __future__ import annotations
from backend import owner as _owner

import json
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

_JUDGE_MAX_TOKENS = 400
_MAX_REPLY_CHARS = 12_000

_JUDGE = _owner.say("""You check a reply before it is sent. While writing it, the assistant opened this
procedure:
{skills}

The message it answers:
<message>
{request}
</message>

The reply:
<reply>
{reply}
</reply>

First decide whether the reply carries out that procedure's task for this message. It does
not when it only sets the task up to run later, describes or changes the procedure, answers
a narrower or different question (a quick price is not research), or asks a question
instead of answering. If it does not, answer {{"applies": false, "checks": []}}.

If it does, go through these checks one by one:
{checks}
A check fails when the reply lacks what it asks for (a bull case with no bear case fails
"both a bull and a bear case"). A check whose condition does not apply to this reply ("if
«he» holds it" when «he» holds nothing) passes.

Answer with JSON only:
{{"applies": true or false, "checks": [{{"check": "<the check's text>", "pass": true or false}}, ...]}}""")

_FIX = ("[Check before sending: your reply does not yet meet these checks of the skill you "
        "followed ({skill}): {failed}. Rewrite the reply so it does, keeping everything else. "
        "Add no figure, date or source you did not get from a tool in this conversation: look "
        "it up with a tool, or leave it out and say so. Don't mention this check.]")


def _checks_of(names: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """([(skill, check)], ["name: description"]) for the skills followed this turn."""
    from backend.brain import skills
    out, about = [], []
    for name in names:
        s = skills.load(name)
        if s is None or not s.active:
            continue
        about.append(f"{s.name}: {s.description}")
        for line in skills._section(s.body, ("checks", "verification")).splitlines():
            text = skills._NUMBERING.sub("", line).strip()
            if text:
                out.append((s.name, text))
    return out, about


def _parse(raw: str) -> tuple[bool, list[str]] | None:
    """(applies, failed checks), or None when the answer is unreadable. An answer without
    "applies" counts as applying, so a missing key waves nothing through."""
    start, end = (raw or "").find("{"), (raw or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("applies") is False:
        return False, []
    if isinstance(data.get("checks"), list):
        return True, [str(c.get("check", "")) for c in data["checks"]
                      if isinstance(c, dict) and c.get("pass") is False and str(c.get("check", "")).strip()]
    failed = data.get("failed")              # the shorter form, also accepted
    if not isinstance(failed, list):
        return None
    return True, [str(f) for f in failed if str(f).strip()]


async def review(reply: str, names: list[str], request: str = "") -> tuple[bool, list[str]]:
    """(the reply carries out the skill's task, the checks it clearly fails). (True, []) when
    the check cannot run: a failed check must not cost him the reply or the line."""
    checks, about = _checks_of(names)
    if not checks or not (reply or "").strip():
        return True, []
    from backend.brain import research
    from backend.storage import llm_calls
    listed = "\n".join(f"- {c}" for _, c in checks)
    prompt = _JUDGE.format(skills="\n".join(about), request=(request or "(not given)")[:2000],
                           checks=listed, reply=reply[:_MAX_REPLY_CHARS])
    try:
        with llm_calls.labelled("skill.check"):
            raw = await research._llm(prompt, max_tokens=_JUDGE_MAX_TOKENS)
    except Exception as exc:
        log.warning("skill checks: could not run (%s)", exc)
        return True, []
    parsed = _parse(raw)
    if parsed is None:
        log.info("skill checks: unreadable answer, reply passed through")
        return True, []
    applies, failed = parsed
    known = {c for _, c in checks}
    return applies, ([f for f in failed if f in known]
                     or [f for f in failed if any(f in k or k in f for k in known)])


async def failed_checks(reply: str, names: list[str], request: str = "") -> list[str]:
    """The checks the draft clearly fails ([] when all pass, they do not apply, or the check
    cannot run)."""
    return (await review(reply, names, request))[1]


async def enforce(reply: str, names: list[str], messages: list[dict],
                  rerun: Callable[[list[dict]], Awaitable[str]], request: str = "") -> str:
    """The reply, fixed once if it misses a check of a skill it followed. A reply that does
    not carry out the skill's task is left as it is and no longer counts as following it."""
    applies, failed = await review(reply, names, request)
    if not applies:
        from backend.brain import skills
        log.info("skill checks: the reply does not carry out %s; not checked", ", ".join(names))
        skills.set_aside(names)
        return reply
    if not failed:
        return reply
    log.info("skill checks: draft misses %d check(s) of %s: %s", len(failed), ", ".join(names),
             "; ".join(failed))
    retry = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": _FIX.format(skill=", ".join(names), failed="; ".join(failed))},
    ]
    try:
        fixed = await rerun(retry) or ""
    except Exception as exc:
        log.warning("skill checks: fix pass failed (%s)", exc)
        return reply
    return fixed if fixed.strip() else reply

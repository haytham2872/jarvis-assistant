"""Shadow promise check (G3, docs/archive/GOALS-ROADMAP.md B1): does a reply promise future work that
nothing saved this turn or session can actually do?

"I'll keep an eye on the listings and send you anything good" with no watch, goal or
schedule behind it is a promise only memory could keep -- and memory does not act. The
roadmap's capability rule: a reminder backs a notification, not future drafting; a
scheduled task or a goal backs work; a watch backs a check.

Shadow mode: every detected promise is typed, checked against the tools that succeeded
this turn (and earlier in the session), and recorded in `promise_checks`. Nothing here
changes a reply or is shown to the owner. The verdicts are for measuring false alarms
before any enforcement is considered.

Detection is deliberately narrow: a first-person future commitment ("I'll", "I will",
"I'm going to") in a sentence that is not a question or an offer ("want me to...", "if
you'd like, I'll..."), with a future marker or a verb that only makes sense later. An
"I'll" about this reply itself ("I'll explain", "I'll check now") is not a promise.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

NOTIFY, DRAFT, MONITOR, RESEARCH = "notify", "draft", "monitor", "research"

# What each kind of promise needs behind it (tool names as _turn_tools_ok records them).
_BACKERS: dict[str, frozenset[str]] = {
    NOTIFY: frozenset({"schedule_reminder", "schedule_task", "schedule_night_task", "watch_action",
                       "goal", "run_in_background", "web_research", "continue_in_background"}),
    MONITOR: frozenset({"watch_action", "goal", "schedule_task"}),
    # a reminder backs a notification, never the drafting itself
    DRAFT: frozenset({"goal", "schedule_task", "schedule_night_task", "run_in_background",
                      "continue_in_background"}),
    RESEARCH: frozenset({"goal", "schedule_task", "schedule_night_task", "run_in_background",
                         "web_research", "continue_in_background", "research_queue"}),
}
_ANY_BACKER = frozenset().union(*_BACKERS.values())

_COMMIT = re.compile(r"\b(?:I'll|I’ll|I will|I'm going to|I’m going to|I am going to)\s+"
                     r"(?!not\b|never\b)", re.I)
_OFFER = re.compile(r"\b(?:if you(?:'d| would)? (?:like|want|prefer)|want me to|shall I|should I|"
                    r"would you like|do you want|I can\b|I could\b|happy to|say the word|"
                    r"just say|let me know if|if that helps|unless you)", re.I)
_FUTURE = re.compile(
    r"\b(?:tomorrow|tonight|later|this (?:afternoon|evening|week|weekend)|next (?:week|month|"
    r"time|session)|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|by "
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|the end|end of|then)|"
    r"in the morning|when\b|once\b|as soon as|after (?:the|his|her|their|earnings|that)|"
    r"every\b|each (?:day|week|morning|evening|time)|daily|weekly|over the (?:next|coming)|"
    r"until\b|whenever|the moment|in (?:a|an|\d+) (?:few )?(?:minutes?|hours?|days?|weeks?)|"
    r"keep\b|continue to|check back|get back to you|circle back|follow up|come back to you|"
    r"overnight|in the background|check in on|again as soon|"
    r"(?:in|within) (?:a |an |the next |\w+ )?(?:few )?(?:minutes?|hours?|days?|weeks?)\b)", re.I)
# How he will behave from now on is a standing rule (standing_instruction), not future work.
_RULE = re.compile(r"\b(?:from now on|next time you|whenever you|every time you|each time you|"
                   r"when you (?:ask|invite|bring|tell|send|give|want))\b", re.I)
_NOW = re.compile(r"\b(?:now|right now|right away|first|below|here|in this (?:reply|answer|message))\b"
                  r"|\b(?:explain|walk you through|break (?:it|this) down|start with|answer|"
                  r"summari[sz]e|be (?:brief|honest|direct))\b", re.I)

_KIND_VERBS = [
    (DRAFT, re.compile(r"\b(?:draft|write (?:to|him|her|them|back|the|a|an)|send (?:him|her|them|"
                       r"the|a|an)|e-?mail (?:him|her|them)|prepare (?:a|the|your)|book|chase|"
                       r"follow up with|reply to|file|submit|retry|try (?:it|that|the \w+) again)\b",
                       re.I)),
    (RESEARCH, re.compile(r"\b(?:research|look into|dig into|dig deeper|read up|investigate|"
                          r"find out|compile|gather)\b", re.I)),
    (MONITOR, re.compile(r"\b(?:keep an eye|watch|monitor|track|keep checking|keep looking|"
                         r"keep tabs|check (?:on|in|back|again)|keep searching)\b", re.I)),
    (NOTIFY, re.compile(r"\b(?:remind you|let you know|ping you|message you|tell you|dm you|"
                        r"alert you|get back to you|update you|send you|report back|"
                        r"come back to you|you'll hear|have (?:the |your |an? )?(?:findings|results|"
                        r"answer|report)(?: \w+)? ready)\b", re.I)),
]

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def detect(reply: str) -> list[tuple[str, str]]:
    """[(kind, sentence)] for each future commitment in the reply. Pure."""
    out: list[tuple[str, str]] = []
    for sentence in _SENTENCE.split(reply or ""):
        s = sentence.strip()
        if not s or s.endswith("?"):
            continue
        m = _COMMIT.search(s)
        if not m:
            continue
        before, after = s[:m.start()], s[m.end():]
        if _OFFER.search(before) or _OFFER.search(after[:40]):
            continue
        if _RULE.search(s) or not _FUTURE.search(s):   # "It'll run within four hours and I'll..."
            continue
        if _NOW.match(after) or re.match(r"(?:now|right now|first)\b", after, re.I):
            continue
        kind = next((k for k, rx in _KIND_VERBS if rx.search(after)), None)
        if kind is None:
            continue
        out.append((kind, s[:400]))
    return out


def verdict(kind: str, ok_tools: set[str]) -> tuple[str, str]:
    """(verdict, backing): backed by a tool that can do this kind of work, a mismatch (only
    something that cannot -- a reminder behind a promise to draft), or unbacked."""
    # Bare names only: engine.py records a read (schedule_task list) as "tool:action" and
    # never as the bare name, so a read cannot back a promise of work.
    names = {t for t in ok_tools if ":" not in t}
    backing = sorted(names & _BACKERS[kind])
    if backing:
        return "backed", ",".join(backing)
    other = sorted(names & _ANY_BACKER)
    if other:
        return "mismatch", ",".join(other)
    return "unbacked", ""


def check(reply: str, ok_tools: set[str], *, run_id: str | None = None,
          channel: str = "") -> list[dict]:
    """Detect, judge and record (shadow). Returns what was recorded. Never raises."""
    try:
        found = detect(reply)
        if not found:
            return []
        from backend.storage import promises
        rows = []
        for kind, sentence in found:
            v, backing = verdict(kind, ok_tools)
            rows.append({"kind": kind, "verdict": v, "backing": backing, "sentence": sentence})
            promises.record(run_id=run_id, channel=channel, kind=kind, verdict=v,
                            backing=backing, sentence=sentence)
        if any(r["verdict"] != "backed" for r in rows):
            log.info("promise check (shadow): %s", [(r["kind"], r["verdict"]) for r in rows])
        return rows
    except Exception as e:
        log.debug("promise check skipped (%s)", e)
        return []

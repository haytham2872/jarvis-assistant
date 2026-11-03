"""The conclusion of a finished background result, shown in the phone app as JARVIS's message.

The owner, 15 October 2026: the whole research belongs in the collapsible job card; only its
conclusion should arrive as a normal message. So a phone-started job that finishes gets a
short conclusion, from the cheapest source that has one:

1. the report's own conclusion: the last section headed Conclusion, TL;DR, Bottom line,
   Verdict, Summary, In short, Takeaways or Recommendation (a heading or a bold label);
2. otherwise one cheap-tier call: two or three sentences, the answer first, nothing the
   result does not say;
3. otherwise (no model, a failed call) the result's last paragraph before its sources.

A result short enough to read at a glance is its own conclusion. Never raises.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import logging
import re

log = logging.getLogger(__name__)

SHORT_RESULT = 500          # a result this short is its own conclusion
MAX_CONCLUSION = 900        # a conclusion section longer than this is cut at a sentence
_INPUT_HEAD = 12_000        # what the model sees of a long report: its start and its end,
_INPUT_TAIL = 12_000        # where conclusions sit
_CALL_TIMEOUT_S = 25.0
_MAX_TOKENS = 400

_LABELS = (r"tl;?\s?dr|conclusions?|bottom\s+line|verdict|summary|in\s+short|"
           r"the\s+short\s+answer|short\s+answer|key\s+takeaways?|takeaways?|recommendations?|"
           r"overall")
# "## Conclusion", "### Bottom line:", "**TL;DR**", "**Verdict:** the rest of the line"
_HEADING = re.compile(rf"^\s{{0,3}}(?:#{{1,6}}\s*|\*\*|__)\s*(?:{_LABELS})\b\s*:?\s*(?:\*\*|__)?\s*:?\s*(.*)$",
                      re.IGNORECASE)
_ANY_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s+\S|(?:\*\*|__)[^*_\n]{1,80}(?:\*\*|__)\s*:?\s*$)")
_SOURCES = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?sources?\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

_PROMPT = _owner.say("""Below is a result JARVIS produced for «owner» in the background. Write its \
conclusion the way JARVIS would tell «him»: two or three plain sentences, the answer first, \
using only facts and figures that appear in the result. No headings, no lists, no \
preamble ("The research shows..."), no sources.

RESULT:
{result}
""")


def _cap(text: str, limit: int = MAX_CONCLUSION) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    out = ""
    for s in _SENTENCE_END.split(text):
        if out and len(out) + 1 + len(s) > limit:
            break
        out = f"{out} {s}" if out else s
    return out if out else text[:limit].rstrip() + "…"


def _without_sources(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if _SOURCES.match(lines[i]):
            return "\n".join(lines[:i]).rstrip()
    return text


def section(result: str) -> str:
    """The report's own last conclusion section, or ''."""
    lines = _without_sources(result).split("\n")
    start = None
    for i, line in enumerate(lines):
        if _HEADING.match(line):
            start = i
    if start is None:
        return ""
    first = _HEADING.match(lines[start]).group(1).strip()
    body = [first] if first else []
    for line in lines[start + 1:]:
        if _ANY_HEADING.match(line):
            break
        body.append(line)
    return _cap("\n".join(body).strip())


def last_paragraph(result: str) -> str:
    paras = [p.strip() for p in _without_sources(result).split("\n\n") if p.strip()]
    for p in reversed(paras):
        if not _ANY_HEADING.match(p) and len(p) > 40:
            return _cap(p)
    return _cap(paras[-1]) if paras else ""


def _model_input(result: str) -> str:
    text = _without_sources(result)
    if len(text) <= _INPUT_HEAD + _INPUT_TAIL:
        return text
    return f"{text[:_INPUT_HEAD]}\n\n[...]\n\n{text[-_INPUT_TAIL:]}"


async def _ask_model(result: str) -> str:
    from backend.brain.research import _llm
    from backend.storage.llm_calls import labelled
    with labelled("job.conclusion"):
        text = await asyncio.wait_for(_llm(_PROMPT.format(result=_model_input(result)),
                                           max_tokens=_MAX_TOKENS), timeout=_CALL_TIMEOUT_S)
    return _cap(text or "")


async def of(result: str) -> str:
    """The conclusion to show as a message for `result`. Never raises."""
    result = (result or "").strip()
    if not result:
        return ""
    if len(result) <= SHORT_RESULT:
        return result
    found = section(result)
    if found:
        return found
    try:
        text = await _ask_model(result)
        if text:
            return text
    except Exception as exc:
        log.warning("conclusion: model call failed (%s); using the last paragraph", exc)
    return last_paragraph(result)

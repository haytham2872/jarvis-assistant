"""H4: a long Discord answer goes to the vault in full, with a summary in the chat.

Decision 4 of the thinking-headroom roadmap. A research conclusion of several thousand
characters arrives on a phone as a wall of four or five messages. Past `LONG_REPLY_CHARS`
the full answer is written to `shared/<date>-<slug>.md` (through `vault_write`, so it is
verified and indexed), and Discord gets a short summary plus the note's name. The owner can
then ask for it and `vault_send` posts it word for word.

Threshold: the roadmap proposed about 3,000 characters. It is 4,000 here, because the H0
benchmark shows answers of 2,000-3,000 characters are common (p95 about 1,565 tokens),
and a reply of up to two Discord messages still reads fine; a lower bar would turn
ordinary answers into "see the file".

Only the Discord delivery changes. Conversation history, memory and the thread record
keep the full answer, so JARVIS still knows what he said. Nothing is lost on failure: if
the note cannot be written the full answer is sent as before, and if the summary call
fails the opening paragraphs stand in for it.
"""
from __future__ import annotations
from backend import owner as _owner

import datetime
import logging
import re

log = logging.getLogger(__name__)

LONG_REPLY_CHARS = 4000
_SUMMARY_MAX_TOKENS = 600
_SUMMARY_CHARS = 1200           # the fallback summary: opening paragraphs up to this

_SUMMARY_PROMPT = _owner.say("""\
Below is an answer JARVIS wrote for «owner». It is too long for a Discord message, so the
full text is being saved to «his» vault. Write the short version «he» will read in the chat:
at most five sentences, plain prose in JARVIS's own voice (first person, no markdown),
keeping the conclusion and the one or two facts or numbers that matter most. Do not
mention the vault or the saving; that line is added separately. Output only the summary.

ANSWER:
{answer}
""")


def _slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].rstrip("-") or "answer"


def note_name(request: str, now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    return f"{now:%Y-%m-%d}-{_slug(' '.join((request or '').split()[:8]))}.md"


def fallback_summary(answer: str) -> str:
    """Opening paragraphs, whole, up to _SUMMARY_CHARS."""
    out = ""
    for para in [p for p in answer.split("\n\n") if p.strip()]:
        if out and len(out) + len(para) > _SUMMARY_CHARS:
            break
        out = f"{out}\n\n{para}" if out else para
    return out[:_SUMMARY_CHARS * 2]


async def _summarise(answer: str) -> str:
    try:
        from backend.brain.research import _llm
        from backend.storage.llm_calls import labelled
        with labelled("long_reply.summary"):
            text = (await _llm(_SUMMARY_PROMPT.format(answer=answer),
                               max_tokens=_SUMMARY_MAX_TOKENS) or "").strip()
        if text:
            return text
    except Exception as e:
        log.warning("long_reply: summary call failed (%s) -- using the opening", e)
    return fallback_summary(answer)


async def shape_for_discord(answer: str, request: str) -> str:
    """The text to send on Discord for `answer`. Never raises; never loses the answer."""
    if not answer or len(answer) <= LONG_REPLY_CHARS:
        return answer
    try:
        from backend.tools import outcomes
        from backend.tools.vault import vault_write
        name = note_name(request)
        title = " ".join((request or "Answer").split())[:120]
        result = await vault_write(name, f"# {title}\n\n{answer}\n", "shared")
        if outcomes.is_failure("vault_write", result):
            log.warning("long_reply: could not save %s (%s) -- sending in full", name, result)
            return answer
    except Exception as e:
        log.warning("long_reply: save failed (%s) -- sending in full", e)
        return answer
    summary = await _summarise(answer)
    return (f"{summary}\n\nThe full answer ({len(answer):,} characters) is saved as "
            f"shared/{name}. Ask and I'll send it to you word for word.")

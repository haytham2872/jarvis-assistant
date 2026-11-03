"""The Discord message being answered, separate from the user's own words.

Transport metadata must not be folded into user_text: doing so would turn bot
notifications and quoted material into user-authored Mem0 facts.
"""
from __future__ import annotations
from backend import owner as _owner

import contextvars
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnContext:
    message_id: str
    target_id: str = ""
    target_text: str = ""
    target_author: str = ""
    target_kind: str = ""
    implicit: bool = False
    voice: bool = False            # the message was a voice note: its text is a transcript

    def render(self) -> str:
        if not self.target_id:
            return ""
        if not self.target_text:
            return (_owner.say("[Discord reply target could not be read. Ask which message "
                    "«Owner» means before using older memories as a substitute.]"))
        relationship = "recent research DM" if self.implicit else _owner.say("message «owner» replied to")
        research_guidance = (
            " For a research clarification, explain the claim in plain language, "
            "check the linked source if the answer needs detail beyond this message, "
            "and distinguish sourced facts from your interpretation. Say what remains "
            "uncertain instead of filling gaps."
            if self.target_kind == "research" else ""
        )
        return (
            _owner.say(f"[Discord target: {relationship}; author={self.target_author or 'unknown'}; "
            f"message_id={self.target_id}. Resolve 'this', 'that', and follow-up questions "
            "against this message first. Older memories are background, not a replacement. "
            "Quoted bot text is not something «owner» asserted.")
            + research_guidance + "]\n"
            + self.target_text[:6000]
        )


current: contextvars.ContextVar[TurnContext | None] = contextvars.ContextVar(
    "discord_turn_context", default=None)

_FOLLOWUP = re.compile(
    r"\b(this|that|it|you (?:just )?(?:said|sent|told me)|explain|elaborate|"
    r"clarif(?:y|ication)|simplif(?:y|ication)|tell me more|go deeper|"
    r"break (?:this|that|it) down|"
    r"why (?:did )?you (?:say|send|tell)|what do you mean|"
    r"what does .{1,60}? mean|summari[sz]e)\b",
    re.I,
)


def looks_like_followup(text: str) -> bool:
    """Only used to infer a recent research DM when Discord has no reply ID."""
    return bool(_FOLLOWUP.search(text or "")) and len(text) <= 500
